"""Fine-grained Google Calendar tools behind the normal permission boundary."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from typing import ClassVar, Literal, Self
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ricky.tools.base import (
    EffectIdentity,
    EffectReceipt,
    Risk,
    ToolContext,
    ToolResult,
    make_effect_identity,
)
from ricky.tools.integrations.gcal.client import GcalClient, GcalError
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
from ricky.tools.integrations.google.types import GoogleAccountId

_EMAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+$")
_RESPONSE_VALUES = {"accepted", "declined", "tentative"}
_NOTIFY_TRAILER = "attendees will be notified"


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GcalListCalendarsParams(_Params):
    account: GoogleAccountId


class GcalListEventsParams(_Params):
    account: GoogleAccountId
    calendar_id: str = Field(
        default="primary",
        description="Calendar id from gcal_list_calendars, or primary.",
    )
    time_min: str | None = Field(
        default=None,
        description="ISO 8601 lower bound; defaults to now in the account timezone.",
    )
    time_max: str | None = Field(
        default=None,
        description="ISO 8601 upper bound; defaults to gcal.default_window_days after time_min.",
    )
    query: str | None = Field(
        default=None,
        description="Optional Calendar free-text search over event fields.",
    )
    max_results: int = Field(
        default=0,
        ge=0,
        le=50,
        description="Maximum events (0 uses gcal.default_list_limit).",
    )


class GcalGetEventParams(_Params):
    account: GoogleAccountId
    event_id: str = Field(description="Event id from gcal_list_events.")
    calendar_id: str = Field(
        default="primary",
        description="Calendar id that owns the event.",
    )


class GcalCheckAvailabilityParams(_Params):
    account: GoogleAccountId
    time_min: str = Field(description="Required ISO 8601 lower bound.")
    time_max: str = Field(description="Required ISO 8601 upper bound.")
    calendar_ids: list[str] = Field(
        default_factory=lambda: ["primary"],
        min_length=1,
        description="Calendar ids to query; defaults to primary.",
    )


class GcalCreateEventParams(_Params):
    account: GoogleAccountId
    calendar_id: str = "primary"
    summary: str = Field(min_length=1)
    start: str = Field(description="ISO date for all-day, otherwise ISO date-time.")
    end: str = Field(description="Exclusive ISO date/date-time; must be after start.")
    timezone: str | None = Field(
        default=None,
        description="IANA timezone; defaults to the account Calendar timezone.",
    )
    all_day: bool = False
    description: str | None = None
    location: str | None = None
    attendees: list[str] = Field(default_factory=list)
    recurrence: list[str] = Field(
        default_factory=list,
        description="RRULE strings such as RRULE:FREQ=WEEKLY;COUNT=4.",
    )

    @field_validator("attendees")
    @classmethod
    def validate_attendees(cls, values: list[str]) -> list[str]:
        return _validated_emails(values)

    @field_validator("recurrence")
    @classmethod
    def validate_recurrence(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            stripped = value.strip()
            if not stripped.startswith("RRULE:"):
                raise ValueError("recurrence values must start with RRULE:")
            normalized.append(stripped)
        return normalized


class GcalUpdateEventParams(_Params):
    account: GoogleAccountId
    event_id: str
    calendar_id: str = "primary"
    summary: str | None = None
    start: str | None = None
    end: str | None = None
    timezone: str | None = None
    description: str | None = None
    location: str | None = None
    add_attendees: list[str] = Field(default_factory=list)
    remove_attendees: list[str] = Field(default_factory=list)

    @field_validator("add_attendees", "remove_attendees")
    @classmethod
    def validate_attendees(cls, values: list[str]) -> list[str]:
        return _validated_emails(values)

    @model_validator(mode="after")
    def validate_changes(self) -> Self:
        changed_fields = self.model_fields_set.difference(
            {"account", "event_id", "calendar_id", "add_attendees", "remove_attendees"}
        )
        for field in sorted(changed_fields):
            if getattr(self, field) is None:
                raise ValueError(
                    f"{field} must not be null; omit it to leave the field unchanged "
                    "(pass an empty string to clear a text field)"
                )
        if not changed_fields and not self.add_attendees and not self.remove_attendees:
            raise ValueError("provide at least one event field or attendee change")
        if "timezone" in changed_fields and not ({"start", "end"} & changed_fields):
            raise ValueError("timezone requires a start or end change")
        if self.summary is not None and not self.summary.strip():
            raise ValueError("summary must not be blank")
        overlap = {value.casefold() for value in self.add_attendees}.intersection(
            value.casefold() for value in self.remove_attendees
        )
        if overlap:
            raise ValueError("the same attendee cannot be both added and removed")
        return self


class GcalRespondToEventParams(_Params):
    account: GoogleAccountId
    event_id: str
    calendar_id: str = "primary"
    response: Literal["accepted", "declined", "tentative"]
    comment: str | None = Field(
        default=None,
        description="Optional RSVP response comment, note, or reason sent to the organizer.",
    )
    note: str | None = Field(
        default=None,
        description="Alias for comment; provide at most one of comment or note.",
    )

    @field_validator("comment", "note")
    @classmethod
    def validate_response_comment(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("RSVP comment/note must not be blank")
        return value

    @model_validator(mode="after")
    def validate_response_comment_alias(self) -> Self:
        if self.comment is not None and self.note is not None:
            raise ValueError("provide at most one of comment or note")
        return self

    @property
    def response_comment(self) -> str | None:
        return self.comment if self.comment is not None else self.note


class GcalDeleteEventParams(_Params):
    account: GoogleAccountId
    event_id: str
    calendar_id: str = "primary"


class _GcalReadTool:
    capability_id = "builtin.calendar.read"
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None


class _GcalMutationTool:
    name: ClassVar[str]
    Params: ClassVar[type[BaseModel]]
    _client: GcalClient

    capability_id = "builtin.calendar.mutate"
    effect_kind = "external"
    unattended = "allowed"
    state_guard_id = None

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        del ctx
        parsed = self.Params.model_validate(args)
        account = str(getattr(parsed, "account", ""))
        self._client.validate_account(account)
        for field in ("calendar_id", "event_id"):
            value = getattr(parsed, field, None)
            if value is not None:
                _id(str(value))
        encoded = json.dumps(parsed.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        occurrence = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return make_effect_identity(
            operation=self.name,
            target=account,
            occurrence=occurrence,
            summary=f"Run {self.name} on Calendar account {account}",
        )


class GcalListCalendarsTool(_GcalReadTool):
    name: ClassVar[str] = "gcal_list_calendars"
    description: ClassVar[str] = (
        "List calendars visible to one configured Google account, including raw "
        "calendar ids, access roles, primary marker, and timezone."
    )
    Params: ClassVar[type[BaseModel]] = GcalListCalendarsParams
    risk: ClassVar[Risk] = "read_only"

    def __init__(self, client: GcalClient) -> None:
        self._client = client

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = GcalListCalendarsParams.model_validate(params)
        items, truncated = await self._client.call_paginated(
            args.account,
            "users/me/calendarList",
            params={"showDeleted": False, "showHidden": False},
            items_key="items",
        )
        calendars = [GcalCalendar.from_api(item) for item in items]
        return ToolResult(content=render_calendars(args.account, calendars, truncated=truncated))


class GcalListEventsTool(_GcalReadTool):
    name: ClassVar[str] = "gcal_list_events"
    description: ClassVar[str] = (
        "List or search expanded Calendar events in a time window. Defaults to now "
        "through gcal.default_window_days, returns event/calendar ids, and marks "
        "recurring instances for safe follow-up updates."
    )
    Params: ClassVar[type[BaseModel]] = GcalListEventsParams
    risk: ClassVar[Risk] = "read_only"

    def __init__(self, client: GcalClient) -> None:
        self._client = client

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = GcalListEventsParams.model_validate(params)
        timezone = await self._client.timezone(args.account)
        lower = (
            _parse_datetime(args.time_min, timezone, label="time_min")
            if args.time_min
            else datetime.now(UTC).astimezone(_zone(timezone))
        )
        upper = (
            _parse_datetime(args.time_max, timezone, label="time_max")
            if args.time_max
            else lower + timedelta(days=ctx.settings.gcal.default_window_days)
        )
        _validate_range(lower, upper)
        limit = args.max_results or ctx.settings.gcal.default_list_limit
        request: dict[str, str | int | bool | list[str]] = {
            "singleEvents": True,
            "orderBy": "startTime",
            "showDeleted": False,
            "timeMin": lower.isoformat(),
            "timeMax": upper.isoformat(),
            "timeZone": timezone,
            "maxResults": limit,
        }
        if args.query:
            request["q"] = args.query
        items, truncated = await self._client.call_paginated(
            args.account,
            f"calendars/{_id(args.calendar_id)}/events",
            params=request,
            items_key="items",
            max_items=limit,
        )
        events = [
            GcalEvent.from_api(
                item,
                calendar_id=args.calendar_id,
                description_char_limit=ctx.settings.gcal.description_char_limit,
            )
            for item in items
        ]
        return ToolResult(
            content=render_events(
                args.account,
                events,
                account_timezone=timezone,
                truncated=truncated,
            )
        )


class GcalGetEventTool(_GcalReadTool):
    name: ClassVar[str] = "gcal_get_event"
    description: ClassVar[str] = (
        "Read one Calendar event by the event and calendar ids returned by a prior "
        "list. Includes attendees, recurrence, location, description, and existing Meet link."
    )
    Params: ClassVar[type[BaseModel]] = GcalGetEventParams
    risk: ClassVar[Risk] = "read_only"

    def __init__(self, client: GcalClient) -> None:
        self._client = client

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = GcalGetEventParams.model_validate(params)
        payload = await self._client.call(
            args.account,
            "GET",
            f"calendars/{_id(args.calendar_id)}/events/{_id(args.event_id)}",
        )
        event = GcalEvent.from_api(
            payload,
            calendar_id=args.calendar_id,
            description_char_limit=ctx.settings.gcal.description_char_limit,
        )
        return ToolResult(
            content=render_event(
                args.account,
                event,
                account_timezone=await self._client.timezone(args.account),
            )
        )


class GcalCheckAvailabilityTool(_GcalReadTool):
    name: ClassVar[str] = "gcal_check_availability"
    description: ClassVar[str] = (
        "Return merged busy blocks for explicit calendar ids in a required time "
        "window. Free time is everything outside those blocks within the range."
    )
    Params: ClassVar[type[BaseModel]] = GcalCheckAvailabilityParams
    risk: ClassVar[Risk] = "read_only"

    def __init__(self, client: GcalClient) -> None:
        self._client = client

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = GcalCheckAvailabilityParams.model_validate(params)
        timezone = await self._client.timezone(args.account)
        lower = _parse_datetime(args.time_min, timezone, label="time_min")
        upper = _parse_datetime(args.time_max, timezone, label="time_max")
        _validate_range(lower, upper)
        payload = await self._client.call(
            args.account,
            "POST",
            "freeBusy",
            json_body={
                "timeMin": lower.isoformat(),
                "timeMax": upper.isoformat(),
                "timeZone": timezone,
                "items": [{"id": value} for value in args.calendar_ids],
            },
        )
        raw_calendars = payload.get("calendars")
        calendars = raw_calendars if isinstance(raw_calendars, dict) else {}
        blocks: dict[str, list[GcalBusyBlock]] = {}
        for calendar_id in args.calendar_ids:
            raw = calendars.get(calendar_id)
            if not isinstance(raw, dict):
                # A missing calendar must never read as "fully free".
                raise GcalError(
                    f"free/busy response omitted calendar {calendar_id!r}; verify the "
                    "calendar id with gcal_list_calendars"
                )
            item = raw
            errors = item.get("errors") or []
            if errors:
                reason = ", ".join(
                    str(error.get("reason") or "unknown")
                    for error in errors
                    if isinstance(error, dict)
                )
                raise GcalError(
                    f"free/busy failed for calendar {calendar_id!r}: {reason or 'unknown error'}"
                )
            blocks[calendar_id] = [
                GcalBusyBlock.from_api(value, calendar_id=calendar_id)
                for value in item.get("busy") or []
                if isinstance(value, dict)
            ]
        return ToolResult(
            content=render_availability(
                args.account,
                blocks,
                time_min=lower,
                time_max=upper,
                account_timezone=timezone,
            )
        )


class GcalCreateEventTool(_GcalMutationTool):
    name: ClassVar[str] = "gcal_create_event"
    description: ClassVar[str] = (
        "Create a timed or all-day Calendar event/invite. Use explicit attendee "
        "emails and check availability first. The review gate shows all details; "
        "attendees are notified."
    )
    Params: ClassVar[type[BaseModel]] = GcalCreateEventParams
    risk: ClassVar[Risk] = "mutating"

    def __init__(self, client: GcalClient) -> None:
        self._client = client

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        parsed = GcalCreateEventParams.model_validate(args)
        if not parsed.summary.strip():
            raise GcalError("event summary must not be blank")
        timezone = parsed.timezone or "UTC"
        start, _ = _event_time(parsed.start, timezone, all_day=parsed.all_day)
        end, _ = _event_time(parsed.end, timezone, all_day=parsed.all_day)
        _validate_range(start, end)
        return super().effect_identity(args, ctx)

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        del ctx
        return _create_preview(args)

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = GcalCreateEventParams.model_validate(params)
        timezone = args.timezone or await self._client.timezone(args.account)
        start, start_value = _event_time(args.start, timezone, all_day=args.all_day)
        end, end_value = _event_time(args.end, timezone, all_day=args.all_day)
        _validate_range(start, end)
        body: dict[str, object] = {
            "summary": args.summary.strip(),
            "start": start_value,
            "end": end_value,
        }
        if args.description is not None:
            body["description"] = args.description
        if args.location is not None:
            body["location"] = args.location
        if args.attendees:
            body["attendees"] = [{"email": value} for value in args.attendees]
        if args.recurrence:
            body["recurrence"] = args.recurrence
        payload = await self._client.call(
            args.account,
            "POST",
            f"calendars/{_id(args.calendar_id)}/events",
            params={"sendUpdates": "all"},
            json_body=body,
            read_only=False,
            mutation_action="create event",
        )
        event = GcalEvent.from_api(
            payload,
            calendar_id=args.calendar_id,
            description_char_limit=ctx.settings.gcal.description_char_limit,
        )
        return ToolResult(
            content=(
                f"[{args.account}] Created Calendar event {event.id} "
                f"on calendar {args.calendar_id}; {_NOTIFY_TRAILER}."
            ),
            effect_receipt=EffectReceipt(
                disposition="performed", provider_reference=event.id or None
            ),
        )


class GcalUpdateEventTool(_GcalMutationTool):
    name: ClassVar[str] = "gcal_update_event"
    description: ClassVar[str] = (
        "Patch specified fields on one Calendar event. An expanded instance id "
        "changes one occurrence; a series id changes the series. Attendee add/remove "
        "uses exact emails and preserves unrelated attendees. Attendees are notified."
    )
    Params: ClassVar[type[BaseModel]] = GcalUpdateEventParams
    risk: ClassVar[Risk] = "mutating"

    def __init__(self, client: GcalClient) -> None:
        self._client = client

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        parsed = GcalUpdateEventParams.model_validate(args)
        timezone = parsed.timezone or "UTC"
        start = (
            _event_time(parsed.start, timezone, all_day=_date_only(parsed.start))[0]
            if parsed.start is not None
            else None
        )
        end = (
            _event_time(parsed.end, timezone, all_day=_date_only(parsed.end))[0]
            if parsed.end is not None
            else None
        )
        if start is not None and end is not None:
            if (type(start) is date) != (type(end) is date):
                raise GcalError("start and end must both be dates or both be date-times")
            _validate_range(start, end)
        return super().effect_identity(args, ctx)

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        del ctx
        return _update_preview("update Calendar event", args)

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = GcalUpdateEventParams.model_validate(params)
        fields = args.model_fields_set
        patch: dict[str, object] = {}
        if "summary" in fields:
            patch["summary"] = args.summary
        if "description" in fields:
            patch["description"] = args.description
        if "location" in fields:
            patch["location"] = args.location
        if {"start", "end"} & fields:
            timezone = args.timezone or await self._client.timezone(args.account)
            start_value: date | datetime | None = None
            end_value: date | datetime | None = None
            if "start" in fields and args.start is not None:
                start_value, patch["start"] = _event_time(
                    args.start,
                    timezone,
                    all_day=_date_only(args.start),
                )
            if "end" in fields and args.end is not None:
                end_value, patch["end"] = _event_time(
                    args.end,
                    timezone,
                    all_day=_date_only(args.end),
                )
            if start_value is not None and end_value is not None:
                if (type(start_value) is date) != (type(end_value) is date):
                    raise GcalError("start and end must both be dates or both be date-times")
                _validate_range(start_value, end_value)

        headers: dict[str, str] | None = None
        if args.add_attendees or args.remove_attendees:
            existing = await self._client.call(
                args.account,
                "GET",
                f"calendars/{_id(args.calendar_id)}/events/{_id(args.event_id)}",
            )
            if existing.get("attendeesOmitted"):
                raise GcalError(
                    "Calendar omitted attendees; refusing to replace an incomplete attendee list"
                )
            attendees = [
                dict(item) for item in existing.get("attendees") or [] if isinstance(item, dict)
            ]
            remove = {value.casefold() for value in args.remove_attendees}
            present = {
                str(item.get("email") or "").casefold() for item in attendees if item.get("email")
            }
            missing = remove.difference(present)
            if missing:
                raise GcalError(
                    "cannot remove attendees not present on the event: "
                    + ", ".join(sorted(missing))
                )
            attendees = [
                item for item in attendees if str(item.get("email") or "").casefold() not in remove
            ]
            present = {
                str(item.get("email") or "").casefold() for item in attendees if item.get("email")
            }
            for email in args.add_attendees:
                if email.casefold() not in present:
                    attendees.append({"email": email})
                    present.add(email.casefold())
            patch["attendees"] = attendees
            etag = str(existing.get("etag") or "")
            if etag:
                # Guard the read-modify-write: a concurrent attendee change
                # fails with 412 instead of being silently overwritten.
                headers = {"If-Match": etag}

        if not patch:
            raise GcalError("no effective changes; provide at least one non-null field")
        await self._client.call(
            args.account,
            "PATCH",
            f"calendars/{_id(args.calendar_id)}/events/{_id(args.event_id)}",
            params={"sendUpdates": "all"},
            json_body=patch,
            headers=headers,
            read_only=False,
            mutation_action="update event",
        )
        return ToolResult(
            content=(
                f"[{args.account}] Updated Calendar event {args.event_id} "
                f"on calendar {args.calendar_id}; {_NOTIFY_TRAILER}."
            ),
            effect_receipt=EffectReceipt(disposition="performed", provider_reference=args.event_id),
        )


class GcalRespondToEventTool(_GcalMutationTool):
    name: ClassVar[str] = "gcal_respond_to_event"
    description: ClassVar[str] = (
        "Accept, decline, or tentatively accept a Calendar invitation for the "
        "acting account, optionally including an RSVP comment, note, or reason. "
        "Only the self attendee response is patched; the organizer is notified."
    )
    Params: ClassVar[type[BaseModel]] = GcalRespondToEventParams
    risk: ClassVar[Risk] = "mutating"

    def __init__(self, client: GcalClient) -> None:
        self._client = client

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        del ctx
        response_comment = args.get("comment")
        if response_comment is None:
            response_comment = args.get("note")
        return (
            f"account: {args.get('account', '')}\n"
            f"respond {args.get('response', '')} to Calendar event "
            f"{args.get('event_id', '')} on calendar {args.get('calendar_id', 'primary')}\n"
            f"target scope: {_target_scope(str(args.get('event_id', '')))}\n"
            f"RSVP comment/note: {response_comment or '[none]'}\n"
            f"{_NOTIFY_TRAILER}"
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = GcalRespondToEventParams.model_validate(params)
        payload = await self._client.call(
            args.account,
            "GET",
            f"calendars/{_id(args.calendar_id)}/events/{_id(args.event_id)}",
        )
        attendees = [item for item in payload.get("attendees") or [] if isinstance(item, dict)]
        self_attendee = next((item for item in attendees if item.get("self") is True), None)
        if self_attendee is None:
            raise GcalError(
                f"account {args.account!r} is not an attendee of event {args.event_id!r}"
            )
        updated_self = dict(self_attendee)
        updated_self["responseStatus"] = args.response
        if args.response_comment is not None:
            updated_self["comment"] = args.response_comment
        await self._client.call(
            args.account,
            "PATCH",
            f"calendars/{_id(args.calendar_id)}/events/{_id(args.event_id)}",
            params={"sendUpdates": "all"},
            json_body={"attendees": [updated_self], "attendeesOmitted": True},
            read_only=False,
            mutation_action="respond to event",
        )
        return ToolResult(
            content=(
                f"[{args.account}] Responded {args.response} to Calendar event "
                f"{args.event_id}; {_NOTIFY_TRAILER}."
            ),
            effect_receipt=EffectReceipt(disposition="performed", provider_reference=args.event_id),
        )


class GcalDeleteEventTool(_GcalMutationTool):
    name: ClassVar[str] = "gcal_delete_event"
    description: ClassVar[str] = (
        "Permanently delete exactly one previously read Calendar event. An instance "
        "id deletes one occurrence; a series id deletes the entire recurring series. "
        "This is destructive, permission-gated, and attendees are notified."
    )
    Params: ClassVar[type[BaseModel]] = GcalDeleteEventParams
    risk: ClassVar[Risk] = "destructive"

    def __init__(self, client: GcalClient) -> None:
        self._client = client

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        del ctx
        return _update_preview("DELETE Calendar event", args, destructive=True)

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = GcalDeleteEventParams.model_validate(params)
        await self._client.call(
            args.account,
            "DELETE",
            f"calendars/{_id(args.calendar_id)}/events/{_id(args.event_id)}",
            params={"sendUpdates": "all"},
            read_only=False,
            mutation_action="delete event",
        )
        return ToolResult(
            content=(
                f"[{args.account}] Deleted Calendar event {args.event_id} "
                f"from calendar {args.calendar_id}; {_NOTIFY_TRAILER}."
            ),
            effect_receipt=EffectReceipt(disposition="performed", provider_reference=args.event_id),
        )


def _create_preview(args: dict[str, object]) -> str:
    attendees = _preview_list(args.get("attendees"))
    recurrence = _preview_list(args.get("recurrence"))
    return "\n".join(
        [
            f"account: {args.get('account', '')}",
            f"create Calendar event on {args.get('calendar_id', 'primary')}",
            f"title: {args.get('summary', '')}",
            (
                f"time: {args.get('start', '')}–{args.get('end', '')} "
                f"(timezone: {args.get('timezone') or 'account default'}; "
                f"{'all day' if args.get('all_day') else 'timed'})"
            ),
            f"attendees: {attendees}",
            f"recurrence: {recurrence}",
            f"location: {args.get('location') or '[none]'}",
            "--- complete description ---",
            str(args.get("description") or ""),
            "--- end description ---",
            _NOTIFY_TRAILER,
        ]
    )


def _update_preview(
    action: str,
    args: dict[str, object],
    *,
    destructive: bool = False,
) -> str:
    event_id = str(args.get("event_id", ""))
    lines = [
        f"account: {args.get('account', '')}",
        f"{action} {event_id} on calendar {args.get('calendar_id', 'primary')}",
        f"target scope: {_target_scope(event_id)}",
    ]
    if destructive:
        lines.append("warning: this permanently removes the selected event target")
    else:
        changes = [
            key
            for key in (
                "summary",
                "start",
                "end",
                "timezone",
                "description",
                "location",
                "add_attendees",
                "remove_attendees",
            )
            if key in args
        ]
        for key in changes:
            lines.append(f"{key}: {args.get(key)}")
    lines.append(_NOTIFY_TRAILER)
    return "\n".join(lines)


def _target_scope(event_id: str) -> str:
    if "_" in event_id:
        return "single recurring occurrence (instance id)"
    return "entire recurring series or standalone event; read the event first if uncertain"


def _validated_emails(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        stripped = value.strip()
        if not _EMAIL.fullmatch(stripped):
            raise ValueError(f"invalid explicit attendee email: {value!r}")
        folded = stripped.casefold()
        if folded not in seen:
            normalized.append(stripped)
            seen.add(folded)
    return normalized


def _parse_datetime(value: str, timezone: str, *, label: str) -> datetime:
    if _date_only(value):
        raise GcalError(f"{label} must be an ISO date-time, not a date-only value")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GcalError(f"invalid {label} ISO date-time {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_zone(timezone))
    return parsed


def _event_time(
    value: str,
    timezone: str,
    *,
    all_day: bool,
) -> tuple[date | datetime, dict[str, str]]:
    if all_day:
        if not _date_only(value):
            raise GcalError("all-day event start/end must both be ISO dates")
        try:
            parsed_date = date.fromisoformat(value)
        except ValueError as exc:
            raise GcalError(f"invalid all-day ISO date {value!r}") from exc
        return parsed_date, {"date": parsed_date.isoformat()}
    parsed_datetime = _parse_datetime(value, timezone, label="event time")
    zone = _zone(timezone)
    localized = parsed_datetime.astimezone(zone)
    return localized, {"dateTime": localized.isoformat(), "timeZone": timezone}


def _validate_range(start: date | datetime, end: date | datetime) -> None:
    try:
        valid = end > start
    except TypeError as exc:
        raise GcalError("start and end must use the same date/date-time shape") from exc
    if not valid:
        raise GcalError("event/time window end must be after start")


def _date_only(value: str) -> bool:
    return "T" not in value and " " not in value


def _zone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise GcalError(f"unknown IANA timezone {value!r}") from exc


def _id(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise GcalError("Calendar/event id must not be blank")
    return quote(stripped, safe="")


def _preview_list(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "[none]"
    return str(value or "[none]")


__all__ = [
    "GcalCheckAvailabilityTool",
    "GcalCreateEventTool",
    "GcalDeleteEventTool",
    "GcalGetEventTool",
    "GcalListCalendarsTool",
    "GcalListEventsTool",
    "GcalRespondToEventTool",
    "GcalUpdateEventTool",
]
