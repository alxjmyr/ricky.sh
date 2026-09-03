"""Canonical Slack objects. Raw wire dicts never leave this package."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ChannelKind = Literal["public", "private", "im", "mpim"]

KIND_API_TYPES: dict[ChannelKind, str] = {
    "public": "public_channel",
    "private": "private_channel",
    "im": "im",
    "mpim": "mpim",
}


class SlackUser(BaseModel):
    """One workspace member."""

    id: str
    name: str = ""
    real_name: str = ""
    display_name: str = ""
    email: str = ""
    is_bot: bool = False
    deleted: bool = False
    tz: str = ""

    @property
    def label(self) -> str:
        """Best human-readable handle for rendering and mention resolution."""
        return self.display_name or self.real_name or self.name or self.id

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> SlackUser:
        profile = data.get("profile") or {}
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            real_name=str(data.get("real_name") or profile.get("real_name") or ""),
            display_name=str(profile.get("display_name") or ""),
            email=str(profile.get("email") or ""),
            is_bot=bool(data.get("is_bot") or False),
            deleted=bool(data.get("deleted") or False),
            tz=str(data.get("tz") or ""),
        )


class SlackChannel(BaseModel):
    """One conversation: channel, private channel, DM, or group DM."""

    id: str
    name: str = ""
    kind: ChannelKind = "public"
    topic: str = ""
    is_member: bool = False
    user_id: str = ""  # DM counterpart (im only)
    updated_ms: int = 0  # Slack's last-change stamp; orders the unread probe

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> SlackChannel:
        kind: ChannelKind = "public"
        if data.get("is_im"):
            kind = "im"
        elif data.get("is_mpim"):
            kind = "mpim"
        elif data.get("is_private") or data.get("is_group"):
            kind = "private"
        topic = data.get("topic") or {}
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            kind=kind,
            topic=str(topic.get("value") or "") if isinstance(topic, dict) else "",
            is_member=bool(data.get("is_member") or kind in {"im", "mpim"}),
            user_id=str(data.get("user") or "") if kind == "im" else "",
            updated_ms=int(data.get("updated") or 0),
        )


class SlackReadState(BaseModel):
    """One conversation's read position. Never cached — it changes constantly."""

    channel_id: str
    last_read: str = ""  # ts of the last message the user has seen
    unread_count: int = 0  # messages after last_read, excluding the user's own
    latest_ts: str = ""  # ts of the newest message, "" when the conversation is empty

    @property
    def has_unread(self) -> bool:
        return self.unread_count > 0


def _search_channel_label(channel: dict[str, Any], channel_id: str) -> str:
    """Name a search hit's conversation, including its kind.

    A DM hit must not render as ``#name`` — that reads as a public channel.
    Slack also omits ``name`` for some DM hits, so fall back to the id: a hit
    with no location at all cannot be acted on.
    """
    name = str(channel.get("name") or "")
    if channel.get("is_im"):
        return f"DM with @{name}" if name else f"DM {channel_id}"
    if channel.get("is_mpim"):
        return f"group DM {name or channel_id}"
    if name:
        return f"#{name}"
    return channel_id


class SlackFileMeta(BaseModel):
    """Attachment metadata; content is fetched on demand only."""

    id: str
    name: str = ""
    mimetype: str = ""
    size: int = 0
    url_private: str = ""
    permalink: str = ""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> SlackFileMeta:
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            mimetype=str(data.get("mimetype") or ""),
            size=int(data.get("size") or 0),
            url_private=str(data.get("url_private") or ""),
            permalink=str(data.get("permalink") or ""),
        )


class SlackMessage(BaseModel):
    """One message (channel, DM, thread reply, or search hit)."""

    channel_id: str = ""
    channel_label: str = ""  # complete display label, e.g. "#eng" or "DM with @dana"
    ts: str
    user_id: str = ""
    user_label: str = ""
    text: str = ""
    thread_ts: str = ""
    reply_count: int = 0
    permalink: str = ""
    files: list[SlackFileMeta] = Field(default_factory=list)

    @classmethod
    def from_api(cls, data: dict[str, Any], *, channel_id: str = "") -> SlackMessage:
        return cls(
            channel_id=channel_id or str(data.get("channel") or ""),
            ts=str(data.get("ts") or ""),
            user_id=str(data.get("user") or data.get("bot_id") or ""),
            user_label=str(data.get("username") or ""),
            text=str(data.get("text") or ""),
            thread_ts=str(data.get("thread_ts") or ""),
            reply_count=int(data.get("reply_count") or 0),
            permalink=str(data.get("permalink") or ""),
            files=[SlackFileMeta.from_api(f) for f in data.get("files") or []],
        )

    @classmethod
    def from_search_api(cls, data: dict[str, Any]) -> SlackMessage:
        message = cls.from_api(data)
        channel = data.get("channel") or {}
        if isinstance(channel, dict):
            message.channel_id = str(channel.get("id") or "")
            message.channel_label = _search_channel_label(channel, message.channel_id)
        return message
