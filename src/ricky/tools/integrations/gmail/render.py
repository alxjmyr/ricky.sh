"""Deterministic, compact rendering for Gmail tool results."""

from __future__ import annotations

from collections.abc import Mapping

from ricky.tools.integrations.gmail.types import (
    GmailDraftMeta,
    GmailLabel,
    GmailMessage,
    GmailThread,
)
from ricky.tools.integrations.text import format_size


def render_search_results(
    account: str,
    messages: list[GmailMessage],
    labels: Mapping[str, str],
    *,
    truncated: bool = False,
) -> str:
    if not messages:
        return f"[{account}] No matching messages."
    blocks = [
        _render_message(account, message, labels, body=message.snippet) for message in messages
    ]
    if truncated:
        blocks.append(f"[{account}] [results truncated at the pagination cap]")
    return "\n".join(blocks)


def render_message(
    account: str,
    message: GmailMessage,
    labels: Mapping[str, str],
) -> str:
    return _render_message(account, message, labels, body=message.body_text)


def render_thread(
    account: str,
    thread: GmailThread,
    labels: Mapping[str, str],
) -> str:
    heading = f"[{account}] Thread {thread.id} ({len(thread.messages)} message(s))"
    if not thread.messages:
        return f"{heading}\n  [empty thread]"
    return "\n".join(
        [heading, *(_render_message(account, message, labels) for message in thread.messages)]
    )


def render_labels(account: str, labels: list[GmailLabel]) -> str:
    if not labels:
        return f"[{account}] No labels."
    return "\n".join(
        f"[{account}] {label.name} (id {label.id}, {label.kind})"
        for label in sorted(labels, key=lambda item: (item.kind, item.name.casefold()))
    )


def render_drafts(
    account: str,
    drafts: list[GmailDraftMeta],
    labels: Mapping[str, str],
    *,
    truncated: bool = False,
) -> str:
    if not drafts:
        return f"[{account}] No drafts."
    blocks = [
        f"[{account}] Draft {draft.draft_id}\n"
        f"{_render_message(account, draft.message, labels, body=draft.message.snippet)}"
        for draft in drafts
    ]
    if truncated:
        blocks.append(f"[{account}] [draft list truncated at the pagination cap]")
    return "\n".join(blocks)


def _render_message(
    account: str,
    message: GmailMessage,
    labels: Mapping[str, str],
    *,
    body: str | None = None,
) -> str:
    date = (
        message.date.strftime("%Y-%m-%d %H:%M UTC") if message.date is not None else "unknown date"
    )
    recipients = ", ".join(message.to) or "[none]"
    resolved_labels = ", ".join(labels.get(value, value) for value in message.label_ids)
    label_text = resolved_labels or "none"
    lines = [
        f"[{account}] [{date}] From: {message.from_addr or '[unknown]'}  To: {recipients}",
        (
            f"  Subject: {message.subject or '(no subject)'}  "
            f"(message {message.id}, thread {message.thread_id}, labels: {label_text})"
        ),
    ]
    shown_body = message.body_text if body is None else body
    if shown_body:
        lines.extend(f"  {line}" if line else "  " for line in shown_body.splitlines())
    for attachment in message.attachments:
        attachment_id = attachment.attachment_id or "[inline; no download id]"
        lines.append(
            f"  [attachment: {attachment.filename} · {attachment.mime_type} · "
            f"{_format_size(attachment.size_bytes)} · id {attachment_id}]"
        )
    return "\n".join(lines)


def _format_size(value: int) -> str:
    return format_size(value)
