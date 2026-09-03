"""Tests for Gmail MIME handling, canonical models, and rendering."""

from __future__ import annotations

import base64
from email import policy
from email.parser import BytesParser
from typing import Any, cast

from ricky.tools.integrations.gmail.mime import (
    build_raw_message,
    decode_base64url,
    extract_body_text,
    strip_html,
)
from ricky.tools.integrations.gmail.render import (
    render_drafts,
    render_labels,
    render_message,
    render_search_results,
    render_thread,
)
from ricky.tools.integrations.gmail.types import (
    GmailDraftMeta,
    GmailLabel,
    GmailMessage,
    GmailThread,
)


def _encoded(value: str, encoding: str = "utf-8") -> str:
    return base64.urlsafe_b64encode(value.encode(encoding)).decode().rstrip("=")


def _message(
    *,
    message_id: str = "m1",
    thread_id: str = "t1",
    internal_date: str = "1784488920000",
    body: str = "Plain body",
) -> dict[str, object]:
    return {
        "id": message_id,
        "threadId": thread_id,
        "labelIds": ["INBOX", "Label_1"],
        "internalDate": internal_date,
        "snippet": "Plain body snippet",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "Dana Doe <dana@example.com>"},
                {"name": "To", "value": "Alex <alex@example.com>, team@example.com"},
                {"name": "Cc", "value": "Sam <sam@example.com>"},
                {"name": "Subject", "value": "Q3 planning"},
                {"name": "Message-ID", "value": "<m1@example.com>"},
            ],
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "text/html",
                            "body": {"data": _encoded("<p>HTML body</p>")},
                        },
                        {
                            "mimeType": "text/plain",
                            "headers": [
                                {
                                    "name": "Content-Type",
                                    "value": "text/plain; charset=utf-8",
                                }
                            ],
                            "body": {"data": _encoded(body)},
                        },
                    ],
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "q3 plan.pdf",
                    "body": {"attachmentId": "att-1", "size": 2_200_000},
                },
            ],
        },
    }


def test_decode_base64url_accepts_missing_padding() -> None:
    assert decode_base64url("aGVsbG8") == b"hello"


def test_inbound_prefers_plain_and_walks_nested_parts() -> None:
    payload = cast(dict[str, Any], _message()["payload"])

    text = extract_body_text(payload, char_limit=20_000)

    assert text == "Plain body"


def test_html_only_is_stripped_with_blocks_and_entities() -> None:
    payload = {
        "mimeType": "text/html",
        "body": {"data": _encoded("<h1>Hello &amp; goodbye</h1><p>One<br>Two</p><div>Three</div>")},
    }

    assert strip_html("<p>A &amp; B</p>") == "A & B"
    assert extract_body_text(payload, char_limit=20_000) == ("Hello & goodbye\nOne\nTwo\nThree")


def test_inbound_respects_declared_charset_and_body_cap() -> None:
    payload = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Type", "value": "text/plain; charset=iso-8859-1"}],
        "body": {"data": _encoded("café" * 400, "iso-8859-1")},
    }

    text = extract_body_text(payload, char_limit=60)

    assert text.startswith("cafécafé")
    assert text.endswith("[... body truncated at 60 chars]")
    assert len(text) == 60


def test_outbound_new_message_round_trips_through_email_parser() -> None:
    payload = build_raw_message(
        from_addr="alex@example.com",
        to=["dana@example.com"],
        cc=["sam@example.com"],
        subject="Hello ✓",
        body="Unicode body: café",
    )

    parsed = BytesParser(policy=policy.default).parsebytes(decode_base64url(payload["raw"]))

    assert parsed["From"] == "alex@example.com"
    assert parsed["To"] == "dana@example.com"
    assert parsed["Cc"] == "sam@example.com"
    assert parsed["Subject"] == "Hello ✓"
    assert parsed.get_content().strip() == "Unicode body: café"
    assert "threadId" not in payload


