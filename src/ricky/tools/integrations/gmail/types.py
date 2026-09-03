"""Canonical, JSON-round-trip-safe Gmail models."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ricky.tools.integrations.gmail.mime import attachment_parts, extract_body_text


class GmailLabel(BaseModel):
    id: str
    name: str
    kind: Literal["system", "user"]

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> GmailLabel:
        return cls(
            id=str(payload.get("id") or ""),
            name=str(payload.get("name") or ""),
            kind="user" if str(payload.get("type") or "").lower() == "user" else "system",
        )


class GmailAttachmentMeta(BaseModel):
    message_id: str
    attachment_id: str
    filename: str
    mime_type: str
    size_bytes: int = Field(ge=0)


class GmailMessage(BaseModel):
    id: str
    thread_id: str
    label_ids: list[str] = Field(default_factory=list)
    date: datetime | None = None
    from_addr: str = ""
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    subject: str = ""
    snippet: str = ""
    body_text: str = ""
    attachments: list[GmailAttachmentMeta] = Field(default_factory=list)
    message_id_header: str = ""

    @classmethod
    def from_api(
        cls,
        payload: dict[str, Any],
        *,
        body_char_limit: int = 20_000,
    ) -> GmailMessage:
        message_id = str(payload.get("id") or "")
        mime_payload = payload.get("payload") or {}
        headers = _headers(mime_payload)
        return cls(
            id=message_id,
            thread_id=str(payload.get("threadId") or ""),
            label_ids=[str(value) for value in payload.get("labelIds") or []],
            date=_message_date(payload, headers),
            from_addr=headers.get("from", ""),
            to=_address_list(headers.get("to", "")),
            cc=_address_list(headers.get("cc", "")),
            subject=headers.get("subject", ""),
            snippet=str(payload.get("snippet") or ""),
            body_text=extract_body_text(mime_payload, char_limit=body_char_limit),
            attachments=[
                GmailAttachmentMeta(message_id=message_id, **metadata)
                for metadata in attachment_parts(mime_payload)
            ],
            message_id_header=headers.get("message-id", ""),
        )


class GmailThread(BaseModel):
    id: str
    messages: list[GmailMessage] = Field(default_factory=list)

    @classmethod
    def from_api(
        cls,
        payload: dict[str, Any],
        *,
        body_char_limit: int = 20_000,
    ) -> GmailThread:
        messages = [
            GmailMessage.from_api(item, body_char_limit=body_char_limit)
            for item in payload.get("messages") or []
            if isinstance(item, dict)
        ]
        messages.sort(
            key=lambda message: (
                message.date or datetime.min.replace(tzinfo=UTC),
                message.id,
            )
        )
        return cls(id=str(payload.get("id") or ""), messages=messages)


class GmailSearchResult(BaseModel):
    account: str
    messages: list[GmailMessage]
    truncated: bool = False


class GmailThreadResult(BaseModel):
    account: str
    thread: GmailThread


class GmailSentAttachment(BaseModel):
    filename: str
    media_type: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class GmailSendResult(BaseModel):
    account: str
    message_id: str
    thread_id: str
    attachments: list[GmailSentAttachment]


class GmailModifyLabelsResult(BaseModel):
    account: str
    target_kind: Literal["message", "thread"]
    target_id: str
    added_label_ids: list[str]
    removed_label_ids: list[str]


class GmailTrashResult(BaseModel):
    account: str
    target_kind: Literal["message", "thread"]
    target_id: str


class GmailDraftMeta(BaseModel):
    draft_id: str
    message: GmailMessage

    @classmethod
    def from_api(
        cls,
        payload: dict[str, Any],
        *,
        body_char_limit: int = 20_000,
    ) -> GmailDraftMeta:
        message = payload.get("message") or {}
        return cls(
            draft_id=str(payload.get("id") or ""),
            message=GmailMessage.from_api(message, body_char_limit=body_char_limit),
        )


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in payload.get("headers") or []:
        name = str(item.get("name") or "").lower()
        if name and name not in headers:
            headers[name] = str(item.get("value") or "")
    return headers


def _message_date(payload: dict[str, Any], headers: dict[str, str]) -> datetime | None:
    internal = payload.get("internalDate")
    if internal is not None:
        try:
            return datetime.fromtimestamp(int(str(internal)) / 1000, tz=UTC)
        except (TypeError, ValueError, OSError):
            pass
    raw_header = headers.get("date")
    if not raw_header:
        return None
    try:
        parsed = parsedate_to_datetime(raw_header)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _address_list(value: str) -> list[str]:
    addresses: list[str] = []
    for name, address in getaddresses([value]):
        if not address:
            continue
        addresses.append(f"{name} <{address}>" if name else address)
    return addresses
