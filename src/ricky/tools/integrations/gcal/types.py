"""Canonical, JSON-round-trip-safe Google Calendar models."""

from __future__ import annotations

from datetime import date as Date
from datetime import datetime as DateTime
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

from ricky.tools.integrations.text import cap_text

ResponseStatus = Literal["needsAction", "accepted", "declined", "tentative"]
_RESPONSE_STATUSES: dict[str, ResponseStatus] = {
    "needsAction": "needsAction",
    "accepted": "accepted",
    "declined": "declined",
    "tentative": "tentative",
}


class GcalCalendar(BaseModel):
    id: str
    summary: str
    primary: bool = False
    access_role: str = ""
    time_zone: str = "UTC"

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> GcalCalendar:
        return cls(
            id=str(payload.get("id") or ""),
            summary=str(payload.get("summary") or ""),
            primary=bool(payload.get("primary")),
            access_role=str(payload.get("accessRole") or ""),
            time_zone=str(payload.get("timeZone") or "UTC"),
        )


class GcalAttendee(BaseModel):
    email: str
    display_name: str = ""
    response_status: ResponseStatus = "needsAction"
    optional: bool = False
    organizer: bool = False
    is_self: bool = False

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> GcalAttendee:
        raw_status = str(payload.get("responseStatus") or "needsAction")
        status = _RESPONSE_STATUSES.get(raw_status, "needsAction")
        return cls(
            email=str(payload.get("email") or ""),
            display_name=str(payload.get("displayName") or ""),
            response_status=status,
            optional=bool(payload.get("optional")),
            organizer=bool(payload.get("organizer")),
            is_self=bool(payload.get("self")),
        )


class GcalEventTime(BaseModel):
    date: Date | None = None
    date_time: DateTime | None = None
    time_zone: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if (self.date is None) == (self.date_time is None):
            raise ValueError("Calendar event time must contain exactly one of date or dateTime")
        return self

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> GcalEventTime:
        raw_date = payload.get("date")
        raw_datetime = payload.get("dateTime")
        if raw_date is not None:
            return cls(
                date=Date.fromisoformat(str(raw_date)),
                time_zone=str(payload.get("timeZone") or "") or None,
            )
        if raw_datetime is None:
            raise ValueError("Calendar event time is missing date/dateTime")
        return cls(
            date_time=DateTime.fromisoformat(str(raw_datetime).replace("Z", "+00:00")),
            time_zone=str(payload.get("timeZone") or "") or None,
        )


class GcalEvent(BaseModel):
    id: str
    calendar_id: str
    status: str = ""
    summary: str = ""
    description: str = ""
    location: str = ""
    start: GcalEventTime
    end: GcalEventTime
    all_day: bool
    organizer: str = ""
    attendees: list[GcalAttendee] = Field(default_factory=list)
    recurrence: list[str] = Field(default_factory=list)
    recurring_event_id: str = ""
    html_link: str = ""
    meet_link: str = ""
    visibility: str = ""
    event_type: str = "default"

    @classmethod
    def from_api(
        cls,
        payload: dict[str, Any],
        *,
        calendar_id: str,
        description_char_limit: int = 20_000,
    ) -> GcalEvent:
        start = GcalEventTime.from_api(_mapping(payload.get("start")))
        end = GcalEventTime.from_api(_mapping(payload.get("end")))
        raw_description = str(payload.get("description") or "")
        description = _capped(raw_description, description_char_limit)
        organizer = _mapping(payload.get("organizer"))
        return cls(
            id=str(payload.get("id") or ""),
            calendar_id=calendar_id,
            status=str(payload.get("status") or ""),
            summary=str(payload.get("summary") or ""),
            description=description,
            location=str(payload.get("location") or ""),
            start=start,
            end=end,
            all_day=start.date is not None,
            organizer=str(organizer.get("email") or ""),
            attendees=[
                GcalAttendee.from_api(item)
                for item in payload.get("attendees") or []
                if isinstance(item, dict)
            ],
            recurrence=[str(value) for value in payload.get("recurrence") or []],
            recurring_event_id=str(payload.get("recurringEventId") or ""),
            html_link=str(payload.get("htmlLink") or ""),
            meet_link=str(payload.get("hangoutLink") or ""),
            visibility=str(payload.get("visibility") or ""),
            event_type=str(payload.get("eventType") or "default"),
        )


class GcalBusyBlock(BaseModel):
    calendar_id: str
    start: DateTime
    end: DateTime

    @classmethod
    def from_api(cls, payload: dict[str, Any], *, calendar_id: str) -> GcalBusyBlock:
        return cls(
            calendar_id=calendar_id,
            start=DateTime.fromisoformat(str(payload.get("start") or "").replace("Z", "+00:00")),
            end=DateTime.fromisoformat(str(payload.get("end") or "").replace("Z", "+00:00")),
        )


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _capped(value: str, limit: int) -> str:
    return cap_text(value, limit, label="description")


__all__ = [
    "GcalAttendee",
    "GcalBusyBlock",
    "GcalCalendar",
    "GcalEvent",
    "GcalEventTime",
    "ResponseStatus",
]
