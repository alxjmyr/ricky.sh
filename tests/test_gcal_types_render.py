"""Canonical Calendar model and deterministic rendering tests."""

from __future__ import annotations

from datetime import datetime

from ricky.tools.integrations.gcal.render import (
    render_availability,
    render_calendars,
    render_event,
    render_events,
)
from ricky.tools.integrations.gcal.types import (
    GcalBusyBlock,
    GcalCalendar,
    GcalEvent,
)


def _timed_event(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "event-1",
        "status": "confirmed",
        "summary": "Planning sync",
        "description": "Agenda",
        "location": "Room 4",
        "start": {
            "dateTime": "2026-07-20T14:30:00Z",
            "timeZone": "America/Chicago",
        },
        "end": {
            "dateTime": "2026-07-20T15:00:00Z",
            "timeZone": "America/Chicago",
        },
        "organizer": {"email": "dana@example.com"},
        "attendees": [
            {
                "email": "alex@company.example",
                "displayName": "Alex",
                "responseStatus": "needsAction",
                "self": True,
            },
            {"email": "sam@example.com", "responseStatus": "accepted"},
        ],
        "htmlLink": "https://calendar.google.com/event?eid=event-1",
        "hangoutLink": "https://meet.google.com/abc-defg-hij",
        "visibility": "default",
        "eventType": "default",
    }
    payload.update(overrides)
    return payload


def test_calendar_model_round_trips_through_json() -> None:
    calendar = GcalCalendar.from_api(
        {
            "id": "alex@example.com",
            "summary": "Alex",
            "primary": True,
            "accessRole": "owner",
            "timeZone": "America/Chicago",
        }
    )

    assert calendar.primary
    assert calendar.time_zone == "America/Chicago"
    assert GcalCalendar.model_validate_json(calendar.model_dump_json()) == calendar


def test_timed_event_parses_attendees_and_round_trips() -> None:
    event = GcalEvent.from_api(_timed_event(), calendar_id="primary")

    assert not event.all_day
    assert event.start.date_time == datetime.fromisoformat("2026-07-20T14:30:00+00:00")
    assert event.attendees[0].is_self
    assert event.attendees[1].response_status == "accepted"
    assert event.meet_link == "https://meet.google.com/abc-defg-hij"
    assert GcalEvent.model_validate_json(event.model_dump_json()) == event


def test_all_day_and_recurring_instance_parse() -> None:
    event = GcalEvent.from_api(
        {
            "id": "series_20260721",
            "summary": "Conference",
            "start": {"date": "2026-07-21"},
            "end": {"date": "2026-07-23"},
            "recurringEventId": "series",
            "recurrence": ["RRULE:FREQ=YEARLY"],
        },
        calendar_id="team@example.com",
    )

    assert event.all_day
    assert event.start.date is not None
    assert event.end.date is not None
    assert event.recurring_event_id == "series"


def test_unknown_attendee_status_falls_back_safely() -> None:
    event = GcalEvent.from_api(
        _timed_event(attendees=[{"email": "new@example.com", "responseStatus": "futureValue"}]),
        calendar_id="primary",
    )
    assert event.attendees[0].response_status == "needsAction"


def test_description_cap_is_explicit() -> None:
    event = GcalEvent.from_api(
        _timed_event(description="x" * 200),
        calendar_id="primary",
        description_char_limit=80,
    )

    assert len(event.description) == 80
    assert "[... description truncated at 80 chars]" in event.description


def test_render_calendars_includes_ids_roles_and_truncation() -> None:
    output = render_calendars(
        "work",
        [
            GcalCalendar(
                id="primary-id",
                summary="Work",
                primary=True,
                access_role="owner",
                time_zone="America/Chicago",
            )
        ],
        truncated=True,
    )

    assert "[work] Work" in output
    assert "calendar primary-id" in output
    assert "role owner" in output
    assert "primary" in output
    assert "truncated" in output


def test_render_timed_event_has_local_offset_ids_attendees_and_recurrence() -> None:
    event = GcalEvent.from_api(
        _timed_event(
            id="series_20260720T143000Z",
            recurringEventId="series",
            recurrence=["RRULE:FREQ=WEEKLY"],
        ),
        calendar_id="primary",
    )

    output = render_event("work", event, account_timezone="America/Chicago")

    assert "[work] Mon 2026-07-20 09:30–10:00 CDT (UTC-05:00)" in output
    assert "event series_20260720T143000Z, calendar primary" in output
    assert "Alex <alex@company.example> [needsAction] [self]" in output
    assert "sam@example.com [accepted]" in output
    assert "instance of series" in output
    assert "RRULE:FREQ=WEEKLY" in output
    assert "meet: https://meet.google.com/abc-defg-hij" in output


def test_render_all_day_uses_exclusive_end_without_times() -> None:
    event = GcalEvent.from_api(
        {
            "id": "all-day",
            "summary": "Conference",
            "start": {"date": "2026-07-21"},
            "end": {"date": "2026-07-23"},
        },
        calendar_id="primary",
    )

    output = render_event("personal", event, account_timezone="America/Chicago")

    assert "2026-07-21–2026-07-23" in output
    assert "end date exclusive" in output
    assert ":00" not in output


def test_render_empty_event_list_is_account_scoped() -> None:
    assert (
        render_events("personal", [], account_timezone="America/Chicago")
        == "[personal] No matching events."
    )


def test_render_availability_merges_overlapping_blocks_and_has_free_trailer() -> None:
    output = render_availability(
        "work",
        {
            "primary": [
                GcalBusyBlock(
                    calendar_id="primary",
                    start=datetime.fromisoformat("2026-07-23T14:00:00+00:00"),
                    end=datetime.fromisoformat("2026-07-23T15:00:00+00:00"),
                ),
                GcalBusyBlock(
                    calendar_id="primary",
                    start=datetime.fromisoformat("2026-07-23T14:30:00+00:00"),
                    end=datetime.fromisoformat("2026-07-23T16:00:00+00:00"),
                ),
            ],
            "team@example.com": [],
        },
        time_min=datetime.fromisoformat("2026-07-23T13:00:00+00:00"),
        time_max=datetime.fromisoformat("2026-07-23T22:00:00+00:00"),
        account_timezone="America/Chicago",
    )

    assert output.count("\n    busy ") == 1
    assert "09:00 CDT (UTC-05:00)–Thu 2026-07-23 11:00 CDT" in output
    assert "calendar team@example.com: no busy blocks" in output
    assert "free outside these busy blocks within the queried range" in output
