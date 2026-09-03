"""Offline tests for fine-grained Google Calendar tools."""

from __future__ import annotations

import json
from collections.abc import Collection
from pathlib import Path
from typing import cast

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from ricky.agent import AgentSession
from ricky.config import (
    GcalSettings,
    GoogleAccountSettings,
    GoogleOAuthClientSettings,
    GoogleSettings,
    RickySettings,
)
from ricky.tools.base import ToolContext
from ricky.tools.integrations.gcal import GcalToolset
from ricky.tools.integrations.gcal.client import GcalClient, GcalError
from ricky.tools.integrations.gcal.tools import (
    GcalCheckAvailabilityParams,
    GcalCheckAvailabilityTool,
    GcalCreateEventParams,
    GcalCreateEventTool,
    GcalDeleteEventParams,
    GcalDeleteEventTool,
    GcalGetEventParams,
    GcalGetEventTool,
    GcalListCalendarsParams,
    GcalListCalendarsTool,
    GcalListEventsParams,
    GcalListEventsTool,
    GcalRespondToEventParams,
    GcalRespondToEventTool,
    GcalUpdateEventParams,
    GcalUpdateEventTool,
)
from ricky.tools.integrations.google import GoogleAuth


class FakeAuth:
    def validate_account(self, account: str) -> GoogleAccountSettings:
        if account not in {"personal", "work"}:
            raise GcalError(f"unknown account {account}")
        email = "alex@company.example" if account == "work" else "alex@example.com"
        return GoogleAccountSettings(email=email)

    async def get_access_token(
        self,
        account: str,
        *,
        force_refresh: bool = False,
        required_scopes: Collection[str] | None = None,
    ) -> str:
        del force_refresh
        return f"token-{account}"


def _settings() -> RickySettings:
    return RickySettings(
        gcal=GcalSettings(
            api_base_url="https://calendar.test/calendar/v3",
            default_list_limit=12,
            default_window_days=7,
            description_char_limit=2_000,
        )
    )


def _context(tmp_path: Path, settings: RickySettings | None = None) -> ToolContext:
    resolved = settings or _settings()
    return ToolContext(
        cwd=tmp_path,
        settings=resolved,
        session=AgentSession.create(resolved, profile_scope=resolved.resolve_profile_scope()),
    )


def _client(handler) -> GcalClient:
    return GcalClient(
        auth=FakeAuth(),
        base_url="https://calendar.test/calendar/v3",
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )


def _event_payload(event_id: str = "event-1") -> dict[str, object]:
    return {
        "id": event_id,
        "summary": "Ricky Calendar smoke test",
        "start": {"dateTime": "2026-07-24T10:00:00-05:00"},
        "end": {"dateTime": "2026-07-24T10:30:00-05:00"},
        "organizer": {"email": "alex@company.example"},
        "attendees": [
            {
                "email": "alex@company.example",
                "responseStatus": "accepted",
                "organizer": True,
            },
            {
                "email": "alex.personal@example.com",
                "responseStatus": "needsAction",
                "self": True,
            },
        ],
    }


async def test_list_calendars_renders_ids_and_pagination_marker(tmp_path: Path) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "work@example.com",
                        "summary": "Work",
                        "primary": True,
                        "accessRole": "owner",
                        "timeZone": "America/Chicago",
                    }
                ],
                "nextPageToken": f"page-{calls}",
            },
        )

    client = _client(handler)
    result = await GcalListCalendarsTool(client).run(
        GcalListCalendarsParams(account="work"),
        _context(tmp_path),
    )

    assert "calendar work@example.com" in result.content
    assert "role owner" in result.content
    assert "truncated" in result.content
    assert calls == 5
    await client.aclose()


