"""Canonical host-file access and directory-grant tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from ricky.agent import AgentSession
from ricky.agent.events import PermissionRequestedEvent
from ricky.agent.tool_dispatch import GateOutcome, decide_tool_permission
from ricky.config import RickySettings
from ricky.llm import ToolCallPart
from ricky.permissions import PermissionEngine, PermissionResponse, Policy
from ricky.tools import Tool, ToolContext, ToolRegistry
from ricky.tools.builtin import (
    EditFileTool,
    GlobSearchTool,
    GrepSearchTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from ricky.tools.paths import resolve_host_glob, resolve_host_path

Responder = Callable[[PermissionRequestedEvent], Awaitable[PermissionResponse]]


def _context(project: Path) -> ToolContext:
    settings = RickySettings()
    return ToolContext(
        cwd=project,
        settings=settings,
        session=AgentSession.create(settings, profile_scope=settings.resolve_profile_scope()),
    )


async def _gate(
    registry: ToolRegistry,
    ctx: ToolContext,
    *,
    call_id: str,
    tool_name: str,
    args: dict[str, object],
    responder: Responder,
) -> GateOutcome:
    return await decide_tool_permission(
        session=ctx.session,
        registry=registry,
        engine=PermissionEngine(Policy()),
        responder=responder,
        turn_id="turn_host_files",
        call=ToolCallPart(id=call_id, name=tool_name, args=args),
        ctx=ctx,
    )


def _permission_request(outcome: GateOutcome) -> PermissionRequestedEvent:
    return next(event for event in outcome.events if isinstance(event, PermissionRequestedEvent))


def test_host_paths_accept_absolute_home_and_workspace_relative_forms(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    absolute = tmp_path / "document.txt"

    assert resolve_host_path(project, "notes/today.txt") == project / "notes" / "today.txt"
    assert resolve_host_path(project, absolute) == absolute
    assert resolve_host_path(project, "~/document.txt") == Path.home() / "document.txt"

    host_glob = resolve_host_glob(project, "../*.txt")
    assert host_glob.root == tmp_path
    assert host_glob.pattern == "*.txt"
    assert host_glob.canonical_pattern == str(tmp_path / "*.txt")


@pytest.mark.asyncio
async def test_external_read_asks_for_the_canonical_symlink_target(tmp_path: Path) -> None:
    project = tmp_path / "project"
    documents = tmp_path / "Documents"
    project.mkdir()
    documents.mkdir()
    source = documents / "brief.txt"
    source.write_text("brief", encoding="utf-8")
    link = project / "brief-link.txt"
    link.symlink_to(source)
    registry = ToolRegistry([ReadFileTool()])
    ctx = _context(project)
    requests: list[PermissionRequestedEvent] = []

    async def allow_once(event: PermissionRequestedEvent) -> PermissionResponse:
        requests.append(event)
        return PermissionResponse(decision="allow", grant="scoped")

    outcome = await _gate(
        registry,
        ctx,
        call_id="call_link",
        tool_name="read_file",
        args={"path": "brief-link.txt"},
        responder=allow_once,
    )

    assert outcome.decision == "allow"
    assert len(requests) == 1
    assert requests[0].args["path"] == str(source)
    assert [option.id for option in requests[0].offered_grants] == [
        "scoped",
        "directory",
    ]
    assert ctx.session.permission_grants[0].params_equal == {"path": str(source)}


@pytest.mark.asyncio
async def test_exact_grant_does_not_follow_a_retargeted_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    documents = tmp_path / "Documents"
    project.mkdir()
    documents.mkdir()
    first = documents / "first.txt"
    second = documents / "second.txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    link = project / "current.txt"
    link.symlink_to(first)
    registry = ToolRegistry([ReadFileTool()])
    ctx = _context(project)

    async def remember_exact(_event: PermissionRequestedEvent) -> PermissionResponse:
        return PermissionResponse(decision="allow", grant="scoped")

    first_outcome = await _gate(
        registry,
        ctx,
        call_id="call_first",
        tool_name="read_file",
        args={"path": "current.txt"},
        responder=remember_exact,
    )
    link.unlink()
    link.symlink_to(second)
    assert first_outcome.normalized_args is not None
    original_result = await registry.dispatch("read_file", first_outcome.normalized_args, ctx)
    asked_again: list[PermissionRequestedEvent] = []

    async def allow_again(event: PermissionRequestedEvent) -> PermissionResponse:
        asked_again.append(event)
        return PermissionResponse(decision="allow")

    second_outcome = await _gate(
        registry,
        ctx,
        call_id="call_second",
        tool_name="read_file",
        args={"path": "current.txt"},
        responder=allow_again,
    )

    assert first_outcome.decision == "allow"
    assert original_result.content == "1: first"
    assert second_outcome.decision == "allow"
    assert asked_again[0].args["path"] == str(second)


@pytest.mark.asyncio
async def test_directory_grant_covers_only_resolved_targets_below_that_directory(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    documents = tmp_path / "Documents"
    elsewhere = tmp_path / "elsewhere"
    project.mkdir()
    documents.mkdir()
    elsewhere.mkdir()
    first = documents / "first.txt"
    sibling = documents / "sibling.txt"
    foreign = elsewhere / "foreign.txt"
    for path in (first, sibling, foreign):
        path.write_text(path.stem, encoding="utf-8")
    foreign_link = documents / "foreign-link.txt"
    foreign_link.symlink_to(foreign)
    registry = ToolRegistry([ReadFileTool()])
    ctx = _context(project)

    async def remember_directory(_event: PermissionRequestedEvent) -> PermissionResponse:
        return PermissionResponse(decision="allow", grant="directory")

    remembered = await _gate(
        registry,
        ctx,
        call_id="call_first",
        tool_name="read_file",
        args={"path": str(first)},
        responder=remember_directory,
    )

    async def unexpected(_event: PermissionRequestedEvent) -> PermissionResponse:
        pytest.fail("the remembered directory grant should have allowed this call")

    sibling_outcome = await _gate(
        registry,
        ctx,
        call_id="call_sibling",
        tool_name="read_file",
        args={"path": str(sibling)},
        responder=unexpected,
    )
    foreign_requests: list[PermissionRequestedEvent] = []

    async def allow_foreign(event: PermissionRequestedEvent) -> PermissionResponse:
        foreign_requests.append(event)
        return PermissionResponse(decision="allow")

    foreign_outcome = await _gate(
        registry,
        ctx,
        call_id="call_foreign",
        tool_name="read_file",
        args={"path": str(foreign_link)},
        responder=allow_foreign,
    )

    assert remembered.remembered_grant is not None
    assert remembered.remembered_grant.directory_path == str(documents)
    assert not remembered.remembered_grant.matches(
        "read_file",
        {"path": str(documents / "nested" / ".." / ".." / "elsewhere" / "foreign.txt")},
    )
    assert sibling_outcome.decision == "allow"
    assert foreign_outcome.decision == "allow"
    assert len(foreign_requests) == 1
    assert foreign_requests[0].args["path"] == str(foreign)


@pytest.mark.asyncio
async def test_external_directory_and_search_reads_all_force_permission(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    documents = tmp_path / "Documents"
    project.mkdir()
    documents.mkdir()
    (documents / "brief.txt").write_text("brief", encoding="utf-8")
    cases: list[tuple[Tool, str, dict[str, object]]] = [
        (ListDirTool(), "list_dir", {"path": str(documents)}),
        (GlobSearchTool(), "glob_search", {"pattern": str(documents / "*.txt")}),
        (GrepSearchTool(), "grep_search", {"regex": "brief", "path": str(documents)}),
    ]

    for index, (tool, name, args) in enumerate(cases):
        registry = ToolRegistry([tool])
        ctx = _context(project)
        requests: list[PermissionRequestedEvent] = []

        async def allow(
            event: PermissionRequestedEvent,
            captured: list[PermissionRequestedEvent] = requests,
        ) -> PermissionResponse:
            captured.append(event)
            return PermissionResponse(decision="allow")

        outcome = await _gate(
            registry,
            ctx,
            call_id=f"call_{index}",
            tool_name=name,
            args=args,
            responder=allow,
        )

        assert outcome.decision == "allow"
        assert len(requests) == 1
        assert [option.id for option in requests[0].offered_grants] == [
            "scoped",
            "directory",
        ]
        assert requests[0].args["path"] == str(documents)


@pytest.mark.asyncio
async def test_glob_directory_grant_covers_other_patterns_at_the_same_root(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    documents = tmp_path / "Documents"
    project.mkdir()
    documents.mkdir()
    registry = ToolRegistry([GlobSearchTool()])
    ctx = _context(project)

    async def remember_directory(_event: PermissionRequestedEvent) -> PermissionResponse:
        return PermissionResponse(decision="allow", grant="directory")

    first = await _gate(
        registry,
        ctx,
        call_id="call_txt",
        tool_name="glob_search",
        args={"pattern": str(documents / "*.txt")},
        responder=remember_directory,
    )

    async def unexpected(_event: PermissionRequestedEvent) -> PermissionResponse:
        pytest.fail("the remembered glob root should allow another pattern")

    second = await _gate(
        registry,
        ctx,
        call_id="call_md",
        tool_name="glob_search",
        args={"pattern": str(documents / "*.md")},
        responder=unexpected,
    )

    assert first.remembered_grant is not None
    assert first.remembered_grant.directory_path == str(documents)
    assert second.decision == "allow"


@pytest.mark.asyncio
async def test_exact_glob_symlink_is_authorized_and_executed_as_its_resolved_target(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    external = tmp_path / "external"
    project.mkdir()
    external.mkdir()
    target = external / "report.txt"
    target.write_text("outside", encoding="utf-8")
    link = project / "report-link.txt"
    link.symlink_to(target)
    registry = ToolRegistry([GlobSearchTool()])
    ctx = _context(project)
    requests: list[PermissionRequestedEvent] = []

    async def allow(event: PermissionRequestedEvent) -> PermissionResponse:
        requests.append(event)
        return PermissionResponse(decision="allow")

    outcome = await _gate(
        registry,
        ctx,
        call_id="call_symlink_glob",
        tool_name="glob_search",
        args={"pattern": str(link)},
        responder=allow,
    )
    assert outcome.normalized_args is not None
    result = await registry.dispatch("glob_search", outcome.normalized_args, ctx)

    assert outcome.decision == "allow"
    assert requests[0].args["pattern"] == str(target)
    assert requests[0].args["path"] == str(external)
    assert result.content == str(target)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("write_file", {"path": "../outside.txt", "content": "written"}),
        ("edit_file", {"path": "../outside.txt", "old": "written", "new": "edited"}),
    ],
)
async def test_external_writes_keep_exact_permission_gates_and_remain_usable(
    tmp_path: Path,
    tool_name: str,
    args: dict[str, object],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    if tool_name == "edit_file":
        outside.write_text("written", encoding="utf-8")
    registry = ToolRegistry([WriteFileTool(), EditFileTool()])
    ctx = _context(project)
    requests: list[PermissionRequestedEvent] = []

    async def allow(event: PermissionRequestedEvent) -> PermissionResponse:
        requests.append(event)
        return PermissionResponse(decision="allow")

    outcome = await _gate(
        registry,
        ctx,
        call_id=f"call_{tool_name}",
        tool_name=tool_name,
        args=args,
        responder=allow,
    )
    result = await registry.dispatch(tool_name, args, ctx)

    assert outcome.decision == "allow"
    assert requests[0].args["path"] == str(outside)
    assert [option.id for option in requests[0].offered_grants] == ["scoped"]
    assert result.is_error is False
    assert outside.read_text(encoding="utf-8") == (
        "written" if tool_name == "write_file" else "edited"
    )


def test_file_params_reject_extra_fields_and_scalar_coercion(tmp_path: Path) -> None:
    registry = ToolRegistry([ReadFileTool()])

    extra = registry.prepare_args("read_file", {"path": "note.txt", "unknown": True})
    coerced = registry.prepare_args("read_file", {"path": "note.txt", "offset": "1"})

    assert extra.error is not None
    assert coerced.error is not None
    assert not (tmp_path / "note.txt").exists()