def test_outbound_reply_adds_threading_headers_and_derives_subject() -> None:
    payload = build_raw_message(
        from_addr="alex@example.com",
        to=["dana@example.com"],
        cc=[],
        subject=None,
        body="Reply body",
        reply_message_id="<original@example.com>",
        reply_subject="Planning",
        thread_id="thread-1",
    )

    parsed = BytesParser(policy=policy.default).parsebytes(decode_base64url(payload["raw"]))

    assert payload["threadId"] == "thread-1"
    assert parsed["Subject"] == "Re: Planning"
    assert parsed["In-Reply-To"] == "<original@example.com>"
    assert parsed["References"] == "<original@example.com>"


def test_message_from_api_parses_headers_body_attachments_and_json_round_trip() -> None:
    message = GmailMessage.from_api(_message())

    assert message.id == "m1"
    assert message.thread_id == "t1"
    assert message.date is not None
    assert message.date.isoformat() == "2026-07-19T19:22:00+00:00"
    assert message.from_addr == "Dana Doe <dana@example.com>"
    assert message.to == ["Alex <alex@example.com>", "team@example.com"]
    assert message.cc == ["Sam <sam@example.com>"]
    assert message.subject == "Q3 planning"
    assert message.body_text == "Plain body"
    assert message.message_id_header == "<m1@example.com>"
    assert message.attachments[0].filename == "q3 plan.pdf"
    assert message.attachments[0].attachment_id == "att-1"
    assert GmailMessage.model_validate_json(message.model_dump_json()) == message


def test_message_date_falls_back_to_rfc_header() -> None:
    payload = _message()
    payload["internalDate"] = "bad"
    mime_payload = cast(dict[str, Any], payload["payload"])
    mime_payload["headers"].append({"name": "Date", "value": "Sat, 19 Jul 2026 15:42:00 -0500"})

    message = GmailMessage.from_api(payload)

    assert message.date is not None
    assert message.date.isoformat() == "2026-07-19T20:42:00+00:00"


def test_thread_orders_messages_by_internal_date() -> None:
    thread = GmailThread.from_api(
        {
            "id": "t1",
            "messages": [
                _message(message_id="late", internal_date="1784488980000"),
                _message(message_id="early", internal_date="1784488920000"),
            ],
        }
    )

    assert [message.id for message in thread.messages] == ["early", "late"]
    assert GmailThread.model_validate_json(thread.model_dump_json()) == thread


def test_rendering_keeps_account_ids_labels_and_attachment_metadata() -> None:
    message = GmailMessage.from_api(_message())
    labels = {"INBOX": "INBOX", "Label_1": "q3"}
    rendered = render_message("work", message, labels)

    assert rendered == (
        "[work] [2026-07-19 19:22 UTC] From: Dana Doe <dana@example.com>  "
        "To: Alex <alex@example.com>, team@example.com\n"
        "  Subject: Q3 planning  (message m1, thread t1, labels: INBOX, q3)\n"
        "  Plain body\n"
        "  [attachment: q3 plan.pdf · application/pdf · 2.1MB · id att-1]"
    )

    search = render_search_results("work", [message], labels, truncated=True)
    assert "Plain body snippet" in search
    assert "results truncated" in search

    thread = render_thread("work", GmailThread(id="t1", messages=[message]), labels)
    assert thread.startswith("[work] Thread t1 (1 message(s))")
    assert "(message m1, thread t1" in thread


def test_label_and_draft_rendering() -> None:
    labels = [
        GmailLabel(id="INBOX", name="INBOX", kind="system"),
        GmailLabel(id="Label_1", name="q3", kind="user"),
    ]
    message = GmailMessage.from_api(_message())
    draft = GmailDraftMeta(draft_id="d1", message=message)

    rendered_labels = render_labels("personal", labels)
    rendered_drafts = render_drafts(
        "personal",
        [draft],
        {label.id: label.name for label in labels},
        truncated=True,
    )

    assert "[personal] INBOX (id INBOX, system)" in rendered_labels
    assert "[personal] q3 (id Label_1, user)" in rendered_labels
    assert "[personal] Draft d1" in rendered_drafts
    assert "draft list truncated" in rendered_drafts
