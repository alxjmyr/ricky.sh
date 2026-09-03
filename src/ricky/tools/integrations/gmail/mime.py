"""Deterministic inbound and outbound MIME handling for Gmail."""

from __future__ import annotations

import base64
import re
from email.message import EmailMessage
from email.policy import SMTP
from html.parser import HTMLParser
from typing import Any, TypedDict

from ricky.attachments import LoadedAttachment
from ricky.tools.integrations.text import cap_text

_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "div",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tr",
    "ul",
}
_CHARSET = re.compile(r"charset\s*=\s*[\"']?([^;\s\"']+)", re.IGNORECASE)


def decode_base64url(value: str) -> bytes:
    """Decode Gmail's unpadded base64url wire representation."""
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def extract_body_text(payload: dict[str, Any], *, char_limit: int) -> str:
    """Prefer the first plain body, falling back to deterministic HTML text."""
    plain: str | None = None
    html: str | None = None
    for part in _walk_parts(payload):
        filename = str(part.get("filename") or "")
        if filename:
            continue
        mime_type = str(part.get("mimeType") or "").lower()
        body = part.get("body") or {}
        encoded = body.get("data")
        if not isinstance(encoded, str) or not encoded:
            continue
        text = _decode_part_text(encoded, part)
        if mime_type == "text/plain" and plain is None:
            plain = text
        elif mime_type == "text/html" and html is None:
            html = strip_html(text)
    selected = plain if plain is not None else html or ""
    return _cap_body(selected.strip(), char_limit)


class AttachmentPart(TypedDict):
    attachment_id: str
    filename: str
    mime_type: str
    size_bytes: int


def attachment_parts(payload: dict[str, Any]) -> list[AttachmentPart]:
    """Return normalized attachment metadata from a Gmail payload tree."""
    attachments: list[AttachmentPart] = []
    for part in _walk_parts(payload):
        filename = str(part.get("filename") or "")
        if not filename:
            continue
        body = part.get("body") or {}
        attachments.append(
            {
                "attachment_id": str(body.get("attachmentId") or ""),
                "filename": filename,
                "mime_type": str(part.get("mimeType") or "application/octet-stream"),
                "size_bytes": _integer(body.get("size")),
            }
        )
    return attachments


def build_raw_message(
    *,
    from_addr: str,
    to: list[str],
    cc: list[str],
    subject: str | None,
    body: str,
    attachments: list[LoadedAttachment] | None = None,
    reply_message_id: str | None = None,
    reply_subject: str | None = None,
    thread_id: str | None = None,
) -> dict[str, str]:
    """Build a UTF-8 text message encoded for Gmail send/draft endpoints."""
    message = EmailMessage()
    message["From"] = from_addr
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)

    resolved_subject = subject
    if reply_message_id is not None and resolved_subject is None:
        original = reply_subject or ""
        resolved_subject = original if original.lower().startswith("re:") else f"Re: {original}"
    message["Subject"] = resolved_subject or ""
    if reply_message_id:
        message["In-Reply-To"] = reply_message_id
        message["References"] = reply_message_id
    message.set_content(body, subtype="plain", charset="utf-8")
    for attachment in attachments or []:
        maintype, subtype = attachment.media_type.split("/", 1)
        message.add_attachment(
            attachment.content,
            maintype=maintype,
            subtype=subtype,
            filename=attachment.filename,
        )

    raw = base64.urlsafe_b64encode(message.as_bytes(policy=SMTP)).decode("ascii").rstrip("=")
    payload = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    return payload


def strip_html(value: str) -> str:
    """Drop HTML tags while retaining compact block-level line structure."""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return _normalize_lines("".join(parser.parts))


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _walk_parts(payload: dict[str, Any]):
    yield payload
    for part in payload.get("parts") or []:
        if isinstance(part, dict):
            yield from _walk_parts(part)


def _decode_part_text(encoded: str, part: dict[str, Any]) -> str:
    data = decode_base64url(encoded)
    charset = "utf-8"
    for header in part.get("headers") or []:
        if str(header.get("name") or "").lower() != "content-type":
            continue
        match = _CHARSET.search(str(header.get("value") or ""))
        if match is not None:
            charset = match.group(1)
            break
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _cap_body(value: str, limit: int) -> str:
    return cap_text(value, limit, label="body")


def _normalize_lines(value: str) -> str:
    lines = [" ".join(line.split()) for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _integer(value: object) -> int:
    if not isinstance(value, str | int | float):
        return 0
    try:
        return int(value)
    except ValueError:
        return 0
