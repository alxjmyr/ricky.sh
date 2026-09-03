"""Exact route and mutating-authority tests for notify_user."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from ricky.agent import AgentSession
from ricky.attachments import AttachmentInput
from ricky.config import MessagingSettings, RickySettings
from ricky.notifications import NotificationService
from ricky.notifications.tools import NotifyUserParams, NotifyUserTool
from ricky.permissions import PermissionEngine, Policy, PolicyRule
from ricky.profiles import ProfileScope
from ricky.tools import ToolContext


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings(
        user_data_dir=str(tmp_path / "user"),
        messaging=MessagingSettings.model_validate(
            {
                "transports": {
                    "main": {"type": "telegram", "account": "personal/owner-bot"},
                },
                "telegram_accounts": {
                    "personal/owner-bot": {"bot_token": "test-token"},
                },
                "routes": {
                    "owner": {
                        "transport": "main",
                        "destination": "chat-owner",
                        "owner_profile": "personal",
                        "accepted_profiles": ["shared", "personal"],
                    },
                    "personal-alerts": {
                        "transport": "main",
                        "destination": "chat-alerts",
                        "owner_profile": "personal",
                        "accepted_profiles": ["shared", "personal"],
                    },
                },
                "agent_routes": ["owner"],
            }
        ),
    )


async def test_notify_user_requires_exact_allowed_route_and_mutating_authority(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    session = AgentSession.create(settings, profile_scope=ProfileScope.create("personal"))
    ctx = ToolContext(cwd=tmp_path, settings=settings, session=session)
    service = NotificationService(settings)
    tool = NotifyUserTool(service, allowed_routes={"owner"})
    args: dict[str, object] = {
        "route": "owner",
        "title": "Question",
        "body": "Please review the task.",
        "occurrence_key": "task-1-revision-3",
    }

    default = PermissionEngine().decide(
        session,
        tool_name=tool.name,
        risk=tool.risk,
        params=tool.normalize_permission_args(args, ctx),
    )
    assert default.decision == "ask"
    allowed = PermissionEngine(
        Policy(
            rules=[
                PolicyRule(
                    tool_name="notify_user",
                    params_equal={"route": "owner"},
                    decision="allow",
                    reason="exact route approved",
                )
            ]
        )
    ).decide(
        session,
        tool_name=tool.name,
        risk=tool.risk,
        params=tool.normalize_permission_args(args, ctx),
    )
    assert allowed.decision == "allow"

    result = await tool.run(NotifyUserParams.model_validate(args), ctx)
    assert not result.is_error
    assert result.effect_receipt is not None
    assert result.effect_receipt.disposition == "performed"
    [record] = await service.store.list(scope=session.profile_scope, limit=10)
    assert record.request.profile_label == session.profile_scope.label()
    assert record.request.body_format == "portable_markdown_v1"

    denied = await tool.run(
        NotifyUserParams.model_validate(args | {"route": "personal-alerts"}),
        ctx,
    )
    assert denied.is_error
    assert "not allowed" in denied.content


def test_notify_user_schema_describes_mobile_portable_markdown() -> None:
    schema = NotifyUserParams.model_json_schema()

    assert schema["properties"]["title"]["description"] == "Plain-text notification title."
    body_help = schema["properties"]["body"]["description"]
    assert "Mobile-first portable Markdown" in body_help
    assert "raw HTML" in body_help
    assert "Markdown images" in body_help


async def test_notify_user_snapshots_attachment_below_user_data(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    source = project / "artifact.html"
    source.write_text("<html>artifact</html>")
    session = AgentSession.create(settings, profile_scope=ProfileScope.create("personal"))
    ctx = ToolContext(cwd=project, settings=settings, session=session)
    service = NotificationService(settings)
    tool = NotifyUserTool(service, allowed_routes={"owner"})

    result = await tool.run(
        NotifyUserParams(
            route="owner",
            body="Attached.",
            occurrence_key="artifact-1",
            attachments=[AttachmentInput(path="artifact.html")],
        ),
        ctx,
    )

    records = await service.store.list(scope=session.profile_scope, limit=10)
    assert not result.is_error
    assert len(records[0].request.attachments) == 1
    stored = records[0].request.attachments[0]
    assert (Path(settings.user_data_dir) / stored.storage_path).read_text() == source.read_text()
    assert list(project.iterdir()) == [source]


async def test_prepared_notification_uses_exact_preflight_bytes_after_source_swap(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    source = project / "report.bin"
    source.write_bytes(b"frozen-before-dispatch")
    session = AgentSession.create(settings, profile_scope=ProfileScope.create("personal"))
    ctx = ToolContext(cwd=project, settings=settings, session=session)
    service = NotificationService(settings)
    tool = NotifyUserTool(service, allowed_routes={"owner"})
    args = NotifyUserParams(
        route="owner",
        body="Attached.",
        occurrence_key="prepared-attachment-1",
        attachments=[AttachmentInput(path="report.bin")],
    )
    prepared = await tool.prepare_effect(args.model_dump(mode="python"), ctx)

    source.write_bytes(b"changed-after-preflight")
    changed_identity = tool.effect_identity(args.model_dump(mode="python"), ctx)
    result = await tool.run_prepared(args, prepared, ctx)

    [record] = await service.store.list(scope=session.profile_scope, limit=10)
    [stored] = record.request.attachments
    assert not result.is_error
    assert (Path(settings.user_data_dir) / stored.storage_path).read_bytes() == (
        b"frozen-before-dispatch"
    )
    assert prepared.identity.action_key != changed_identity.action_key


def test_notify_user_rejects_raw_destinations_and_has_stable_effect_identity(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    session = AgentSession.create(settings, profile_scope=ProfileScope.create("personal"))
    ctx = ToolContext(cwd=tmp_path, settings=settings, session=session)
    tool = NotifyUserTool(NotificationService(settings), allowed_routes={"owner"})
    args: dict[str, object] = {
        "route": "owner",
        "body": "Update",
        "occurrence_key": "one",
    }
    try:
        NotifyUserParams.model_validate(args | {"chat_id": "raw"})
    except ValidationError:
        pass
    else:
        raise AssertionError("raw destination input was accepted")
    assert tool.effect_identity(args, ctx) == tool.effect_identity(args, ctx)
    assert tool.permission_scope(args, ctx).params_equal == {"route": "owner"}
