"""Offline tests for Google Calendar REST client behavior."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from typing import Any, cast

import httpx
import pytest

from ricky.tools.integrations.gcal.client import (
    GcalApiError,
    GcalClient,
    GcalError,
    GcalMutationUnknownError,
    GcalTransportError,
)


class FakeAuth:
    def __init__(self) -> None:
        self.accounts = {"personal", "work"}
        self.calls: list[tuple[str, bool]] = []

    def validate_account(self, account: str) -> object:
        if account not in self.accounts:
            raise GcalError(f"unknown account {account}")
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


def _client(handler: Handler, auth: FakeAuth | None = None) -> tuple[GcalClient, FakeAuth]:
    resolved_auth = auth or FakeAuth()
    return (
        GcalClient(
            auth=resolved_auth,
            base_url="https://calendar.test/calendar/v3",
            timeout_seconds=5,
            transport=httpx.MockTransport(cast(Any, handler)),
        ),
        resolved_auth,
    )


async def test_bearer_header_and_calendar_base_path_are_correct() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"items": []})

    client, auth = _client(handler)

    await client.call("work", "GET", "users/me/calendarList")

    assert requests[0].url.path == "/calendar/v3/users/me/calendarList"
    assert requests[0].headers["Authorization"] == "Bearer initial-work-token"
    assert auth.calls == [("work", False)]
    await client.aclose()


async def test_401_for_mutation_refreshes_and_retries_once() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                401,
                json={"error": {"errors": [{"reason": "authError"}]}},
            )
        return httpx.Response(
            200,
            json={
                "id": "event-1",
                "start": {"dateTime": "2026-07-20T10:00:00-05:00"},
                "end": {"dateTime": "2026-07-20T10:30:00-05:00"},
            },
        )

    client, auth = _client(handler)

    result = await client.call(
        "work",
        "POST",
        "calendars/primary/events",
        json_body={"summary": "test"},
        read_only=False,
        mutation_action="create event",
    )

    assert result["id"] == "event-1"
    assert len(requests) == 2
    assert auth.calls == [("work", False), ("work", True)]
    await client.aclose()


async def test_second_401_has_reauth_hint() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"errors": [{"reason": "authError"}]}},
        )

    client, _ = _client(handler)

    with pytest.raises(GcalApiError, match="ricky config google auth personal"):
        await client.call("personal", "GET", "calendars/primary")
    await client.aclose()


@pytest.mark.parametrize("status", [429, 503])
async def test_reads_retry_rate_limits_and_server_errors(
    status: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(
                status,
                headers={"Retry-After": "0.25"},
                json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}},
            )
        return httpx.Response(200, json={"items": []})

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("ricky.tools.integrations.google.client.asyncio.sleep", fake_sleep)
    client, _ = _client(handler)

    await client.call("work", "GET", "calendars/primary/events")

    assert calls == 3
    assert sleeps == [0.25, 0.25]
    await client.aclose()


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
async def test_mutations_never_retry_server_errors(method: str) -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(503, json={"error": {"status": "UNAVAILABLE"}})

    client, _ = _client(handler)

    with pytest.raises(GcalMutationUnknownError, match="Check Google Calendar"):
        await client.call(
            "work",
            method,
            "calendars/primary/events/event-1",
            read_only=False,
            mutation_action="change event",
        )

    assert requests == 1
    await client.aclose()


@pytest.mark.parametrize("mode", ["timeout", "non_json_502"])
async def test_mutation_transport_failures_have_unknown_status(mode: str) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if mode == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(502, text="bad gateway")

    client, _ = _client(handler)

    with pytest.raises(GcalMutationUnknownError):
        await client.call(
            "personal",
            "PATCH",
            "calendars/primary/events/event-1",
            read_only=False,
            mutation_action="respond to event",
        )
    assert requests == 1
    await client.aclose()


@pytest.mark.parametrize("mode", ["timeout", "non_json_502"])
async def test_read_transport_failures_are_not_mutation_unknown(mode: str) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if mode == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(502, text="bad gateway")

    client, _ = _client(handler)

    with pytest.raises(GcalTransportError):
        await client.call("personal", "GET", "calendars/primary")
    assert requests == (1 if mode == "timeout" else 3)
    await client.aclose()


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        (404, "notFound", "stale"),
        (403, "insufficientPermissions", "ricky config google auth work"),
        (403, "forbiddenForNonOrganizer", "gcal_respond_to_event"),
        (429, "rateLimitExceeded", "rate limit"),
    ],
)
async def test_structured_errors_include_calendar_specific_hints(
    status: int,
    reason: str,
    expected: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={"error": {"message": "failed", "errors": [{"reason": reason}]}},
        )

    client, _ = _client(handler)

    with pytest.raises(GcalApiError) as captured:
        await client.call("work", "GET", "calendars/primary/events/stale")
    assert expected in str(captured.value)
    await client.aclose()


async def test_pagination_reports_hard_cap() -> None:
    tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("pageToken", "")
        tokens.append(token)
        page = len(tokens)
        return httpx.Response(
            200,
            json={
                "items": [{"id": f"event-{page}"}],
                "nextPageToken": f"page-{page}",
            },
        )

    client, _ = _client(handler)
    items, truncated = await client.call_paginated(
        "work",
        "calendars/primary/events",
        params={"maxResults": 1},
        items_key="items",
        page_cap=3,
    )

    assert [item["id"] for item in items] == ["event-1", "event-2", "event-3"]
    assert tokens == ["", "page-1", "page-2"]
    assert truncated
    await client.aclose()


async def test_timezone_is_cached_once_per_account() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, json={"value": "America/Chicago"})

    client, _ = _client(handler)

    assert await client.timezone("personal") == "America/Chicago"
    assert await client.timezone("personal") == "America/Chicago"
    assert requests == ["/calendar/v3/users/me/settings/timezone"]
    await client.aclose()


async def test_delete_accepts_empty_204_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    client, _ = _client(handler)
    assert (
        await client.call(
            "work",
            "DELETE",
            "calendars/primary/events/event-1",
            read_only=False,
            mutation_action="delete event",
        )
        == {}
    )
    await client.aclose()


async def test_invalid_base_url_names_setting() -> None:
    client = GcalClient(
        auth=FakeAuth(),
        base_url="https://calendar.test:bad/calendar/v3",
        timeout_seconds=5,
    )

    with pytest.raises(GcalError, match="gcal.api_base_url"):
        await client.call("personal", "GET", "calendars/primary")
