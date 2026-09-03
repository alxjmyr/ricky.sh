"""Tests for tools, registry dispatch, and permission policy."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel, ConfigDict

from ricky.agent import AgentSession, PermissionGrant
from ricky.config import RickySettings
from ricky.permissions import PermissionEngine, Policy, PolicyRule
from ricky.tools import (
    EffectIdentity,
    Risk,
    ToolContext,
    ToolRegistry,
    ToolResult,
)
from ricky.tools.builtin import (
    EditFileTool,
    GlobSearchTool,
    GrepSearchTool,
    ListDirTool,
    ReadFileTool,
    RunShellTool,
    UpdateTasksTool,
    WriteFileTool,
)


def _ctx(tmp_path: Path) -> ToolContext:
    settings = RickySettings(shell_timeout_seconds=2)
    return ToolContext(
        cwd=tmp_path,
        settings=settings,
        session=AgentSession.create(settings, profile_scope=settings.resolve_profile_scope()),
    )


@pytest.mark.asyncio
async def test_file_tools_support_workspace_and_external_host_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "note.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("external", encoding="utf-8")
    registry = ToolRegistry([ReadFileTool(), ListDirTool(), GlobSearchTool(), GrepSearchTool()])
    ctx = _ctx(project)

    read = await registry.dispatch("read_file", {"path": "note.txt", "offset": 2}, ctx)
    listing = await registry.dispatch("list_dir", {"path": "."}, ctx)
    globbed = await registry.dispatch("glob_search", {"pattern": "*.txt"}, ctx)
    grep = await registry.dispatch("grep_search", {"regex": "bet", "path": "."}, ctx)
    external = await registry.dispatch("read_file", {"path": "../outside.txt"}, ctx)

    assert read.content.startswith("2: beta")
    assert "note.txt" in listing.content
    assert globbed.content == "note.txt"
    assert "note.txt:2: beta" in grep.content
    assert external.is_error is False
    assert external.content == "1: external"


@pytest.mark.asyncio
async def test_write_and_edit_file_tools(tmp_path: Path) -> None:
    registry = ToolRegistry([WriteFileTool(), EditFileTool(), ReadFileTool()])
    ctx = _ctx(tmp_path)

    written = await registry.dispatch("write_file", {"path": "probe.txt", "content": "old"}, ctx)
    edited = await registry.dispatch(
        "edit_file",
        {"path": "probe.txt", "old": "old", "new": "new"},
        ctx,
    )
    read = await registry.dispatch("read_file", {"path": "probe.txt"}, ctx)

    assert written.is_error is False
    assert edited.is_error is False
    assert "1: new" in read.content


def test_file_effect_preflight_rejects_deterministic_local_failures(
    tmp_path: Path,
) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(ValueError, match="parent directory"):
        WriteFileTool().effect_identity(
            {"path": "missing/probe.txt", "content": "value"},
            ctx,
        )

    path = tmp_path / "probe.txt"
    path.write_text("current", encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one match"):
        EditFileTool().effect_identity(
            {"path": "probe.txt", "old": "stale", "new": "replacement"},
            ctx,
        )


def test_registry_permission_scope_reports_tool_declaration(tmp_path: Path) -> None:
    registry = ToolRegistry([WriteFileTool(), ReadFileTool()])
    ctx = _ctx(tmp_path)

    scope = registry.permission_scope("write_file", {"path": "a.txt", "content": "x"}, ctx)
    read_scope = registry.permission_scope("read_file", {"path": "a.txt"}, ctx)
    external = tmp_path.parent / "outside.txt"
    external_scope = registry.permission_scope("read_file", {"path": str(external)}, ctx)

    assert scope is not None
    assert scope.params_equal == {"path": str(tmp_path / "a.txt")}
    assert scope.allow_unconstrained is False
    assert read_scope is not None
    assert read_scope.requires_permission is False
    assert read_scope.params_equal == {"path": str(tmp_path / "a.txt")}
    assert external_scope is not None
    assert external_scope.requires_permission is True
    assert external_scope.directory_path == str(tmp_path.parent)
    assert registry.permission_scope("nonexistent", {}, ctx) is None


def test_run_shell_offers_an_unconstrained_session_scope(tmp_path: Path) -> None:
    registry = ToolRegistry([RunShellTool()])

    scope = registry.permission_scope(
        "run_shell",
        {"command": "printf hi"},
        _ctx(tmp_path),
    )

    assert scope is not None
    assert scope.params_equal == {}
    assert scope.allow_unconstrained is True


def test_run_shell_effect_identity_hashes_the_command(tmp_path: Path) -> None:
    secret = "credential-that-must-not-enter-the-ledger"
    identity = RunShellTool().effect_identity(
        {"command": f"deploy --token {secret}"},
        _ctx(tmp_path),
    )

    rendered = identity.model_dump_json()
    assert secret not in rendered
    assert "deploy --token" not in rendered


@pytest.mark.asyncio
async def test_run_shell_reports_exit_code_and_timeout(tmp_path: Path) -> None:
    registry = ToolRegistry([RunShellTool()])
    ctx = _ctx(tmp_path)

    ok = await registry.dispatch("run_shell", {"command": "printf hi"}, ctx)
    timeout = await registry.dispatch(
        "run_shell",
        {"command": "sleep 1", "timeout_seconds": 0.01},
        ctx,
    )

    assert "exit_code: 0" in ok.content
    assert "hi" in ok.content
    assert timeout.is_error is True
    assert "timed out" in timeout.content


@pytest.mark.asyncio
async def test_update_tasks_mutates_session_task_list(tmp_path: Path) -> None:
    registry = ToolRegistry([UpdateTasksTool()])
    ctx = _ctx(tmp_path)

    result = await registry.dispatch(
        "update_tasks",
        {"tasks": [{"id": "task_1", "title": "Inspect", "status": "in_progress"}]},
        ctx,
    )

    assert result.is_error is False
    assert ctx.session.tasks[0].id == "task_1"
    assert ctx.session.tasks[0].status == "in_progress"


class EchoParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str


class NestedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str


class StructuredParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    payload: NestedPayload
    items: list[NestedPayload]
    literal: str


class StructuredTool:
    name: ClassVar[str] = "structured"
    description: ClassVar[str] = "Accept nested structured values."
    Params: ClassVar[type[BaseModel]] = StructuredParams
    risk: ClassVar[Risk] = "read_only"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        parsed = StructuredParams.model_validate(params)
        return ToolResult(content=parsed.model_dump_json())


class EchoTool:
    name: ClassVar[str] = "echo"
    description: ClassVar[str] = "Echo text."
    Params: ClassVar[type[BaseModel]] = EchoParams
    risk: ClassVar[Risk] = "read_only"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        _ = ctx
        args = EchoParams.model_validate(params)
        return ToolResult(content=args.text)


class MissingReceiptTool(EchoTool):
    name: ClassVar[str] = "missing_receipt"
    risk: ClassVar[Risk] = "mutating"
    capability_id = None
    effect_kind = "external"
    unattended = "allowed"
    state_guard_id = None

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        del args, ctx
        return EffectIdentity(
            operation=self.name,
            target="test",
            occurrence="one",
            summary="Test missing receipt",
            action_key="a" * 64,
        )


@pytest.mark.asyncio
async def test_external_normal_result_without_receipt_is_an_in_doubt_contract_error(
    tmp_path: Path,
) -> None:
    result = await ToolRegistry([MissingReceiptTool()]).dispatch(
        "missing_receipt",
        {"text": "provider returned normally"},
        _ctx(tmp_path),
    )

    assert result.is_error
    assert "contract error" in result.content
    assert result.effect_receipt is not None
    assert result.effect_receipt.disposition == "in_doubt"


@pytest.mark.asyncio
async def test_registry_validates_args_and_truncates_results(tmp_path: Path) -> None:
    registry = ToolRegistry([EchoTool()], max_result_chars=12)
    ctx = _ctx(tmp_path)

    invalid = await registry.dispatch("echo", {}, ctx)
    truncated = await registry.dispatch("echo", {"text": "abcdefghijklmnopqrstuvwxyz"}, ctx)

    assert invalid.is_error is True
    assert "Invalid arguments" in invalid.content
    assert truncated.content.endswith("[truncated]")
    assert len(truncated.content) == 12


@pytest.mark.asyncio
async def test_registry_normalizes_only_schema_declared_structured_json_strings(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry([StructuredTool()])
    raw = {
        "payload": '{"label":"one"}',
        "items": '[{"label":"two"}]',
        "literal": '{"must":"stay a string"}',
    }

    prepared = registry.prepare_args("structured", raw)
    result = await registry.dispatch("structured", raw, _ctx(tmp_path))

    assert prepared.error is None
    assert prepared.normalized_paths == ("payload", "items")
    assert prepared.args == {
        "payload": {"label": "one"},
        "items": [{"label": "two"}],
        "literal": '{"must":"stay a string"}',
    }
    assert result.is_error is False
    assert '"literal":"{\\"must\\":\\"stay a string\\"}"' in result.content


def test_registry_rejects_malformed_or_wrong_shape_json_without_semantic_coercion() -> None:
    registry = ToolRegistry([StructuredTool()])

    malformed = registry.prepare_args(
        "structured",
        {"payload": "{bad", "items": "[]", "literal": "ok"},
    )
    wrong_shape = registry.prepare_args(
        "structured",
        {"payload": "[]", "items": "{}", "literal": "ok"},
    )

    assert malformed.error is not None
    assert malformed.error.data is not None
    assert "received str" in malformed.error.content
    assert wrong_shape.error is not None
    assert wrong_shape.normalized_paths == ()


def test_permission_engine_uses_grants_rules_and_risk_defaults() -> None:
    settings = RickySettings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    session.permission_grants.append(
        PermissionGrant(tool_name="run_shell", params_equal={"command": "pwd"})
    )
    engine = PermissionEngine(
        Policy(rules=[PolicyRule(tool_name="write_file", decision="deny", reason="locked")])
    )

    granted = engine.decide(
        session,
        tool_name="run_shell",
        risk="destructive",
        params={"command": "pwd"},
    )
    denied = engine.decide(session, tool_name="write_file", risk="mutating", params={})
    read = engine.decide(session, tool_name="read_file", risk="read_only", params={})
    shell = engine.decide(session, tool_name="run_shell", risk="destructive", params={})

    assert granted.decision == "allow"
    assert denied.decision == "deny"
    assert read.decision == "allow"
    assert shell.decision == "ask"


def test_permission_engine_deny_rule_overrides_generalized_grant() -> None:
    settings = RickySettings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    session.permission_grants.append(
        PermissionGrant(tool_name="gmail_trash", params_equal={"account": "work"})
    )
    engine = PermissionEngine(
        Policy(
            rules=[
                PolicyRule(
                    tool_name="gmail_trash",
                    params_equal={"message_id": "protected"},
                    decision="deny",
                    reason="protected message",
                )
            ]
        )
    )

    denied = engine.decide(
        session,
        tool_name="gmail_trash",
        risk="mutating",
        params={"account": "work", "message_id": "protected"},
    )
    granted = engine.decide(
        session,
        tool_name="gmail_trash",
        risk="mutating",
        params={"account": "work", "message_id": "ordinary"},
    )

    assert denied.decision == "deny"
    assert denied.reason == "protected message"
    assert granted.decision == "allow"
    assert granted.reason == "allowed by session grant"
