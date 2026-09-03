"""Offline tests for Gmail tools, previews, and attachment confinement."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Collection
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from ricky.agent import AgentSession
from ricky.attachments import AttachmentInput, PreparedAttachmentEffect
from ricky.config import (
    GmailSettings,
    GoogleAccountSettings,
    GoogleOAuthClientSettings,
    GoogleSettings,
    RickySettings,
)
from ricky.tools import ToolContext, ToolRegistry
from ricky.tools.integrations.gmail import GmailToolset, gmail_toolset
from ricky.tools.integrations.gmail.client import GmailClient, GmailError
from ricky.tools.integrations.gmail.mime import decode_base64url
from ricky.tools.integrations.gmail.tools import (
    GmailCreateDraftTool,
    GmailCreateLabelParams,
    GmailCreateLabelTool,
    GmailDownloadAttachmentParams,
    GmailDownloadAttachmentTool,
    GmailListDraftsParams,
    GmailListDraftsTool,
    GmailListLabelsParams,
    GmailListLabelsTool,
    GmailModifyLabelsParams,
    GmailModifyLabelsTool,
    GmailReadMessageParams,
    GmailReadMessageTool,
    GmailReadThreadParams,
    GmailReadThreadTool,
    GmailSearchParams,
    GmailSearchTool,
    GmailSendMessageTool,
    GmailTrashParams,
    GmailTrashTool,
    OutboundMessageParams,
)
from ricky.tools.integrations.gmail.types import GmailSendResult
from ricky.tools.integrations.google import GoogleAuthError

BASE = "https://gmail.test"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"


def test_gmail_account_schema_requires_an_exact_profile_qualified_id() -> None:
    for params in (GmailSearchParams, OutboundMessageParams, GmailTrashParams):
        description = params.model_json_schema()["properties"]["account"]["description"]
        assert "Exact profile-qualified Google account id" in description
        assert "personal/personal" in description


def _encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _message(
    *,
    message_id: str = "m1",
    thread_id: str = "t1",
    body: str = "Plain body",
    subject: str = "Planning",
    attachment: bool = True,
) -> dict[str, Any]:
    parts: list[dict[str, Any]] = [
        {
            "mimeType": "text/plain",
            "body": {"data": _encoded(body.encode())},
        }
    ]
    if attachment:
        parts.append(
            {
                "mimeType": "application/pdf",
                "filename": "report.pdf",
                "body": {"attachmentId": "att-1", "size": 4},
            }
        )
    return {
        "id": message_id,
        "threadId": thread_id,
        "labelIds": ["INBOX", "Label_1"],
        "internalDate": "1784488920000",
        "snippet": "Plain body snippet",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "Dana <dana@example.com>"},
                {"name": "To", "value": "alex@example.com"},
                {"name": "Subject", "value": subject},
                {"name": "Message-ID", "value": f"<{message_id}@example.com>"},
            ],
            "parts": parts,
        },
    }


class FakeAuth:
    def __init__(self) -> None:
        self.identities = {
            "personal": GoogleAccountSettings(email="personal@example.com"),
            "work": GoogleAccountSettings(email="work@example.com"),
            "personal/personal": GoogleAccountSettings(email="personal@example.com"),
        }

    def validate_account(self, account: str) -> GoogleAccountSettings:
        identity = self.identities.get(account)
        if identity is None:
            raise GoogleAuthError(
                f"unknown Google account {account!r}; configured accounts: personal, work"
            )
        return identity

    async def get_access_token(
        self,
        account: str,
        *,
        force_refresh: bool = False,
        required_scopes: Collection[str] | None = None,
    ) -> str:
        del force_refresh
        self.validate_account(account)
        return f"{account}-access"


class FakeGmail:
    def __init__(self, routes: dict[tuple[str, str], Any] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any], Any]] = []
        self.routes: dict[tuple[str, str], Any] = {
            ("GET", "labels"): httpx.Response(
                200,
                json={
                    "labels": [
                        {"id": "INBOX", "name": "INBOX", "type": "system"},
                        {"id": "UNREAD", "name": "UNREAD", "type": "system"},
                        {"id": "SENT", "name": "SENT", "type": "system"},
                        {"id": "DRAFT", "name": "DRAFT", "type": "system"},
                        {"id": "Label_1", "name": "Q3 Planning", "type": "user"},
                    ]
                },
            ),
            **(routes or {}),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("/gmail/v1/users/me/", 1)[1]
        query: dict[str, Any] = dict(request.url.params)
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, query, body))
        route = self.routes.get((request.method, path))
        if route is None:
            return httpx.Response(
                404,
                json={
                    "error": {
                        "status": "NOT_FOUND",
                        "errors": [{"reason": f"unrouted:{request.method}:{path}"}],
                    }
                },
            )
        if callable(route):
            return cast(httpx.Response, route(request))
        if isinstance(route, list):
            return route.pop(0)
        return cast(httpx.Response, route)

    def client(self) -> GmailClient:
        return GmailClient(
            auth=FakeAuth(),
            base_url=BASE,
            timeout_seconds=5,
            transport=httpx.MockTransport(self.handler),
        )

    def count(self, method: str, path: str) -> int:
        return sum(1 for item in self.calls if item[:2] == (method, path))


def _settings(**gmail_overrides: Any) -> RickySettings:
    return RickySettings(
        gmail=GmailSettings(api_base_url=BASE, **gmail_overrides),
    )


def _ctx(tmp_path: Path, **gmail_overrides: Any) -> ToolContext:
    settings = _settings(**gmail_overrides)
    return ToolContext(
        cwd=tmp_path,
        settings=settings,
        session=AgentSession.create(settings, profile_scope=settings.resolve_profile_scope()),
    )


async def test_search_resolves_exact_label_fetches_metadata_and_renders_ids(
    tmp_path: Path,
) -> None:
    fake = FakeGmail(
        {
            ("GET", "messages"): httpx.Response(
                200, json={"messages": [{"id": "m1", "threadId": "t1"}]}
            ),
            ("GET", "messages/m1"): httpx.Response(200, json=_message()),
        }
    )
    tool = GmailSearchTool(fake.client())

    result = await tool.run(
        GmailSearchParams(
            account="work",
            query="from:dana newer_than:7d",
            label="q3 planning",
            max_results=5,
        ),
        _ctx(tmp_path),
    )

    assert not result.is_error
    assert "[work]" in result.content
    assert "message m1, thread t1" in result.content
    search_call = next(call for call in fake.calls if call[1] == "messages")
    assert search_call[2]["q"] == "from:dana newer_than:7d"
    assert search_call[2]["labelIds"] == "Label_1"


async def test_search_unknown_label_is_exact_only(tmp_path: Path) -> None:
    fake = FakeGmail()
    tool = GmailSearchTool(fake.client())

    with pytest.raises(Exception, match="close matches: Q3 Planning"):
        await tool.run(
            GmailSearchParams(account="work", query="x", label="q3 plan"),
            _ctx(tmp_path),
        )
    assert fake.count("GET", "messages") == 0


async def test_read_message_and_thread_render_full_bodies(tmp_path: Path) -> None:
    fake = FakeGmail(
        {
            ("GET", "messages/m1"): httpx.Response(
                200, json=_message(body="Complete message body")
            ),
            ("GET", "threads/t1"): httpx.Response(
                200,
                json={
                    "id": "t1",
                    "messages": [
                        _message(message_id="m2", body="Second"),
                        _message(message_id="m1", body="First"),
                    ],
                },
            ),
        }
    )
    client = fake.client()

    message = await GmailReadMessageTool(client).run(
        GmailReadMessageParams(account="work", message_id="m1"),
        _ctx(tmp_path),
    )
    thread = await GmailReadThreadTool(client).run(
        GmailReadThreadParams(account="work", thread_id="t1"),
        _ctx(tmp_path),
    )

    assert "Complete message body" in message.content
    assert "[work] Thread t1 (2 message(s))" in thread.content
    assert "First" in thread.content and "Second" in thread.content


async def test_list_labels_and_drafts(tmp_path: Path) -> None:
    fake = FakeGmail(
        {
            ("GET", "drafts"): httpx.Response(
                200, json={"drafts": [{"id": "d1", "message": {"id": "m1"}}]}
            ),
            ("GET", "drafts/d1"): httpx.Response(200, json={"id": "d1", "message": _message()}),
        }
    )
    client = fake.client()

    labels = await GmailListLabelsTool(client).run(
        GmailListLabelsParams(account="personal"),
        _ctx(tmp_path),
    )
    drafts = await GmailListDraftsTool(client).run(
        GmailListDraftsParams(account="personal", max_results=10),
        _ctx(tmp_path),
    )

    assert "[personal] Q3 Planning (id Label_1, user)" in labels.content
    assert "[personal] Draft d1" in drafts.content


@pytest.mark.parametrize(
    ("tool_type", "expected_path", "nested"),
    [
        (GmailSendMessageTool, "messages/send", False),
        (GmailCreateDraftTool, "drafts", True),
    ],
)
async def test_outbound_tools_build_threaded_mime_and_show_complete_preview(
    tmp_path: Path,
    tool_type: type,
    expected_path: str,
    nested: bool,
) -> None:
    captured_body: dict[str, Any] = {}

    def mutate(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content))
        if nested:
            return httpx.Response(
                200,
                json={"id": "d1", "message": {"id": "new", "threadId": "t1"}},
            )
        return httpx.Response(200, json={"id": "new", "threadId": "t1"})

    fake = FakeGmail(
        {
            ("GET", "messages/m1"): httpx.Response(200, json=_message()),
            ("POST", expected_path): mutate,
        }
    )
    tool = tool_type(fake.client())
    params = OutboundMessageParams(
        account="work",
        to=["Dana <dana@example.com>"],
        cc=["sam@example.com"],
        subject=None,
        body="Complete reply body\nSecond line",
        reply_to_message_id="m1",
    )
    preview = tool.summarize_permission(params.model_dump(), _ctx(tmp_path))

    result = await tool.run(params, _ctx(tmp_path))

    assert "account: work" in preview
    assert "Dana <dana@example.com>" in preview
    assert "Complete reply body\nSecond line" in preview
    assert "Reply target message: m1" in preview
    api_message = captured_body["message"] if nested else captured_body
    parsed = decode_base64url(api_message["raw"]).decode("utf-8", errors="replace")
    assert "From: work@example.com" in parsed
    assert "Subject: Re: Planning" in parsed
    assert "In-Reply-To: <m1@example.com>" in parsed
    assert api_message["threadId"] == "t1"
    assert f"[work] {'Created Gmail draft' if nested else 'Sent Gmail message'}" in result.content
    if tool_type is GmailSendMessageTool:
        assert result.effect_receipt is not None
        assert result.effect_receipt.disposition == "performed"
        assert result.effect_receipt.provider_reference == "new"
        first = tool.effect_identity(params.model_dump(), _ctx(tmp_path))
        second = tool.effect_identity(params.model_dump(), _ctx(tmp_path))
        changed = tool.effect_identity(
            params.model_copy(update={"body": "Different body"}).model_dump(),
            _ctx(tmp_path),
        )
        assert first == second
        assert first.action_key != changed.action_key


async def test_send_message_builds_multipart_attachment_from_confined_file(
    tmp_path: Path,
) -> None:
    attachment = tmp_path / "values-map.html"
    attachment.write_text("<html>values</html>")
    captured: dict[str, Any] = {}

    def sent(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "sent-1", "threadId": "thread-1"})

    fake = FakeGmail({("POST", "messages/send"): sent})
    tool = GmailSendMessageTool(fake.client())
    args = OutboundMessageParams(
        account="personal",
        to=["recipient@example.com"],
        subject="Values map",
        body="Attached.",
        attachments=[AttachmentInput(path="values-map.html")],
    )

    result = await tool.run(args, _ctx(tmp_path))

    message = BytesParser(policy=policy.default).parsebytes(decode_base64url(captured["raw"]))
    parts = list(message.iter_attachments())
    assert not result.is_error
    assert len(parts) == 1
    assert parts[0].get_filename() == "values-map.html"
    assert parts[0].get_content_type() == "text/html"
    assert parts[0].get_payload(decode=True) == b"<html>values</html>"
    preview = tool.summarize_permission(args.model_dump(mode="python"), _ctx(tmp_path))
    assert "values-map.html" in preview


@pytest.mark.parametrize(
    ("tool_type", "expected_path", "nested"),
    [
        (GmailSendMessageTool, "messages/send", False),
        (GmailCreateDraftTool, "drafts", True),
    ],
)
async def test_prepared_gmail_attachment_uses_exact_preflight_bytes_after_source_swap(
    tmp_path: Path,
    tool_type: type,
    expected_path: str,
    nested: bool,
) -> None:
    source = tmp_path / "report.bin"
    source.write_bytes(b"frozen-before-dispatch")
    captured: dict[str, Any] = {}

    def sent(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        if nested:
            return httpx.Response(
                200,
                json={"id": "draft-1", "message": {"id": "message-1"}},
            )
        return httpx.Response(200, json={"id": "message-1"})

    ctx = _ctx(tmp_path)
    tool = tool_type(FakeGmail({("POST", expected_path): sent}).client())
    args = OutboundMessageParams(
        account="personal",
        to=["recipient@example.com"],
        subject="Prepared attachment",
        body="Attached.",
        attachments=[AttachmentInput(path="report.bin")],
    )
    prepared = await tool.prepare_effect(args.model_dump(mode="python"), ctx)
    restored = PreparedAttachmentEffect.model_validate_json(prepared.model_dump_json())

    source.write_bytes(b"changed-after-preflight")
    changed_identity = tool.effect_identity(args.model_dump(mode="python"), ctx)
    result = await tool.run_prepared(args, restored, ctx)

    api_message = captured["message"] if nested else captured
    message = BytesParser(policy=policy.default).parsebytes(decode_base64url(api_message["raw"]))
    [part] = list(message.iter_attachments())
    assert not result.is_error
    assert part.get_payload(decode=True) == b"frozen-before-dispatch"
    assert restored.identity.action_key != changed_identity.action_key


async def test_send_message_builds_multipart_attachment_from_logical_task_reference(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    user = tmp_path / "user"
    project.mkdir()
    task_id = "task_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    source = user / "profiles" / "personal" / "tasks" / "artifacts" / task_id / "values-map.html"
    source.parent.mkdir(parents=True)
    source.write_text("<html>logical values</html>")
    captured: dict[str, Any] = {}

    def sent(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "sent-2", "threadId": "thread-2"})

    settings = RickySettings(
        user_data_dir=str(user),
        gmail=GmailSettings(api_base_url=BASE),
    )
    ctx = ToolContext(
        cwd=project,
        settings=settings,
        session=AgentSession.create(settings, profile_scope=settings.resolve_profile_scope()),
    )
    tool = GmailSendMessageTool(FakeGmail({("POST", "messages/send"): sent}).client())
    result = await ToolRegistry([tool]).dispatch(
        "gmail_send_message",
        {
            "account": "personal/personal",
            "to": ["recipient@example.com"],
            "subject": "Values map",
            "body": "Attached.",
            "attachments": [
                json.dumps(
                    {
                        "task_id": task_id,
                        "task_artifact_path": "values-map.html",
                        "profile": "personal",
                    }
                )
            ],
        },
        ctx,
    )

    message = BytesParser(policy=policy.default).parsebytes(decode_base64url(captured["raw"]))
    parts = list(message.iter_attachments())
    evidence = GmailSendResult.model_validate(result.data)
    assert not result.is_error
    assert len(parts) == 1
    assert parts[0].get_filename() == "values-map.html"
    assert parts[0].get_payload(decode=True) == source.read_bytes()
    assert "with 1 attachment(s) (values-map.html)" in result.content
    assert len(evidence.attachments) == 1
    assert evidence.attachments[0].filename == "values-map.html"
    assert evidence.attachments[0].size_bytes == len(source.read_bytes())
    assert evidence.attachments[0].sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert not (project / "values-map.html").exists()


def test_outbound_attachment_string_normalization_remains_strict() -> None:
    registry = ToolRegistry([GmailSendMessageTool(FakeGmail({}).client())])
    base = {
        "account": "personal",
        "to": ["recipient@example.com"],
        "subject": "Values map",
        "body": "Attached.",
    }

    malformed = registry.prepare_args("gmail_send_message", base | {"attachments": ["not JSON"]})
    wrong_shape = registry.prepare_args(
        "gmail_send_message", base | {"attachments": ['["not", "an object"]']}
    )
    incomplete = registry.prepare_args(
        "gmail_send_message",
        base | {"attachments": ['{"task_id":"task_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}']},
    )

    assert malformed.error is not None
    assert wrong_shape.error is not None
    assert incomplete.error is not None


@pytest.mark.parametrize(
    "field,value",
    [
        ("to", ["not-an-address"]),
        ("to", ["a@example.com\nBcc: victim@example.com"]),
        ("cc", ["two@example.com, three@example.com"]),
    ],
)
def test_outbound_recipient_validation(field: str, value: list[str]) -> None:
    payload: dict[str, Any] = {
        "account": "work",
        "to": ["valid@example.com"],
        "body": "body",
        field: value,
    }

    with pytest.raises(ValidationError):
        OutboundMessageParams.model_validate(payload)


def test_send_effect_identity_rejects_unknown_account_before_dispatch(
    tmp_path: Path,
) -> None:
    fake = FakeGmail()
    tool = GmailSendMessageTool(fake.client())
    params = OutboundMessageParams(
        account="personal@example.com",
        to=["recipient@example.com"],
        subject="Subject",
        body="Body",
    )

    with pytest.raises(GoogleAuthError, match="unknown Google account"):
        tool.effect_identity(params.model_dump(), _ctx(tmp_path))

    assert fake.calls == []


async def test_create_label_invalidates_cache(tmp_path: Path) -> None:
    label_lists = 0

    def labels(_request: httpx.Request) -> httpx.Response:
        nonlocal label_lists
        label_lists += 1
        return httpx.Response(
            200,
            json={"labels": [{"id": "Label_1", "name": "Old", "type": "user"}]},
        )

    fake = FakeGmail(
        {
            ("GET", "labels"): labels,
            ("POST", "labels"): httpx.Response(200, json={"id": "Label_2", "name": "New"}),
        }
    )
    client = fake.client()
    await client.labels("work")
    tool = GmailCreateLabelTool(client)

    result = await tool.run(
        GmailCreateLabelParams(account="work", name="New"),
        _ctx(tmp_path),
    )
    await client.labels("work")

    assert "Created Gmail label 'New' (id Label_2)" in result.content
    assert label_lists == 2
    assert "create Gmail label: New" in tool.summarize_permission(
        {"account": "work", "name": "New"}, _ctx(tmp_path)
    )


def test_label_mutation_effect_preflight_rejects_local_errors(tmp_path: Path) -> None:
    fake = FakeGmail()
    ctx = _ctx(tmp_path)

    with pytest.raises(ValueError, match="must not be blank"):
        GmailCreateLabelTool(fake.client()).effect_identity(
            {"account": "work", "name": "   "},
            ctx,
        )
    with pytest.raises(ValueError, match="same labels"):
        GmailModifyLabelsTool(fake.client()).effect_identity(
            {
                "account": "work",
                "message_id": "m1",
                "add_labels": ["Reviewed"],
                "remove_labels": ["reviewed"],
            },
            ctx,
        )
    assert fake.calls == []


async def test_modify_labels_uses_exact_ids_and_supports_archive(tmp_path: Path) -> None:
    fake = FakeGmail(
        {
            ("POST", "threads/t1/modify"): httpx.Response(
                200, json={"id": "t1", "labelIds": ["Label_1"]}
            )
        }
    )
    tool = GmailModifyLabelsTool(fake.client())
    params = GmailModifyLabelsParams(
        account="work",
        thread_id="t1",
        add_labels=["Q3 Planning"],
        remove_labels=["INBOX"],
    )

    result = await tool.run(params, _ctx(tmp_path))

    body = next(call[3] for call in fake.calls if call[1] == "threads/t1/modify")
    assert body == {"addLabelIds": ["Label_1"], "removeLabelIds": ["INBOX"]}
    assert "Modified labels on thread t1" in result.content
    preview = tool.summarize_permission(params.model_dump(), _ctx(tmp_path))
    assert "thread t1" in preview
    assert "Q3 Planning" in preview
    assert "INBOX" in preview


@pytest.mark.parametrize("label", ["SENT", "DRAFT"])
async def test_modify_labels_refuses_non_modifiable_system_labels(
    tmp_path: Path,
    label: str,
) -> None:
    fake = FakeGmail()
    tool = GmailModifyLabelsTool(fake.client())

    with pytest.raises(Exception, match="cannot be manually"):
        await tool.run(
            GmailModifyLabelsParams(
                account="work",
                message_id="m1",
                remove_labels=[label],
            ),
            _ctx(tmp_path),
        )
    assert not any(call[0] == "POST" for call in fake.calls)


def test_modify_and_trash_require_exactly_one_target() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        GmailModifyLabelsParams(
            account="work",
            message_id="m1",
            thread_id="t1",
            add_labels=["INBOX"],
        )
    with pytest.raises(ValidationError, match="exactly one"):
        GmailTrashParams(account="work")


@pytest.mark.parametrize(
    ("message_id", "thread_id", "path"),
    [("m1", None, "messages/m1/trash"), (None, "t1", "threads/t1/trash")],
)
async def test_trash_supports_message_or_thread(
    tmp_path: Path,
    message_id: str | None,
    thread_id: str | None,
    path: str,
) -> None:
    fake = FakeGmail({("POST", path): httpx.Response(200, json={"id": "ok"})})
    tool = GmailTrashTool(fake.client())
    params = GmailTrashParams(
        account="personal",
        message_id=message_id,
        thread_id=thread_id,
    )

    result = await tool.run(params, _ctx(tmp_path))

    assert fake.count("POST", path) == 1
    assert "[personal] Moved" in result.content
    assert "to Trash" in tool.summarize_permission(params.model_dump(), _ctx(tmp_path))


async def test_download_attachment_is_confined_collision_safe_and_binary_exact(
    tmp_path: Path,
) -> None:
    content = b"\x00\x01PDF\xff"
    fake = FakeGmail(
        {
            ("GET", "messages/m1"): httpx.Response(200, json=_message()),
            ("GET", "messages/m1/attachments/att-1"): httpx.Response(
                200, json={"data": _encoded(content), "size": len(content)}
            ),
        }
    )
    tool = GmailDownloadAttachmentTool(fake.client())
    destination = tmp_path / "user-data" / "profiles" / "personal" / "downloads" / "gmail"
    destination.mkdir(parents=True)
    (destination / "m1-report.pdf").write_bytes(b"existing")
    params = GmailDownloadAttachmentParams(
        account="personal/personal",
        message_id="m1",
        attachment_id="att-1",
    )

    preview = tool.summarize_permission(params.model_dump(), _ctx(tmp_path))
    result = await tool.run(params, _ctx(tmp_path))

    written = destination / "m1-report-2.pdf"
    assert written.read_bytes() == content
    assert "m1-<Gmail filename>" in preview
    assert str(written) in result.content
    assert destination.stat().st_mode & 0o777 == 0o700
    assert written.stat().st_mode & 0o777 == 0o600
    assert not list(destination.glob("*.part"))


async def test_download_reconciles_rotated_id_by_unique_filename_and_mime_type(
    tmp_path: Path,
) -> None:
    content = b"fresh attachment"
    message = _message()
    message["payload"]["parts"][1]["body"]["attachmentId"] = "att-current"
    fake = FakeGmail(
        {
            ("GET", "messages/m1"): httpx.Response(200, json=message),
            ("GET", "messages/m1/attachments/att-current"): httpx.Response(
                200,
                json={"data": _encoded(content), "size": len(content)},
            ),
        }
    )
    tool = GmailDownloadAttachmentTool(fake.client())
    params = GmailDownloadAttachmentParams(
        account="personal/personal",
        message_id="m1",
        attachment_id="att-from-prior-read",
        filename="report.pdf",
        mime_type="application/pdf",
    )

    preview = tool.summarize_permission(params.model_dump(), _ctx(tmp_path))
    result = await tool.run(params, _ctx(tmp_path))

    written = (
        tmp_path / "user-data" / "profiles" / "personal" / "downloads" / "gmail" / "m1-report.pdf"
    )
    assert written.read_bytes() == content
    assert "m1-report.pdf" in preview
    assert "m1-report.pdf" in result.content
    assert fake.count("GET", "messages/m1/attachments/att-current") == 1
    assert fake.count("GET", "messages/m1/attachments/att-from-prior-read") == 0


async def test_download_refuses_ambiguous_rotated_attachment_metadata(
    tmp_path: Path,
) -> None:
    message = _message()
    message["payload"]["parts"][1]["body"]["attachmentId"] = "att-current-1"
    message["payload"]["parts"].append(
        {
            "mimeType": "application/pdf",
            "filename": "report.pdf",
            "body": {"attachmentId": "att-current-2", "size": 8},
        }
    )
    fake = FakeGmail(
        {
            ("GET", "messages/m1"): httpx.Response(200, json=message),
        }
    )
    tool = GmailDownloadAttachmentTool(fake.client())
    params = GmailDownloadAttachmentParams(
        account="personal/personal",
        message_id="m1",
        attachment_id="att-from-prior-read",
        filename="report.pdf",
        mime_type="application/pdf",
    )

    with pytest.raises(GmailError, match="matches 2 current parts"):
        await tool.run(params, _ctx(tmp_path))

    assert all(not item[1].startswith("messages/m1/attachments/") for item in fake.calls)


async def test_download_rejects_user_data_escape_before_attachment_fetch_or_write(
    tmp_path: Path,
) -> None:
    fake = FakeGmail({("GET", "messages/m1"): httpx.Response(200, json=_message())})
    tool = GmailDownloadAttachmentTool(fake.client())
    params = GmailDownloadAttachmentParams(
        account="personal/personal",
        message_id="m1",
        attachment_id="att-1",
    )
    ctx = _ctx(tmp_path)
    ctx.settings.gmail.download_dir = "../outside"

    with pytest.raises(GmailError, match="escapes profile data directory"):
        await tool.run(params, ctx)

    assert fake.count("GET", "messages/m1/attachments/att-1") == 0
    assert not (tmp_path / "outside").exists()
    assert "invalid: escapes profile data directory" in tool.summarize_permission(
        params.model_dump(), ctx
    )


async def test_registry_converts_unknown_account_to_model_readable_error(
    tmp_path: Path,
) -> None:
    fake = FakeGmail()
    registry = ToolRegistry([GmailListLabelsTool(fake.client())])

    result = await registry.dispatch(
        "gmail_list_labels",
        {"account": "other"},
        _ctx(tmp_path),
    )

    assert result.is_error
    assert "configured accounts: personal, work" in result.content
    assert fake.calls == []


async def test_toolset_factory_is_conditional_and_registers_complete_pack(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    absent = RickySettings(
        google=GoogleSettings(
            accounts={"personal": GoogleAccountSettings(email="personal@example.com")}
        ),
        google_oauth_clients={},
    )
    assert gmail_toolset(absent, root=tmp_path) is None

    configured = RickySettings(
        google=GoogleSettings(
            accounts={"personal": GoogleAccountSettings(email="personal@example.com")}
        ),
        google_oauth_clients={
            "personal": GoogleOAuthClientSettings(
                client_id="client-id",
                client_secret=SecretStr("client-secret"),
            )
        },
    )
    toolset = gmail_toolset(configured, root=tmp_path)

    assert isinstance(toolset, GmailToolset)
    assert [tool.name for tool in toolset.tools] == [
        "gmail_search",
        "gmail_read_message",
        "gmail_read_thread",
        "gmail_list_labels",
        "gmail_list_drafts",
        "gmail_create_draft",
        "gmail_send_message",
        "gmail_create_label",
        "gmail_modify_labels",
        "gmail_trash",
        "gmail_download_attachment",
    ]
    await toolset.aclose()


async def test_modify_labels_allows_user_label_named_like_system_label(
    tmp_path: Path,
) -> None:
    fake = FakeGmail(
        {
            ("GET", "labels"): httpx.Response(
                200,
                json={
                    "labels": [
                        {"id": "SENT", "name": "SENT", "type": "system"},
                        {"id": "Label_9", "name": "Sent", "type": "user"},
                    ]
                },
            ),
            ("POST", "messages/m1/modify"): httpx.Response(
                200, json={"id": "m1", "labelIds": ["Label_9"]}
            ),
        }
    )
    tool = GmailModifyLabelsTool(fake.client())

    result = await tool.run(
        GmailModifyLabelsParams(
            account="work",
            message_id="m1",
            add_labels=["Label_9"],
        ),
        _ctx(tmp_path),
    )

    body = next(call[3] for call in fake.calls if call[:2] == ("POST", "messages/m1/modify"))
    assert body == {"addLabelIds": ["Label_9"], "removeLabelIds": []}
    assert "Label_9" in result.content


async def test_download_reconciles_rotated_id_by_unique_mime_type_alone(
    tmp_path: Path,
) -> None:
    content = b"fresh attachment"
    message = _message()
    message["payload"]["parts"][1]["body"]["attachmentId"] = "att-current"
    fake = FakeGmail(
        {
            ("GET", "messages/m1"): httpx.Response(200, json=message),
            ("GET", "messages/m1/attachments/att-current"): httpx.Response(
                200,
                json={"data": _encoded(content), "size": len(content)},
            ),
        }
    )
    tool = GmailDownloadAttachmentTool(fake.client())

    result = await tool.run(
        GmailDownloadAttachmentParams(
            account="personal/personal",
            message_id="m1",
            attachment_id="att-from-prior-read",
            mime_type="application/pdf",
        ),
        _ctx(tmp_path),
    )

    written = (
        tmp_path / "user-data" / "profiles" / "personal" / "downloads" / "gmail" / "m1-report.pdf"
    )
    assert written.read_bytes() == content
    assert "m1-report.pdf" in result.content
