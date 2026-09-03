"""Deterministic model-readable rendering of Slack objects.

Output is compact line-oriented text; the registry's truncation rule still
applies on top. Raw ``ts`` values are always included because the model
needs them to address threads.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from ricky.tools.integrations.slack.types import SlackChannel, SlackMessage, SlackUser

_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
_CHANNEL_REF = re.compile(r"<#[CGD][A-Z0-9]+\|([^>]+)>")
_LINK = re.compile(r"<(https?://[^>|]+)\|([^>]+)>")
_BARE_LINK = re.compile(r"<(https?://[^>|]+)>")
_DM_LABEL = re.compile(r"DM with @([UW][A-Z0-9]+)")


def format_ts(ts: str) -> str:
    """Render a Slack ``ts`` as a UTC timestamp; fall back to the raw value."""
    try:
        moment = datetime.fromtimestamp(float(ts), tz=UTC)
    except (ValueError, OverflowError):
        return ts
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def unescape_text(text: str, users: dict[str, SlackUser]) -> str:
    """Resolve mention/channel/link markup into plain readable text."""

    def mention(match: re.Match[str]) -> str:
        user = users.get(match.group(1))
        return f"@{user.label}" if user else f"@{match.group(1)}"

    text = _MENTION.sub(mention, text)
    text = _CHANNEL_REF.sub(lambda m: f"#{m.group(1)}", text)
    text = _LINK.sub(lambda m: f"{m.group(2)} ({m.group(1)})", text)
    text = _BARE_LINK.sub(lambda m: m.group(1), text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _resolve_dm_label(label: str, users: dict[str, SlackUser]) -> str:
    """Turn ``DM with @U123`` into ``DM with @dana``.

    ``search.messages`` names an IM channel by the counterpart's *id*, which
    is unreadable in a result list; the directory already has the handle.
    """
    match = _DM_LABEL.fullmatch(label)
    if not match:
        return label
    user = users.get(match.group(1))
    return f"DM with @{user.label}" if user else label


def render_message(message: SlackMessage, users: dict[str, SlackUser]) -> str:
    """Render one message as a header line, indented text, and file lines."""
    user = users.get(message.user_id)
    author = f"@{user.label}" if user else f"@{message.user_label or message.user_id or 'unknown'}"

    header = f"[{format_ts(message.ts)}] {author}"
    if message.channel_label:
        header += f" in {_resolve_dm_label(message.channel_label, users)}"
    header += f" (ts {message.ts}"
    if message.thread_ts and message.thread_ts != message.ts:
        header += f", in thread {message.thread_ts}"
    elif message.reply_count:
        header += f", thread: {message.reply_count} replies"
    header += ")"

    lines = [header]
    text = unescape_text(message.text, users).strip()
    lines.extend(f"  {line}" for line in text.splitlines() if line.strip())
    lines.extend(
        f"  [file: {f.name or f.id} · {f.mimetype or 'unknown'} · {_size(f.size)} · id {f.id}]"
        for f in message.files
    )
    if message.permalink:
        lines.append(f"  {message.permalink}")
    return "\n".join(lines)


def render_messages(
    messages: list[SlackMessage],
    users: dict[str, SlackUser],
    *,
    heading: str = "",
    truncated: bool = False,
) -> str:
    """Render a message list oldest-first with an optional heading."""
    ordered = sorted(messages, key=lambda m: _ts_key(m.ts))
    parts = [heading] if heading else []
    parts.extend(render_message(message, users) for message in ordered)
    if truncated:
        parts.append("[result truncated: more messages exist than were fetched]")
    if not messages:
        parts.append("[no messages]")
    return "\n\n".join(parts)


def channel_label(channel: SlackChannel, users: dict[str, SlackUser]) -> str:
    """Name one conversation, including its kind, for any human-facing line."""
    if channel.kind == "im":
        user = users.get(channel.user_id)
        return f"DM with @{user.label}" if user else f"DM {channel.user_id or channel.id}"
    if channel.kind == "mpim":
        return f"group DM {channel.name or channel.id}"
    return f"#{channel.name}" if channel.name else channel.id


def render_channel(channel: SlackChannel, users: dict[str, SlackUser]) -> str:
    """Render one directory entry as a single line."""
    label = channel_label(channel, users)
    line = f"{channel.id}  {label}  ({channel.kind}"
    if channel.kind in {"public", "private"} and not channel.is_member:
        line += ", not a member"
    line += ")"
    if channel.topic:
        line += f" — {channel.topic}"
    return line


def render_user(user: SlackUser) -> str:
    """Render one user directory entry as a single line."""
    parts = [user.id, f"@{user.label}"]
    if user.real_name and user.real_name != user.label:
        parts.append(user.real_name)
    if user.email:
        parts.append(user.email)
    if user.tz:
        parts.append(user.tz)
    if user.is_bot:
        parts.append("(bot)")
    if user.deleted:
        parts.append("(deactivated)")
    return "  ".join(parts)


def _ts_key(ts: str) -> float:
    try:
        return float(ts)
    except ValueError:
        return 0.0


def _size(size: int) -> str:
    if size >= 1_048_576:
        return f"{size / 1_048_576:.1f}MB"
    if size >= 1024:
        return f"{size / 1024:.0f}KB"
    return f"{size}B"
