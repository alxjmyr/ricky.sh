"""Shared OAuth infrastructure for Google service integrations.

Refresh tokens are persisted in their owning profile data directory. The
store and every in-memory credential wrapper keep secret values out of reprs,
errors, tool results, and events. Store read-modify-write cycles are
serialized across processes with an flock'd sidecar lock file (POSIX-only,
matching the supported platforms).
"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import json
import os
import secrets
import tempfile
import webbrowser
from collections.abc import Callable, Collection
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from pydantic import BaseModel, Field, SecretStr, ValidationError

from ricky.config import (
    GoogleAccountSettings,
    GoogleOAuthClientSettings,
    RickySettings,
    profile_data_path,
)
from ricky.profiles import ProfileResourceRef

IDENTITY_SCOPES = frozenset({"openid", "email"})
_REFRESH_SKEW_SECONDS = 60.0
_REFRESH_RETRY_LIMIT = 2


class GoogleAuthError(Exception):
    """Safe-to-display Google OAuth or token-store failure."""


class GoogleTokenRecord(BaseModel):
    """One persisted account authorization."""

    email: str
    oauth_client_id: str | None = None
    refresh_token: SecretStr = Field(repr=False)
    granted_scopes: list[str]
    obtained_at: datetime


class GoogleAuthStatus(BaseModel):
    """Redacted configuration/token status for one account."""

    account: str
    expected_email: str
    client_configured: bool
    token_present: bool
    client_matches: bool | None = None
    stored_email: str | None = None
    granted_scopes: list[str] = Field(default_factory=list)
    obtained_at: datetime | None = None
    missing_scopes: list[str] = Field(default_factory=list)


class _TokenStore(BaseModel):
    accounts: dict[str, GoogleTokenRecord] = Field(default_factory=dict)


class _AccessToken(BaseModel):
    value: SecretStr = Field(repr=False)
    expires_at: datetime


class GoogleAuth:
    """Multi-account OAuth consent, refresh, and token-store owner."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        scopes: Collection[str],
        root: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        browser_opener: Callable[[str], bool] | None = None,
    ) -> None:
        self._settings = settings
        self._scopes = frozenset(scopes)
        # ``root`` remains accepted for constructor compatibility. Token storage
        # is identity-level state and no longer depends on a project checkout.
        del root
        self._transport = transport
        self._browser_opener = browser_opener or webbrowser.open
        self._client: httpx.AsyncClient | None = None
        self._access_tokens: dict[str, _AccessToken] = {}
        self._refresh_locks: dict[str, asyncio.Lock] = {}

    @property
    def required_scopes(self) -> frozenset[str]:
        """Service scopes this auth instance requires (identity scopes excluded)."""
        return self._scopes

    @property
    def token_store_path(self) -> Path:
        """Resolved token-store path when exactly one account is configured."""

        if len(self.account_names) == 1:
            return self._store_path_for(self.account_names[0])
        raise GoogleAuthError(
            "multiple Google accounts use distinct profile token stores; use token_store_paths"
        )

    @property
    def token_store_paths(self) -> dict[str, Path]:
        """Return each accessible account's profile-owned token store."""

        return {account: self._store_path_for(account) for account in self.account_names}

    @property
    def account_names(self) -> tuple[str, ...]:
        """Configured account names in deterministic order."""
        return tuple(sorted(self._settings.google.accounts))

    def has_configured_client(self, account: str) -> bool:
        """Whether one configured account has matching OAuth credentials."""
        return (
            account in self._settings.google.accounts
            and account in self._settings.google_oauth_clients
        )

    def has_any_configured_client(self) -> bool:
        """Whether at least one account can perform OAuth."""
        return any(self.has_configured_client(name) for name in self.account_names)

    def validate_account(self, account: str) -> GoogleAccountSettings:
        """Return one configured identity or raise an actionable error."""
        configured = self._settings.google.accounts.get(account)
        if configured is None:
            valid = ", ".join(self.account_names) or "[none configured]"
            raise GoogleAuthError(
                f"unknown profile-qualified Google account {account!r}; "
                f"accessible configured accounts: {valid}. Use an exact account id."
            )
        return configured

    def oauth_client(self, account: str) -> GoogleOAuthClientSettings:
        """Return matching per-account credentials or raise safely."""
        self.validate_account(account)
        client = self._settings.google_oauth_clients.get(account)
        if client is None:
            resource = self._resource_ref(account)
            raise GoogleAuthError(
                f"Google OAuth client credentials are not configured for account "
                f"{account!r}; add [google_oauth_clients.{resource.name}] to "
                f"<user_data_dir>/profiles/{resource.profile}/.secrets.toml"
            )
        return client

    def status(self, account: str, *, store: _TokenStore | None = None) -> GoogleAuthStatus:
        """Read redacted config/token status without refreshing or network I/O."""
        identity = self.validate_account(account)
        store = store if store is not None else self._read_store(account)
        record = store.accounts.get(self._local_account_name(account))
        configured_client = self._settings.google_oauth_clients.get(account)
        client_matches = (
            record.oauth_client_id == configured_client.client_id
            if record is not None and configured_client is not None
            else None
        )
        missing = (
            sorted(self._scopes.difference(record.granted_scopes))
            if record is not None
            else sorted(self._scopes)
        )
        return GoogleAuthStatus(
            account=account,
            expected_email=identity.email,
            client_configured=account in self._settings.google_oauth_clients,
            token_present=record is not None,
            client_matches=client_matches,
            stored_email=record.email if record is not None else None,
            granted_scopes=sorted(record.granted_scopes) if record is not None else [],
            obtained_at=record.obtained_at if record is not None else None,
            missing_scopes=missing,
        )

    def statuses(self) -> list[GoogleAuthStatus]:
        """Return redacted status for every accessible configured account."""

        return [self.status(account) for account in self.account_names]

    async def authorize(
        self,
        account: str,
        *,
        on_authorization_url: Callable[[str], None] | None = None,
        open_browser: bool = True,
        callback_port: int = 0,
    ) -> GoogleAuthStatus:
        """Run PKCE consent, verify identity, and persist the refresh token."""
        identity = self.validate_account(account)
        client = self.oauth_client(account)
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(32)
        callback = await _LoopbackCallback.start(
            expected_state=state,
            port=callback_port,
        )

        try:
            requested_scopes = self._scopes | IDENTITY_SCOPES
            authorization_url = self._authorization_url(
                identity=identity,
                client=client,
                redirect_uri=callback.redirect_uri,
                state=state,
                code_challenge=challenge,
                scopes=requested_scopes,
            )
            if on_authorization_url is not None:
                on_authorization_url(authorization_url)
            if open_browser:
                await asyncio.to_thread(self._browser_opener, authorization_url)
            code = await callback.receive(
                timeout_seconds=self._settings.google.auth_callback_timeout_seconds
            )
        finally:
            await callback.aclose()

        token_payload = await self._exchange_code(
            account=account,
            client=client,
            code=code,
            code_verifier=verifier,
            redirect_uri=callback.redirect_uri,
        )
        raw_access_token = _required_string(token_payload, "access_token", "token exchange")
        raw_refresh_token = _required_string(token_payload, "refresh_token", "token exchange")
        granted_scopes = _scope_set(token_payload.get("scope")) or requested_scopes
        # Granular consent may grant a subset of the requested service scopes.
        # A partial grant is stored as-is (per-service runtime checks enforce
        # the rest); a grant with no usable service scope is refused.
        if not self._scopes.intersection(granted_scopes):
            raise GoogleAuthError(
                f"Google did not grant any requested service scopes for account "
                f"{account!r}; run ricky config google auth {account} and keep at "
                "least one service enabled on the consent screen"
            )

        actual_email = await self._userinfo_email(raw_access_token)
        if actual_email.casefold() != identity.email.casefold():
            raise GoogleAuthError(
                f"Google account identity mismatch for {account!r}: expected "
                f"{identity.email}, but the browser authorized {actual_email}; "
                "no token was stored"
            )

        obtained_at = datetime.now(UTC)
        record = GoogleTokenRecord(
            email=actual_email,
            oauth_client_id=client.client_id,
            refresh_token=SecretStr(raw_refresh_token),
            granted_scopes=sorted(granted_scopes),
            obtained_at=obtained_at,
        )
        local_name = self._local_account_name(account)

        def _store_record(store: _TokenStore) -> None:
            store.accounts[local_name] = record

        await asyncio.to_thread(self._locked_store_mutation, account, _store_record)

        expires_in = _expires_in(token_payload)
        self._access_tokens[account] = _AccessToken(
            value=SecretStr(raw_access_token),
            expires_at=obtained_at + timedelta(seconds=expires_in),
        )
        return await asyncio.to_thread(self.status, account)

    async def get_access_token(
        self,
        account: str,
        *,
        force_refresh: bool = False,
        required_scopes: Collection[str] | None = None,
    ) -> str:
        """Return a valid access token, single-flighting refreshes per account.

        ``required_scopes`` narrows the stored-grant check to the calling
        service's scopes; without it the instance-wide scope set applies.
        """
        self.validate_account(account)
        self.oauth_client(account)
        observed = self._access_tokens.get(account)
        if not force_refresh and observed is not None and _token_is_fresh(observed):
            return _secret_value(observed.value)

        lock = self._refresh_locks.setdefault(account, asyncio.Lock())
        async with lock:
            current = self._access_tokens.get(account)
            if current is not None and _token_is_fresh(current):
                # Another coroutine may have refreshed while we waited on the
                # lock; a force_refresh caller still needs a token newer than
                # the one that was just rejected.
                replaced_while_waiting = observed is not None and current is not observed
                if not force_refresh or replaced_while_waiting:
                    return _secret_value(current.value)

            record = await asyncio.to_thread(self._record_for_runtime, account, required_scopes)
            token = await self._refresh(account, record)
            self._access_tokens[account] = token
            return _secret_value(token.value)

    async def aclose(self) -> None:
        """Close the shared OAuth HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _record_for_runtime(
        self,
        account: str,
        required_scopes: Collection[str] | None = None,
    ) -> GoogleTokenRecord:
        store = self._read_store(account)
        record = store.accounts.get(self._local_account_name(account))
        if record is None:
            raise GoogleAuthError(
                f"Google account {account!r} is not authorized; run "
                f"ricky config google auth {account}"
            )
        client = self.oauth_client(account)
        if record.oauth_client_id != client.client_id:
            raise GoogleAuthError(
                f"stored Google authorization for account {account!r} belongs to a "
                "different OAuth client; run "
                f"ricky config google auth {account}"
            )
        if record.email.casefold() != self.validate_account(account).email.casefold():
            raise GoogleAuthError(
                f"stored Google identity for {account!r} is {record.email}, not "
                f"{self.validate_account(account).email}; run "
                f"ricky config google auth {account}"
            )
        needed = frozenset(required_scopes) if required_scopes is not None else self._scopes
        missing = needed.difference(record.granted_scopes)
        if missing:
            raise GoogleAuthError(
                f"Google account {account!r} is missing required scopes "
                f"({', '.join(sorted(missing))}); run ricky config google auth {account}"
            )
        return record

    async def _refresh(self, account: str, record: GoogleTokenRecord) -> _AccessToken:
        client = self.oauth_client(account)
        data = {
            "client_id": client.client_id,
            "client_secret": _secret_value(client.client_secret),
            "refresh_token": _secret_value(record.refresh_token),
            "grant_type": "refresh_token",
        }
        attempt = 0
        while True:
            final_attempt = attempt >= _REFRESH_RETRY_LIMIT
            try:
                response = await self._http().post(self._settings.google.token_url, data=data)
            except httpx.HTTPError as exc:
                if final_attempt:
                    raise GoogleAuthError(
                        f"Google token refresh failed for account {account!r}: network error"
                    ) from exc
                attempt += 1
                continue
            if response.status_code >= 500 and not final_attempt:
                attempt += 1
                continue
            break

        payload = _safe_json(response)
        if response.status_code >= 400:
            code = _oauth_error_code(payload)
            if code == "invalid_grant":
                raise GoogleAuthError(
                    f"Google authorization for account {account!r} expired or was revoked; "
                    f"run ricky config google auth {account}"
                )
            detail = f": {code}" if code else ""
            raise GoogleAuthError(
                f"Google token refresh failed for account {account!r} "
                f"(HTTP {response.status_code}{detail})"
            )
        raw_token = _required_string(payload, "access_token", "token refresh")
        rotated = payload.get("refresh_token")
        if isinstance(rotated, str) and rotated and rotated != _secret_value(record.refresh_token):
            # Google rotated the refresh token; persist it or the next refresh
            # after the old token is invalidated fails with invalid_grant.
            await asyncio.to_thread(self._persist_rotated_refresh_token, account, rotated)
        return _AccessToken(
            value=SecretStr(raw_token),
            expires_at=datetime.now(UTC) + timedelta(seconds=_expires_in(payload)),
        )

    async def _exchange_code(
        self,
        *,
        account: str,
        client: GoogleOAuthClientSettings,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        try:
            response = await self._http().post(
                self._settings.google.token_url,
                data={
                    "client_id": client.client_id,
                    "client_secret": _secret_value(client.client_secret),
                    "code": code,
                    "code_verifier": code_verifier,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
        except httpx.HTTPError as exc:
            raise GoogleAuthError(
                f"Google token exchange failed for account {account!r}: network error"
            ) from exc
        payload = _safe_json(response)
        if response.status_code >= 400:
            code_name = _oauth_error_code(payload)
            detail = f": {code_name}" if code_name else ""
            raise GoogleAuthError(
                f"Google token exchange failed for account {account!r} "
                f"(HTTP {response.status_code}{detail})"
            )
        return payload

    async def _userinfo_email(self, access_token: str) -> str:
        try:
            response = await self._http().get(
                self._settings.google.userinfo_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise GoogleAuthError("Google identity verification failed: network error") from exc
        payload = _safe_json(response)
        if response.status_code >= 400:
            raise GoogleAuthError(
                f"Google identity verification failed (HTTP {response.status_code})"
            )
        return _required_string(payload, "email", "identity verification")

    def _authorization_url(
        self,
        *,
        identity: GoogleAccountSettings,
        client: GoogleOAuthClientSettings,
        redirect_uri: str,
        state: str,
        code_challenge: str,
        scopes: Collection[str],
    ) -> str:
        query = urlencode(
            {
                "client_id": client.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(sorted(scopes)),
                "access_type": "offline",
                "prompt": "consent",
                "login_hint": identity.email,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self._settings.google.auth_base_url}?{query}"

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            try:
                self._client = httpx.AsyncClient(
                    timeout=self._settings.request_timeout_seconds,
                    transport=self._transport,
                )
            except httpx.InvalidURL as exc:
                raise GoogleAuthError(f"invalid Google OAuth URL setting: {exc}") from exc
        return self._client

    def _persist_rotated_refresh_token(self, account: str, rotated: str) -> None:
        local_name = self._local_account_name(account)

        def mutate(store: _TokenStore) -> None:
            current = store.accounts.get(local_name)
            if current is not None:
                store.accounts[local_name] = current.model_copy(
                    update={"refresh_token": SecretStr(rotated)}
                )

        self._locked_store_mutation(account, mutate)

    def _locked_store_mutation(
        self,
        account: str,
        mutate: Callable[[_TokenStore], None],
    ) -> None:
        """Serialize store read-modify-write cycles across processes."""
        store_path = self._store_path_for(account)
        parent = store_path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = parent / f"{store_path.name}.lock"
        with open(lock_path, "a", encoding="utf-8") as lock_handle:
            os.fchmod(lock_handle.fileno(), 0o600)
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            store = self._read_store(account)
            mutate(store)
            self._write_store(store, store_path)

    def _read_store(self, account: str) -> _TokenStore:
        store_path = self._store_path_for(account)
        if not store_path.exists():
            return _TokenStore()
        try:
            raw = json.loads(store_path.read_text(encoding="utf-8"))
            return _TokenStore.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise GoogleAuthError(
                f"Google token store for {account!r} is unreadable or invalid; "
                "re-run ricky config google auth for each account"
            ) from exc

    def _write_store(self, store: _TokenStore, store_path: Path) -> None:
        parent = store_path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "accounts": {
                account: {
                    "email": record.email,
                    "oauth_client_id": record.oauth_client_id,
                    "refresh_token": _secret_value(record.refresh_token),
                    "granted_scopes": record.granted_scopes,
                    "obtained_at": record.obtained_at.isoformat(),
                }
                for account, record in sorted(store.accounts.items())
            }
        }
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".tokens.", suffix=".tmp", dir=parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(file_descriptor, 0o600)
            handle = os.fdopen(file_descriptor, "w", encoding="utf-8")
        except BaseException:
            with suppress(OSError):
                os.close(file_descriptor)
            with suppress(OSError):
                temporary.unlink()
            raise
        # From here the handle owns the descriptor; never close it twice.
        try:
            with handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, store_path)
            os.chmod(store_path, 0o600)
        except BaseException:
            with suppress(OSError):
                temporary.unlink()
            raise

    def _store_path_for(self, account: str) -> Path:
        resource = self._resource_ref(account)
        return (
            profile_data_path(self._settings, resource.profile)
            / self._settings.google.token_store_path
        )

    @staticmethod
    def _resource_ref(account: str) -> ProfileResourceRef:
        profile, separator, local_name = account.partition("/")
        if not separator:
            raise GoogleAuthError(f"Google account {account!r} is not profile-qualified")
        return ProfileResourceRef(profile=profile, name=local_name)

    @classmethod
    def _local_account_name(cls, account: str) -> str:
        return cls._resource_ref(account).name


class _LoopbackCallback:
    """OAuth loopback receiver that tolerates stray connections.

    Only a request carrying the expected ``state`` resolves the pending
    future; preconnects, probes, and unrelated requests get a 400 (or are
    dropped) while the listener keeps waiting for the real redirect.
    """

    def __init__(
        self,
        *,
        server: asyncio.AbstractServer,
        future: asyncio.Future[str],
        port: int,
    ) -> None:
        self._server = server
        self._future = future
        self.redirect_uri = f"http://127.0.0.1:{port}"

    @classmethod
    async def start(cls, *, expected_state: str, port: int = 0) -> _LoopbackCallback:
        if not 0 <= port <= 65535:
            raise GoogleAuthError("Google OAuth callback port must be between 1 and 65535")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
            except Exception:
                # Speculative preconnects, port scans, and aborted connections
                # are not the OAuth redirect; drop them and keep listening.
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()
                return

            status = "200 OK"
            message = "Authorization complete. You can close this window."
            request_line = header.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
            parts = request_line.split()
            query = (
                parse_qs(urlsplit(parts[1]).query)
                if len(parts) >= 2 and parts[0] == "GET"
                else None
            )
            returned_state = (query.get("state") or [""])[0] if query is not None else ""
            if query is None or not secrets.compare_digest(returned_state, expected_state):
                # Not our callback (favicon probe, scanner, stale tab): refuse
                # this request but keep waiting for the real redirect.
                status = "400 Bad Request"
                message = "Not an expected OAuth callback."
            else:
                oauth_error = (query.get("error") or [""])[0]
                code = (query.get("code") or [""])[0]
                if oauth_error:
                    status = "400 Bad Request"
                    message = "Authorization failed. Return to the terminal for details."
                    if not future.done():
                        future.set_exception(
                            GoogleAuthError(f"Google authorization failed: {oauth_error}")
                        )
                elif not code:
                    status = "400 Bad Request"
                    message = "Authorization failed. Return to the terminal for details."
                    if not future.done():
                        future.set_exception(
                            GoogleAuthError("Google authorization callback did not include a code")
                        )
                elif not future.done():
                    future.set_result(code)

            body = (
                "<!doctype html><meta charset=utf-8><title>Ricky Google auth</title>"
                f"<p>{message}</p>"
            ).encode()
            writer.write(
                (
                    f"HTTP/1.1 {status}\r\n"
                    "Content-Type: text/html; charset=utf-8\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
                + body
            )
            with suppress(ConnectionError):
                await writer.drain()
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

        try:
            server = await asyncio.start_server(handle, "127.0.0.1", port)
        except OSError as exc:
            requested = f" port {port}" if port else " an available port"
            raise GoogleAuthError(
                f"could not start the Google OAuth loopback listener on{requested}"
            ) from exc
        sockets = server.sockets or []
        if not sockets:
            server.close()
            await server.wait_closed()
            raise GoogleAuthError("could not start the Google OAuth loopback listener")
        port = int(sockets[0].getsockname()[1])
        return cls(server=server, future=future, port=port)

    async def receive(self, *, timeout_seconds: float) -> str:
        try:
            return await asyncio.wait_for(self._future, timeout=timeout_seconds)
        except TimeoutError as exc:
            raise GoogleAuthError(
                "Google authorization timed out; run the auth command again"
            ) from exc

    async def aclose(self) -> None:
        self._server.close()
        await self._server.wait_closed()
        if not self._future.done():
            self._future.cancel()


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _secret_value(value: SecretStr) -> str:
    """The sole unwrap point for secrets entering storage or OAuth requests."""
    return value.get_secret_value()


def _token_is_fresh(token: _AccessToken | None) -> bool:
    if token is None:
        return False
    return token.expires_at > datetime.now(UTC) + timedelta(seconds=_REFRESH_SKEW_SECONDS)


def _expires_in(payload: dict[str, Any]) -> float:
    raw = payload.get("expires_in", 3600)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 3600.0


def _scope_set(raw: object) -> frozenset[str]:
    if isinstance(raw, str):
        return frozenset(raw.split())
    if isinstance(raw, list):
        return frozenset(str(value) for value in raw)
    return frozenset()


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise GoogleAuthError(
            f"Google OAuth endpoint returned non-JSON (HTTP {response.status_code})"
        ) from exc
    if not isinstance(payload, dict):
        raise GoogleAuthError("Google OAuth endpoint returned an invalid JSON payload")
    return payload


def _oauth_error_code(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        return str(error.get("status") or error.get("message") or "")
    return ""


def _required_string(payload: dict[str, Any], key: str, operation: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise GoogleAuthError(f"Google {operation} response did not include {key}")
    return value


__all__ = [
    "GoogleAuth",
    "GoogleAuthError",
    "GoogleAuthStatus",
    "GoogleTokenRecord",
    "IDENTITY_SCOPES",
]
