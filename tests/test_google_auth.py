"""Offline tests for shared Google OAuth infrastructure."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr

from ricky.config import (
    GoogleAccountSettings,
    GoogleOAuthClientSettings,
    GoogleSettings,
    ProfileConfigSettings,
    RickySettings,
)
from ricky.tools.integrations.google.auth import (
    GoogleAuth,
    GoogleAuthError,
    GoogleAuthStatus,
    GoogleTokenRecord,
    _LoopbackCallback,
    _pkce_pair,
)

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
PERSONAL_ACCOUNT = "personal/personal"
WORK_ACCOUNT = "work/work"


def _settings(tmp_path: Path, *, callback_timeout: float = 1.0) -> RickySettings:
    google_common = {
        "auth_base_url": "https://auth.test/authorize",
        "token_url": "https://auth.test/token",
        "userinfo_url": "https://auth.test/userinfo",
        "token_store_path": "google/tokens.json",
        "auth_callback_timeout_seconds": callback_timeout,
    }
    root = RickySettings(
        user_data_dir=str(tmp_path / "user-data"),
        profile_configs={
            "personal": ProfileConfigSettings(
                google=GoogleSettings(
                    **google_common,
                    accounts={"personal": GoogleAccountSettings(email="alex.personal@example.com")},
                ),
                google_oauth_clients={
                    "personal": GoogleOAuthClientSettings(
                        client_id="personal-client",
                        client_secret=SecretStr("personal-client-secret"),
                    )
                },
            ),
            "work": ProfileConfigSettings(
                google=GoogleSettings(
                    **google_common,
                    accounts={"work": GoogleAccountSettings(email="alex@company.example")},
                ),
                google_oauth_clients={
                    "work": GoogleOAuthClientSettings(
                        client_id="work-client",
                        client_secret=SecretStr("work-client-secret"),
                    )
                },
            ),
        },
    )
    return root.resolve_profile_runtime_settings(
        root.resolve_profile_scope("personal", access_profiles=["work"])
    )


async def _send_callback(redirect_uri: str, *, state: str, code: str = "code-1") -> None:
    parsed = urlsplit(redirect_uri)
    reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
    path = f"/?code={code}&state={state}"
    writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    await reader.read()
    writer.close()
    await writer.wait_closed()


async def _authorize(
    auth: GoogleAuth,
    account: str = PERSONAL_ACCOUNT,
) -> tuple[GoogleAuthStatus, str]:
    seen_url = ""
    callback_task: asyncio.Task[None] | None = None

    def on_url(url: str) -> None:
        nonlocal seen_url, callback_task
        seen_url = url
        query = parse_qs(urlsplit(url).query)
        callback_task = asyncio.create_task(
            _send_callback(
                query["redirect_uri"][0],
                state=query["state"][0],
            )
        )

    status = await auth.authorize(account, on_authorization_url=on_url)
    if callback_task is not None:
        await callback_task
    return status, seen_url


def test_pkce_pair_uses_s256_and_valid_verifier_shape() -> None:
    verifier, challenge = _pkce_pair()

    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    assert 43 <= len(verifier) <= 128
    assert challenge == expected
    assert "=" not in challenge


async def test_authorize_verifies_identity_and_atomically_stores_mode_0600(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/token":
            form = parse_qs(request.content.decode())
            assert form["client_id"] == ["personal-client"]
            assert form["client_secret"] == ["personal-client-secret"]
            assert form["code_verifier"][0]
            return httpx.Response(
                200,
                json={
                    "access_token": "access-secret",
                    "refresh_token": "refresh-secret",
                    "expires_in": 3600,
                    "scope": f"openid email {GMAIL_SCOPE}",
                },
            )
        assert request.url.path == "/userinfo"
        assert request.headers["Authorization"] == "Bearer access-secret"
        return httpx.Response(200, json={"email": "alex.personal@example.com"})

    auth = GoogleAuth(
        _settings(tmp_path),
        scopes={GMAIL_SCOPE},
        root=tmp_path,
        transport=httpx.MockTransport(handler),
        browser_opener=lambda _url: True,
    )
    status, authorization_url = await _authorize(auth)

    assert status.account == PERSONAL_ACCOUNT
    assert status.token_present
    query = parse_qs(urlsplit(authorization_url).query)
    assert query["client_id"] == ["personal-client"]
    assert query["code_challenge_method"] == ["S256"]
    assert set(query["scope"][0].split()) == {"openid", "email", GMAIL_SCOPE}
    assert query["login_hint"] == ["alex.personal@example.com"]

    token_path = tmp_path / "user-data" / "profiles" / "personal" / "google" / "tokens.json"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    stored = json.loads(token_path.read_text())
    assert stored["accounts"]["personal"]["refresh_token"] == "refresh-secret"
    assert stored["accounts"]["personal"]["oauth_client_id"] == "personal-client"
    assert not list(token_path.parent.glob(".tokens.*.tmp"))

    representation = repr(auth.status(PERSONAL_ACCOUNT))
    assert "refresh-secret" not in representation
    assert "personal-client-secret" not in repr(auth)
    await auth.aclose()
    assert len(requests) == 2


async def test_authorize_refuses_identity_mismatch_without_storing(
    tmp_path: Path,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "access-secret",
                    "refresh_token": "refresh-secret",
                    "expires_in": 3600,
                    "scope": f"openid email {GMAIL_SCOPE}",
                },
            )
        return httpx.Response(200, json={"email": "wrong@example.com"})

    auth = GoogleAuth(
        _settings(tmp_path),
        scopes={GMAIL_SCOPE},
        root=tmp_path,
        transport=httpx.MockTransport(handler),
        browser_opener=lambda _url: True,
    )

    with pytest.raises(GoogleAuthError, match="identity mismatch"):
        await _authorize(auth)

    assert not auth.token_store_paths[PERSONAL_ACCOUNT].exists()
    await auth.aclose()


async def test_concurrent_access_requests_single_flight_refresh(tmp_path: Path) -> None:
    refreshes = 0

    async def initial_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "initial-access",
                    "refresh_token": "refresh-secret",
                    "expires_in": 3600,
                    "scope": f"openid email {GMAIL_SCOPE}",
                },
            )
        return httpx.Response(200, json={"email": "alex.personal@example.com"})

    initial = GoogleAuth(
        _settings(tmp_path),
        scopes={GMAIL_SCOPE},
        root=tmp_path,
        transport=httpx.MockTransport(initial_handler),
        browser_opener=lambda _url: True,
    )
    await _authorize(initial)
    await initial.aclose()

    async def refresh_handler(request: httpx.Request) -> httpx.Response:
        nonlocal refreshes
        refreshes += 1
        await asyncio.sleep(0)
        form = parse_qs(request.content.decode())
        assert form["refresh_token"] == ["refresh-secret"]
        return httpx.Response(
            200,
            json={"access_token": "fresh-access", "expires_in": 3600},
        )

    auth = GoogleAuth(
        _settings(tmp_path),
        scopes={GMAIL_SCOPE},
        root=tmp_path,
        transport=httpx.MockTransport(refresh_handler),
        browser_opener=lambda _url: True,
    )
    tokens = await asyncio.gather(*(auth.get_access_token(PERSONAL_ACCOUNT) for _ in range(8)))

    assert tokens == ["fresh-access"] * 8
    assert refreshes == 1
    await auth.aclose()


async def test_force_refresh_coalesces_when_cache_changed(tmp_path: Path) -> None:
    refreshes = 0

    async def initial_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "initial-access",
                    "refresh_token": "refresh-secret",
                    "expires_in": 3600,
                    "scope": f"openid email {GMAIL_SCOPE}",
                },
            )
        return httpx.Response(200, json={"email": "alex.personal@example.com"})

    initial = GoogleAuth(
        _settings(tmp_path),
        scopes={GMAIL_SCOPE},
        root=tmp_path,
        transport=httpx.MockTransport(initial_handler),
        browser_opener=lambda _url: True,
    )
    await _authorize(initial)
    await initial.aclose()

    gate = asyncio.Event()

    async def refresh_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal refreshes
        refreshes += 1
        gate.set()
        await asyncio.sleep(0)
        return httpx.Response(200, json={"access_token": "refreshed", "expires_in": 3600})

    auth = GoogleAuth(
        _settings(tmp_path),
        scopes={GMAIL_SCOPE},
        root=tmp_path,
        transport=httpx.MockTransport(refresh_handler),
        browser_opener=lambda _url: True,
    )
    await auth.get_access_token(PERSONAL_ACCOUNT)
    first = asyncio.create_task(auth.get_access_token(PERSONAL_ACCOUNT, force_refresh=True))
    await gate.wait()
    second = asyncio.create_task(auth.get_access_token(PERSONAL_ACCOUNT, force_refresh=True))

    assert await first == "refreshed"
    assert await second == "refreshed"
    assert refreshes == 2
    await auth.aclose()


async def test_invalid_grant_names_reauth_command_and_never_leaks_tokens(
    tmp_path: Path,
) -> None:
    token_path = tmp_path / "user-data" / "profiles" / "personal" / "google" / "tokens.json"
    token_path.parent.mkdir(parents=True)
    token_path.write_text(
        json.dumps(
            {
                "accounts": {
                    "personal": {
                        "email": "alex.personal@example.com",
                        "oauth_client_id": "personal-client",
                        "refresh_token": "refresh-secret",
                        "granted_scopes": [GMAIL_SCOPE, "openid", "email"],
                        "obtained_at": "2026-07-19T12:00:00Z",
                    }
                }
            }
        )
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    auth = GoogleAuth(
        _settings(tmp_path),
        scopes={GMAIL_SCOPE},
        root=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(GoogleAuthError) as captured:
        await auth.get_access_token(PERSONAL_ACCOUNT)

    message = str(captured.value)
    assert f"ricky config google auth {PERSONAL_ACCOUNT}" in message
    assert "refresh-secret" not in message
    assert "personal-client-secret" not in message
    await auth.aclose()


def test_missing_account_client_token_and_scopes_are_actionable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.google_oauth_clients.pop(WORK_ACCOUNT)
    auth = GoogleAuth(settings, scopes={GMAIL_SCOPE}, root=tmp_path)

    with pytest.raises(
        GoogleAuthError,
        match=(
            "unknown profile-qualified Google account 'other'; accessible configured accounts: "
            "personal/personal, work/work. Use an exact account id."
        ),
    ):
        auth.validate_account("other")
    with pytest.raises(GoogleAuthError, match=r"google_oauth_clients\.work"):
        auth.oauth_client(WORK_ACCOUNT)
    with pytest.raises(GoogleAuthError, match="not authorized"):
        auth._record_for_runtime(PERSONAL_ACCOUNT)

    token_path = auth.token_store_paths[PERSONAL_ACCOUNT]
    token_path.parent.mkdir(parents=True)
    token_path.write_text(
        json.dumps(
            {
                "accounts": {
                    "personal": {
                        "email": "alex.personal@example.com",
                        "oauth_client_id": "personal-client",
                        "refresh_token": "refresh-secret",
                        "granted_scopes": ["openid", "email"],
                        "obtained_at": "2026-07-19T12:00:00Z",
                    }
                }
            }
        )
    )
    with pytest.raises(GoogleAuthError, match="missing required scopes"):
        auth._record_for_runtime(PERSONAL_ACCOUNT)


def test_secret_wrappers_do_not_reveal_refresh_token() -> None:
    record = GoogleTokenRecord(
        email="a@example.com",
        oauth_client_id="personal-client",
        refresh_token=SecretStr("refresh-secret"),
        granted_scopes=[GMAIL_SCOPE],
        obtained_at=datetime(2026, 7, 19, 12, tzinfo=UTC),
    )

    assert "refresh-secret" not in repr(record)
    assert "refresh-secret" not in str(record)


async def test_loopback_survives_stray_requests_and_times_out_cleanly() -> None:
    callback = await _LoopbackCallback.start(expected_state="expected")
    # A state-mismatched request (favicon probe, scanner, stale tab) must not
    # poison the flow; the listener keeps waiting for the real redirect.
    await _send_callback(callback.redirect_uri, state="wrong")
    await _send_callback(callback.redirect_uri, state="expected", code="late-code")

    assert await callback.receive(timeout_seconds=1) == "late-code"
    await callback.aclose()

    timed_out = await _LoopbackCallback.start(expected_state="expected")
    with pytest.raises(GoogleAuthError, match="timed out"):
        await timed_out.receive(timeout_seconds=0.001)
    port = urlsplit(timed_out.redirect_uri).port
    await timed_out.aclose()
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)


async def test_authorize_cancellation_closes_loopback_listener(tmp_path: Path) -> None:
    ready = asyncio.Event()
    authorization_url = ""

    def on_url(url: str) -> None:
        nonlocal authorization_url
        authorization_url = url
        ready.set()

    auth = GoogleAuth(
        _settings(tmp_path, callback_timeout=30),
        scopes={GMAIL_SCOPE},
        root=tmp_path,
        browser_opener=lambda _url: True,
    )
    task = asyncio.create_task(auth.authorize(PERSONAL_ACCOUNT, on_authorization_url=on_url))
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    redirect_uri = parse_qs(urlsplit(authorization_url).query)["redirect_uri"][0]
    port = urlsplit(redirect_uri).port
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)
    await auth.aclose()


async def test_authorize_headless_skips_browser_and_uses_fixed_callback_port(
    tmp_path: Path,
    unused_tcp_port: int,
) -> None:
    opened_urls: list[str] = []
    callback_task: asyncio.Task[None] | None = None
    authorization_url = ""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "headless-access",
                    "refresh_token": "headless-refresh",
                    "expires_in": 3600,
                    "scope": f"openid email {GMAIL_SCOPE}",
                },
            )
        return httpx.Response(
            200,
            json={"email": "alex.personal@example.com"},
        )

    def on_url(url: str) -> None:
        nonlocal authorization_url, callback_task
        authorization_url = url
        query = parse_qs(urlsplit(url).query)
        callback_task = asyncio.create_task(
            _send_callback(
                query["redirect_uri"][0],
                state=query["state"][0],
            )
        )

    auth = GoogleAuth(
        _settings(tmp_path),
        scopes={GMAIL_SCOPE},
        root=tmp_path,
        transport=httpx.MockTransport(handler),
        browser_opener=lambda url: opened_urls.append(url) or True,
    )
    status = await auth.authorize(
        PERSONAL_ACCOUNT,
        on_authorization_url=on_url,
        open_browser=False,
        callback_port=unused_tcp_port,
    )
    if callback_task is not None:
        await callback_task

    redirect_uri = parse_qs(urlsplit(authorization_url).query)["redirect_uri"][0]
    assert urlsplit(redirect_uri).port == unused_tcp_port
    assert opened_urls == []
    assert status.client_matches is True
    await auth.aclose()


def test_runtime_rejects_authorization_from_a_different_oauth_client(
    tmp_path: Path,
) -> None:
    token_path = tmp_path / "user-data" / "profiles" / "personal" / "google" / "tokens.json"
    token_path.parent.mkdir(parents=True)
    token_path.write_text(
        json.dumps(
            {
                "accounts": {
                    "personal": {
                        "email": "alex.personal@example.com",
                        "oauth_client_id": "retired-client",
                        "refresh_token": "refresh-secret",
                        "granted_scopes": [GMAIL_SCOPE, "openid", "email"],
                        "obtained_at": "2026-07-19T12:00:00Z",
                    }
                }
            }
        )
    )
    auth = GoogleAuth(
        _settings(tmp_path),
        scopes={GMAIL_SCOPE},
        root=tmp_path,
    )

    assert auth.status(PERSONAL_ACCOUNT).client_matches is False
    with pytest.raises(GoogleAuthError, match="different OAuth client"):
        auth._record_for_runtime(PERSONAL_ACCOUNT)


async def test_loopback_accepts_fixed_port_and_rejects_invalid_port(
    unused_tcp_port: int,
) -> None:
    callback = await _LoopbackCallback.start(
        expected_state="expected",
        port=unused_tcp_port,
    )
    receive_task = asyncio.create_task(callback.receive(timeout_seconds=1))
    await _send_callback(callback.redirect_uri, state="expected", code="fixed-port-code")

    assert await receive_task == "fixed-port-code"
    assert urlsplit(callback.redirect_uri).port == unused_tcp_port
    await callback.aclose()

    with pytest.raises(GoogleAuthError, match="between 1 and 65535"):
        await _LoopbackCallback.start(expected_state="expected", port=65536)


async def test_refresh_persists_rotated_refresh_token(tmp_path: Path) -> None:
    async def initial_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "initial-access",
                    "refresh_token": "refresh-secret",
                    "expires_in": 3600,
                    "scope": f"openid email {GMAIL_SCOPE}",
                },
            )
        return httpx.Response(200, json={"email": "alex.personal@example.com"})

    initial = GoogleAuth(
        _settings(tmp_path),
        scopes={GMAIL_SCOPE},
        root=tmp_path,
        transport=httpx.MockTransport(initial_handler),
        browser_opener=lambda _url: True,
    )
    await _authorize(initial)
    await initial.aclose()

    async def rotating_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "fresh-access",
                "refresh_token": "rotated-secret",
                "expires_in": 3600,
            },
        )

    auth = GoogleAuth(
        _settings(tmp_path),
        scopes={GMAIL_SCOPE},
        root=tmp_path,
        transport=httpx.MockTransport(rotating_handler),
        browser_opener=lambda _url: True,
    )
    assert await auth.get_access_token(PERSONAL_ACCOUNT) == "fresh-access"
    await auth.aclose()

    stored = json.loads(
        (tmp_path / "user-data" / "profiles" / "personal" / "google" / "tokens.json").read_text()
    )
    assert stored["accounts"]["personal"]["refresh_token"] == "rotated-secret"


async def test_partial_grant_is_stored_and_enforced_per_service(tmp_path: Path) -> None:
    calendar_scope = "https://www.googleapis.com/auth/calendar.events"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            # Granular consent: the user unchecked the Calendar scopes.
            return httpx.Response(
                200,
                json={
                    "access_token": "partial-access",
                    "refresh_token": "refresh-secret",
                    "expires_in": 3600,
                    "scope": f"openid email {GMAIL_SCOPE}",
                },
            )
        return httpx.Response(200, json={"email": "alex.personal@example.com"})

    auth = GoogleAuth(
        _settings(tmp_path),
        scopes={GMAIL_SCOPE, calendar_scope},
        root=tmp_path,
        transport=httpx.MockTransport(handler),
        browser_opener=lambda _url: True,
    )
    status, _ = await _authorize(auth)

    assert status.token_present is True
    assert status.missing_scopes == [calendar_scope]
    assert await auth.get_access_token(PERSONAL_ACCOUNT, required_scopes={GMAIL_SCOPE})
    with pytest.raises(GoogleAuthError, match="missing required scopes"):
        await auth.get_access_token(
            PERSONAL_ACCOUNT,
            force_refresh=True,
            required_scopes={calendar_scope},
        )
    await auth.aclose()


async def test_grant_with_no_service_scope_is_refused(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "identity-only",
                    "refresh_token": "refresh-secret",
                    "expires_in": 3600,
                    "scope": "openid email",
                },
            )
        return httpx.Response(200, json={"email": "alex.personal@example.com"})

    auth = GoogleAuth(
        _settings(tmp_path),
        scopes={GMAIL_SCOPE},
        root=tmp_path,
        transport=httpx.MockTransport(handler),
        browser_opener=lambda _url: True,
    )
    with pytest.raises(GoogleAuthError, match="any requested service scopes"):
        await _authorize(auth)
    assert not (
        tmp_path / "user-data" / "profiles" / "personal" / "google" / "tokens.json"
    ).exists()
    await auth.aclose()
