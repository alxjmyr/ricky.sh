"""Offline tests for Gmail REST client behavior."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from typing import Any, cast

import httpx
import pytest

from ricky.tools.integrations.gmail.client import (
    GmailApiError,
    GmailClient,
    GmailError,
    GmailMutationUnknownError,
    GmailTransportError,
)


class FakeAuth:
    def __init__(self) -> None:
        self.accounts = {"personal", "work"}
        self.calls: list[tuple[str, bool]] = []

    def validate_account(self, account: str) -> object:
        if account not in self.accounts:
            raise GmailError(f"unknown account {account}")
        return object()

    async def get_access_token(
        self,
        account: str,
        *,
        force_refresh: bool = False,
        required_scopes: Collection[str] | None = None,
    ) -> str:
        self.calls.append((account, force_refresh))
        prefix = "refreshed" if force_refresh else "initial"
        return f"{prefix}-{account}-token"


Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


def _client(handler: Handler, auth: FakeAuth | None = None) -> tuple[GmailClient, FakeAuth]:
    resolved_auth = auth or FakeAuth()
    return (
        GmailClient(
            auth=resolved_auth,
            base_url="https://gmail.test",
            timeout_seconds=5,
            transport=httpx.MockTransport(cast(Any, handler)),
        ),
        resolved_auth,
    )


async def test_bearer_header_is_account_correct() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"emailAddress": "alex@example.com"})

    client, auth = _client(handler)

    await client.call("work", "GET", "profile")

    assert requests[0].headers["Authorization"] == "Bearer initial-work-token"
    assert auth.calls == [("work", False)]
    await client.aclose()


async def test_401_for_send_forces_refresh_once_and_retries_exactly_once() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                401,
                json={
                    "error": {
                        "code": 401,
                        "status": "UNAUTHENTICATED",
                        "errors": [{"reason": "authError"}],
                    }
                },
            )
        return httpx.Response(200, json={"id": "sent-1", "threadId": "thread-1"})

    client, auth = _client(handler)

    payload = await client.call(
        "personal",
        "POST",
        "messages/send",
        json_body={"raw": "encoded"},
        read_only=False,
        mutation_action="send message",
        mutation_check="the Sent folder",
    )

    assert payload["id"] == "sent-1"
    assert len(requests) == 2
    assert requests[0].headers["Authorization"] == "Bearer initial-personal-token"
    assert requests[1].headers["Authorization"] == "Bearer refreshed-personal-token"
    assert auth.calls == [("personal", False), ("personal", True)]
    await client.aclose()


async def test_second_401_surfaces_actionable_api_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": {
                    "status": "UNAUTHENTICATED",
                    "errors": [{"reason": "authError"}],
                }
            },
        )

    client, _ = _client(handler)

    with pytest.raises(GmailApiError) as captured:
        await client.call("work", "GET", "profile")

    assert "ricky config google auth work" in str(captured.value)
    await client.aclose()


async def test_read_retries_429_and_honors_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests < 3:
            return httpx.Response(
                429,
                headers={"Retry-After": "0.25"},
                json={
                    "error": {
                        "status": "RESOURCE_EXHAUSTED",
                        "errors": [{"reason": "rateLimitExceeded"}],
                    }
                },
            )
        return httpx.Response(200, json={"messages": []})

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("ricky.tools.integrations.google.client.asyncio.sleep", fake_sleep)
    client, _ = _client(handler)

    assert await client.call("personal", "GET", "messages") == {"messages": []}
    assert requests == 3
    assert sleeps == [0.25, 0.25]
    await client.aclose()


async def test_read_retries_403_rate_limit_reason() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(
                403,
                headers={"Retry-After": "0"},
                json={
                    "error": {
                        "status": "PERMISSION_DENIED",
                        "errors": [{"reason": "userRateLimitExceeded"}],
                    }
                },
            )
        return httpx.Response(200, json={"messages": []})

    client, _ = _client(handler)

    await client.call("personal", "GET", "messages")

    assert requests == 2
    await client.aclose()


async def test_mutation_never_retries_5xx_and_reports_unknown_status() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(503, json={"error": {"status": "UNAVAILABLE"}})

    client, _ = _client(handler)

    with pytest.raises(GmailMutationUnknownError, match="Sent folder"):
        await client.call(
            "personal",
            "POST",
            "messages/send",
            json_body={"raw": "encoded"},
            read_only=False,
            mutation_action="send message",
            mutation_check="the Sent folder",
        )

    assert requests == 1
    await client.aclose()


@pytest.mark.parametrize("mode", ["timeout", "non_json_502"])
async def test_mutation_transport_and_gateway_failures_are_unknown(
    mode: str,
) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if mode == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(502, text="bad gateway")

    client, _ = _client(handler)

    with pytest.raises(GmailMutationUnknownError, match="Drafts"):
        await client.call(
            "work",
            "POST",
            "drafts",
            json_body={"message": {"raw": "encoded"}},
            read_only=False,
            mutation_action="create draft",
            mutation_check="Gmail Drafts",
        )

    assert requests == 1
    await client.aclose()


@pytest.mark.parametrize("mode", ["timeout", "non_json_502"])
async def test_same_transport_failures_on_reads_are_transport_errors(mode: str) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if mode == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(502, text="bad gateway")

    client, _ = _client(handler)

    with pytest.raises(GmailTransportError):
        await client.call("work", "GET", "messages")

    expected = 1 if mode == "timeout" else 3
    assert requests == expected
    await client.aclose()


async def test_structured_errors_map_reasons_and_hints() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "error": {
                    "code": 404,
                    "message": "Requested entity was not found.",
                    "status": "NOT_FOUND",
                    "errors": [{"reason": "notFound"}],
                }
            },
        )

    client, _ = _client(handler)

    with pytest.raises(GmailApiError) as captured:
        await client.call("personal", "GET", "messages/stale")

    error = captured.value
    assert error.status_code == 404
    assert error.reason == "notFound"
    assert "stale" in str(error)
    await client.aclose()


async def test_pagination_follows_tokens_and_reports_cap() -> None:
    tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("pageToken", "")
        tokens.append(token)
        page = len(tokens)
        return httpx.Response(
            200,
            json={
                "messages": [{"id": f"m{page}"}],
                "nextPageToken": f"page-{page}",
            },
        )

    client, _ = _client(handler)

    items, truncated = await client.call_paginated(
        "personal",
        "messages",
        params={"maxResults": 1},
        items_key="messages",
        page_cap=3,
    )

    assert [item["id"] for item in items] == ["m1", "m2", "m3"]
    assert tokens == ["", "page-1", "page-2"]
    assert truncated
    await client.aclose()


async def test_label_cache_resolution_and_invalidation() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "labels": [
                    {"id": "INBOX", "name": "INBOX", "type": "system"},
                    {"id": "Label_1", "name": "Q3 Planning", "type": "user"},
                ]
            },
        )

    client, _ = _client(handler)

    assert (await client.resolve_label("work", "q3 planning")).id == "Label_1"
    assert (await client.resolve_label("work", "Label_1")).name == "Q3 Planning"
    assert calls == 1
    with pytest.raises(GmailError, match="close matches: Q3 Planning"):
        await client.resolve_label("work", "q3 plan")
    client.invalidate_labels("work")
    await client.labels("work")
    assert calls == 2
    await client.aclose()


async def test_invalid_base_url_names_setting() -> None:
    client = GmailClient(
        auth=FakeAuth(),
        base_url="https://gmail.test:bad",
        timeout_seconds=5,
    )

    with pytest.raises(GmailError, match="gmail.api_base_url"):
        await client.call("personal", "GET", "profile")
