"""Deterministic, compact rendering for Google Calendar tool results."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ricky.tools.integrations.gcal.types import (
    GcalBusyBlock,
    GcalCalendar,
    GcalEvent,
)


def render_calendars(
    account: str,
    calendars: list[GcalCalendar],
    *,
    truncated: bool = False,
) -> str:
    if not calendars:
        return f"[{account}] No calendars."
    lines = [
        (
            f"[{account}] {item.summary or '(untitled calendar)'} "
            f"(calendar {item.id}, role {item.access_role or 'unknown'}, "
            f"timezone {item.time_zone}"
            f"{', primary' if item.primary else ''})"
        )
        for item in calendars
    ]
    if truncated:
        lines.append(f"[{account}] [calendar list truncated at the pagination cap]")
    return "\n".join(lines)


def render_events(
    account: str,
    events: list[GcalEvent],
    *,
    account_timezone: str,
    truncated: bool = False,
) -> str:
    if not events:
        return f"[{account}] No matching events."
    blocks = [render_event(account, event, account_timezone=account_timezone) for event in events]
    if truncated:
        blocks.append(f"[{account}] [event list truncated at the pagination cap]")
    return "\n".join(blocks)


def render_event(account: str, event: GcalEvent, *, account_timezone: str) -> str:
    when = _event_range(event, account_timezone)
    lines = [
        (
            f"[{account}] {when}  {event.summary or '(untitled event)'}  "
            f"(event {event.id}, calendar {event.calendar_id})"
        )
    ]
    details: list[str] = []
    if event.organizer:
        details.append(f"organizer: {event.organizer}")
    if event.attendees:
        rendered = ", ".join(
            (
                f"{attendee.display_name} <{attendee.email}>"
                if attendee.display_name
                else attendee.email
            )
            + f" [{attendee.response_status}]"
            + (" [self]" if attendee.is_self else "")
            for attendee in event.attendees
        )
        details.append(f"attendees: {rendered}")
    if details:
        lines.append(f"  {' · '.join(details)}")

    recurrence: list[str] = []
    if event.recurrence:
        recurrence.append("repeats: " + ", ".join(event.recurrence))
    if event.recurring_event_id:
        recurrence.append(f"instance of {event.recurring_event_id}")
    if recurrence:
        lines.append(f"  {' · '.join(recurrence)}")
    if event.location:
        lines.append(f"  location: {event.location}")
    if event.meet_link:
        lines.append(f"  meet: {event.meet_link}")
    if event.description:
        lines.append("  description:")
        lines.extend(f"    {line}" if line else "    " for line in event.description.splitlines())
    if event.html_link:
        lines.append(f"  link: {event.html_link}")
    return "\n".join(lines)


def render_availability(
    account: str,
    blocks: Mapping[str, list[GcalBusyBlock]],
    *,
    time_min: datetime,
    time_max: datetime,
    account_timezone: str,
) -> str:
    zone = _zone(account_timezone)
    lines = [
        (
            f"[{account}] Availability {_format_datetime(time_min.astimezone(zone))} "
            f"to {_format_datetime(time_max.astimezone(zone))}"
        )
    ]
    for calendar_id, calendar_blocks in blocks.items():
        merged = _merge_blocks(calendar_blocks)
        if not merged:
            lines.append(f"  calendar {calendar_id}: no busy blocks")
            continue
        lines.append(f"  calendar {calendar_id}:")
        for block in merged:
            lines.append(
                "    busy "
                f"{_format_datetime(block.start.astimezone(zone))}–"
                f"{_format_datetime(block.end.astimezone(zone))}"
            )
    lines.append(
        "  free outside these busy blocks within the queried range "
        f"({_offset_label(time_min.astimezone(zone))})."
    )
    return "\n".join(lines)


def _event_range(event: GcalEvent, timezone: str) -> str:
    if event.all_day:
        if event.start.date is None or event.end.date is None:
            return "[invalid all-day range]"
        if event.end.date == event.start.date + timedelta(days=1):
            return f"{event.start.date.isoformat()} (all day)"
        return (
            f"{event.start.date.isoformat()}–{event.end.date.isoformat()} "
            "(all day; end date exclusive)"
        )
    if event.start.date_time is None or event.end.date_time is None:
        return "[invalid timed range]"
    zone = _zone(timezone)
    start = event.start.date_time.astimezone(zone)
    end = event.end.date_time.astimezone(zone)
    if start.date() == end.date():
        return f"{start:%a %Y-%m-%d %H:%M}–{end:%H:%M} {_offset_label(start)}"
    return f"{_format_datetime(start)}–{_format_datetime(end)}"


def _format_datetime(value: datetime) -> str:
    return f"{value:%a %Y-%m-%d %H:%M} {_offset_label(value)}"


def _offset_label(value: datetime) -> str:
    offset = value.utcoffset()
    if offset is None:
        return value.tzname() or "unknown timezone"
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, remaining = divmod(abs(minutes), 60)
    name = value.tzname() or "local"
    return f"{name} (UTC{sign}{hours:02d}:{remaining:02d})"


def _zone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _merge_blocks(blocks: list[GcalBusyBlock]) -> list[GcalBusyBlock]:
    merged: list[GcalBusyBlock] = []
    for block in sorted(blocks, key=lambda item: (item.start, item.end)):
        if not merged or block.start > merged[-1].end:
            merged.append(block.model_copy())
            continue
        if block.end > merged[-1].end:
            merged[-1] = merged[-1].model_copy(update={"end": block.end})
    return merged


__all__ = [
    "render_availability",
    "render_calendars",
    "render_event",
    "render_events",
]