async def test_list_events_uses_defaults_query_expansion_and_local_render(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/users/me/settings/timezone"):
            return httpx.Response(200, json={"value": "America/Chicago"})
        return httpx.Response(200, json={"items": [_event_payload("series_20260724")]})

    client = _client(handler)
    result = await GcalListEventsTool(client).run(
        GcalListEventsParams(
            account="work",
            time_min="2026-07-24T00:00:00-05:00",
            query="Ricky smoke",
        ),
        _context(tmp_path),
    )

    event_request = requests[1]
    assert event_request.url.path.endswith("/calendars/primary/events")
    assert event_request.url.params["singleEvents"] == "true"
    assert event_request.url.params["orderBy"] == "startTime"
    assert event_request.url.params["q"] == "Ricky smoke"
    assert event_request.url.params["maxResults"] == "12"
    assert "Fri 2026-07-24 10:00–10:30 CDT (UTC-05:00)" in result.content
    assert "event series_20260724" in result.content
    await client.aclose()


async def test_get_event_escapes_ids_and_renders_complete_event(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/users/me/settings/timezone"):
            return httpx.Response(200, json={"value": "America/Chicago"})
        return httpx.Response(200, json=_event_payload("event/with slash"))

    client = _client(handler)
    result = await GcalGetEventTool(client).run(
        GcalGetEventParams(
            account="work",
            calendar_id="team@example.com",
            event_id="event/with slash",
        ),
        _context(tmp_path),
    )

    assert "/calendars/team%40example.com/events/event%2Fwith%20slash" in str(requests[0].url)
    assert "Ricky Calendar smoke test" in result.content
    assert "alex.personal@example.com [needsAction] [self]" in result.content
    await client.aclose()


async def test_availability_posts_explicit_calendars_and_renders_free_trailer(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/users/me/settings/timezone"):
            return httpx.Response(200, json={"value": "America/Chicago"})
        return httpx.Response(
            200,
            json={
                "calendars": {
                    "primary": {
                        "busy": [
                            {
                                "start": "2026-07-23T14:00:00Z",
                                "end": "2026-07-23T15:00:00Z",
                            }
                        ]
                    },
                    "team@example.com": {"busy": []},
                }
            },
        )

    client = _client(handler)
    result = await GcalCheckAvailabilityTool(client).run(
        GcalCheckAvailabilityParams(
            account="work",
            time_min="2026-07-23T08:00:00-05:00",
            time_max="2026-07-23T17:00:00-05:00",
            calendar_ids=["primary", "team@example.com"],
        ),
        _context(tmp_path),
    )

    body = json.loads(requests[1].content)
    assert body["items"] == [{"id": "primary"}, {"id": "team@example.com"}]
    assert "busy Thu 2026-07-23 09:00" in result.content
    assert "free outside these busy blocks" in result.content
    await client.aclose()


async def test_availability_surfaces_per_calendar_errors(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/me/settings/timezone"):
            return httpx.Response(200, json={"value": "UTC"})
        return httpx.Response(
            200,
            json={"calendars": {"missing": {"errors": [{"reason": "notFound"}], "busy": []}}},
        )

    client = _client(handler)
    with pytest.raises(GcalError, match="notFound"):
        await GcalCheckAvailabilityTool(client).run(
            GcalCheckAvailabilityParams(
                account="work",
                time_min="2026-07-23T08:00:00Z",
                time_max="2026-07-23T17:00:00Z",
                calendar_ids=["missing"],
            ),
            _context(tmp_path),
        )
    await client.aclose()


async def test_create_event_validates_and_sends_notify_all(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/users/me/settings/timezone"):
            return httpx.Response(200, json={"value": "America/Chicago"})
        return httpx.Response(200, json=_event_payload())

    client = _client(handler)
    params = GcalCreateEventParams(
        account="work",
        summary="Ricky Calendar smoke test",
        start="2026-07-24T10:00:00",
        end="2026-07-24T10:30:00",
        attendees=["alex.personal@example.com"],
        description="Created by Ricky test",
        location="Video call",
    )
    result = await GcalCreateEventTool(client).run(params, _context(tmp_path))

    request = requests[1]
    body = json.loads(request.content)
    assert request.method == "POST"
    assert request.url.params["sendUpdates"] == "all"
    assert body["start"] == {
        "dateTime": "2026-07-24T10:00:00-05:00",
        "timeZone": "America/Chicago",
    }
    assert body["attendees"] == [{"email": "alex.personal@example.com"}]
    assert "Created Calendar event event-1" in result.content
    assert "attendees will be notified" in result.content
    await client.aclose()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start": "2026-07-24", "end": "2026-07-24T10:30:00", "all_day": True},
        {"start": "2026-07-24T10:30:00Z", "end": "2026-07-24T10:00:00Z"},
    ],
)
async def test_create_rejects_invalid_time_shapes(
    tmp_path: Path,
    kwargs: dict[str, object],
) -> None:
    client = _client(lambda _request: httpx.Response(500))
    base: dict[str, object] = {
        "account": "work",
        "summary": "bad",
        "timezone": "UTC",
    }
    with pytest.raises(GcalError):
        await GcalCreateEventTool(client).run(
            GcalCreateEventParams.model_validate(base | kwargs),
            _context(tmp_path),
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_mutation_effect_identity_rejects_local_errors_before_dispatch(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    client = _client(handler)
    context = _context(tmp_path)
    with pytest.raises(GcalError, match="end must be after start"):
        GcalCreateEventTool(client).effect_identity(
            {
                "account": "work",
                "summary": "bad range",
                "start": "2026-07-24T10:30:00Z",
                "end": "2026-07-24T10:00:00Z",
            },
            context,
        )
    with pytest.raises(GcalError, match="must not be blank"):
        GcalDeleteEventTool(client).effect_identity(
            {"account": "work", "calendar_id": "primary", "event_id": " "},
            context,
        )
    assert requests == []
    await client.aclose()


def test_create_params_validate_attendee_email_and_rrule() -> None:
    with pytest.raises(ValidationError, match="explicit attendee email"):
        GcalCreateEventParams(
            account="work",
            summary="bad",
            start="2026-07-24T10:00:00Z",
            end="2026-07-24T10:30:00Z",
            attendees=["not an email"],
        )
    with pytest.raises(ValidationError, match="RRULE"):
        GcalCreateEventParams(
            account="work",
            summary="bad",
            start="2026-07-24T10:00:00Z",
            end="2026-07-24T10:30:00Z",
            recurrence=["FREQ=WEEKLY"],
        )


async def test_update_patches_only_provided_fields(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_event_payload())

    client = _client(handler)
    await GcalUpdateEventTool(client).run(
        GcalUpdateEventParams(
            account="work",
            event_id="event-1",
            summary="Updated title",
            location="",
        ),
        _context(tmp_path),
    )

    assert len(requests) == 1
    assert requests[0].method == "PATCH"
    assert requests[0].url.params["sendUpdates"] == "all"
    assert json.loads(requests[0].content) == {
        "summary": "Updated title",
        "location": "",
    }
    await client.aclose()


def test_update_rejects_explicit_null_fields() -> None:
    with pytest.raises(ValidationError, match="must not be null"):
        GcalUpdateEventParams(
            account="work",
            event_id="event-1",
            start=None,
        )


async def test_update_attendees_preserves_unrelated_fields_and_removes_exactly(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    existing = _event_payload()
    existing["attendees"] = [
        {
            "email": "keep@example.com",
            "responseStatus": "accepted",
            "comment": "preserve me",
        },
        {"email": "remove@example.com", "responseStatus": "tentative"},
    ]
    existing["etag"] = '"etag-123"'

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=existing)

    client = _client(handler)
    await GcalUpdateEventTool(client).run(
        GcalUpdateEventParams(
            account="work",
            event_id="event-1",
            add_attendees=["new@example.com"],
            remove_attendees=["remove@example.com"],
        ),
        _context(tmp_path),
    )

    assert [request.method for request in requests] == ["GET", "PATCH"]
    assert requests[1].headers["If-Match"] == '"etag-123"'
    body = json.loads(requests[1].content)
    assert body["attendees"] == [
        {
            "email": "keep@example.com",
            "responseStatus": "accepted",
            "comment": "preserve me",
        },
        {"email": "new@example.com"},
    ]
    await client.aclose()


async def test_update_rejects_missing_attendee_without_patch(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_event_payload())

    client = _client(handler)
    with pytest.raises(GcalError, match="not present"):
        await GcalUpdateEventTool(client).run(
            GcalUpdateEventParams(
                account="work",
                event_id="event-1",
                remove_attendees=["absent@example.com"],
            ),
            _context(tmp_path),
        )
    assert [request.method for request in requests] == ["GET"]
    await client.aclose()


async def test_rsvp_patches_only_self_with_attendees_omitted(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_event_payload())

    client = _client(handler)
    result = await GcalRespondToEventTool(client).run(
        GcalRespondToEventParams(
            account="personal",
            event_id="event-1",
            response="accepted",
        ),
        _context(tmp_path),
    )

    assert [request.method for request in requests] == ["GET", "PATCH"]
    assert requests[1].url.params["sendUpdates"] == "all"
    body = json.loads(requests[1].content)
    assert body["attendeesOmitted"] is True
    assert len(body["attendees"]) == 1
    assert body["attendees"][0]["email"] == "alex.personal@example.com"
    assert body["attendees"][0]["responseStatus"] == "accepted"
    assert "Responded accepted" in result.content
    await client.aclose()


@pytest.mark.parametrize("field", ["comment", "note"])
async def test_rsvp_sends_comment_or_note_as_attendee_response_comment(
    tmp_path: Path,
    field: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_event_payload())

    client = _client(handler)
    params = GcalRespondToEventParams.model_validate(
        {
            "account": "personal",
            "event_id": "event-1",
            "response": "declined",
            field: "I have a conflicting appointment.",
        }
    )
    await GcalRespondToEventTool(client).run(params, _context(tmp_path))

    body = json.loads(requests[1].content)
    assert body == {
        "attendees": [
            {
                "email": "alex.personal@example.com",
                "responseStatus": "declined",
                "self": True,
                "comment": "I have a conflicting appointment.",
            }
        ],
        "attendeesOmitted": True,
    }
    await client.aclose()


def test_rsvp_comment_and_note_validation() -> None:
    common = {
        "account": "personal",
        "event_id": "event-1",
        "response": "declined",
    }
    with pytest.raises(ValidationError, match="must not be blank"):
        GcalRespondToEventParams.model_validate(common | {"comment": "  "})
    with pytest.raises(ValidationError, match="at most one"):
        GcalRespondToEventParams.model_validate(
            common | {"comment": "Conflict", "note": "Also a conflict"}
        )


async def test_rsvp_requires_self_attendee(tmp_path: Path) -> None:
    payload = _event_payload()
    payload["attendees"] = [{"email": "someone@example.com"}]
    client = _client(lambda _request: httpx.Response(200, json=payload))

    with pytest.raises(GcalError, match="not an attendee"):
        await GcalRespondToEventTool(client).run(
            GcalRespondToEventParams(
                account="personal",
                event_id="event-1",
                response="declined",
            ),
            _context(tmp_path),
        )
    await client.aclose()


async def test_delete_is_destructive_notified_and_accepts_204(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    client = _client(handler)
    tool = GcalDeleteEventTool(client)
    result = await tool.run(
        GcalDeleteEventParams(account="work", event_id="event-1"),
        _context(tmp_path),
    )

    assert tool.risk == "destructive"
    assert requests[0].method == "DELETE"
    assert requests[0].url.params["sendUpdates"] == "all"
    assert "Deleted Calendar event event-1" in result.content
    await client.aclose()


def test_permission_previews_are_complete_and_recurring_scope_aware(tmp_path: Path) -> None:
    client = _client(lambda _request: httpx.Response(500))
    ctx = _context(tmp_path)
    create = GcalCreateEventTool(client).summarize_permission(
        {
            "account": "work",
            "summary": "Smoke test",
            "start": "2026-07-24T10:00:00-05:00",
            "end": "2026-07-24T10:30:00-05:00",
            "attendees": ["alex@example.com"],
            "description": "Complete description",
        },
        ctx,
    )
    update = GcalUpdateEventTool(client).summarize_permission(
        {
            "account": "work",
            "event_id": "series_20260724T150000Z",
            "start": "2026-07-24T11:00:00-05:00",
        },
        ctx,
    )
    delete = GcalDeleteEventTool(client).summarize_permission(
        {"account": "work", "event_id": "series"},
        ctx,
    )
    respond = GcalRespondToEventTool(client).summarize_permission(
        {
            "account": "personal",
            "event_id": "series_20260724T150000Z",
            "response": "accepted",
            "note": "Looking forward to it.",
        },
        ctx,
    )

    assert "Complete description" in create
    assert "alex@example.com" in create
    assert "attendees will be notified" in create
    assert "single recurring occurrence" in update
    assert "single recurring occurrence" in respond
    assert "RSVP comment/note: Looking forward to it." in respond
    assert "warning: this permanently removes" in delete
    assert "entire recurring series or standalone" in delete


def test_toolset_has_exact_eight_tools() -> None:
    settings = RickySettings(
        google=GoogleSettings(
            accounts={"work": GoogleAccountSettings(email="alex@company.example")}
        ),
        google_oauth_clients={
            "work": GoogleOAuthClientSettings(
                client_id="client",
                client_secret=SecretStr("secret"),
            )
        },
    )
    toolset = GcalToolset(settings, auth=cast(GoogleAuth, FakeAuth()))
    assert [tool.name for tool in toolset.tools] == [
        "gcal_list_calendars",
        "gcal_list_events",
        "gcal_get_event",
        "gcal_check_availability",
        "gcal_create_event",
        "gcal_update_event",
        "gcal_respond_to_event",
        "gcal_delete_event",
    ]


async def test_availability_missing_calendar_is_an_error_not_free(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/me/settings/timezone"):
            return httpx.Response(200, json={"value": "UTC"})
        # Google echoes back a different key than the one requested.
        return httpx.Response(200, json={"calendars": {"someone@example.com": {"busy": []}}})

    client = _client(handler)
    with pytest.raises(GcalError, match="omitted calendar 'primary'"):
        await GcalCheckAvailabilityTool(client).run(
            GcalCheckAvailabilityParams(
                account="work",
                time_min="2026-07-23T08:00:00Z",
                time_max="2026-07-23T17:00:00Z",
            ),
            _context(tmp_path),
        )
    await client.aclose()
