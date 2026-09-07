"""Skill search confinement, bounds, pinning, and released documentation lookup."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ricky.agent import AgentSession
from ricky.config import RickySettings
from ricky.jobs.runner import _rebind_skill_tools
from ricky.skills.registry import SkillRegistry, discover_skills
from ricky.skills.search import SearchSkillResourcesTool
from ricky.skills.spec import parse_skill_markdown
from ricky.skills.tool import ReadSkillResourceTool
from ricky.tools import ToolContext, ToolRegistry
from ricky.tools.testing import assert_tool_contract


def _bundle(
    path: Path, body: str = "# Guide\nConfigure Gmail.\nGMAIL authorization.\n"
) -> SkillRegistry:
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text("---\nname: guide\ndescription: A guide.\n---\nRead refs.\n")
    (path / "guide.md").write_text(body)
    return SkillRegistry(
        [parse_skill_markdown(path / "SKILL.md", profile="personal", bundle_path=path)]
    )


def _ctx(tmp_path: Path) -> ToolContext:
    settings = RickySettings(
        user_data_dir=str(tmp_path / "user"), project_data_dir=str(tmp_path / "project")
    )
    return ToolContext(
        cwd=tmp_path,
        settings=settings,
        session=AgentSession.create(settings, profile_scope=settings.resolve_profile_scope()),
    )


async def test_search_contract_and_match_paging(tmp_path: Path) -> None:
    skills = _bundle(tmp_path / "skill")
    ctx = _ctx(tmp_path)
    assert skills.activate(ctx.session, "guide").ok
    tool = SearchSkillResourcesTool(skills)
    first = await assert_tool_contract(tool, valid_args={"query": "gmail", "limit": 1}, ctx=ctx)
    assert isinstance(first.data, dict)
    assert first.data["matches"] == [{"path": "guide.md", "line": 2, "text": "Configure Gmail."}]
    assert first.data["has_more"] is True
    registry = ToolRegistry([tool])
    second = await registry.dispatch("search_skill_resources", {"query": "gmail", "offset": 1}, ctx)
    assert isinstance(second.data, dict)
    assert second.data["matches"] == [
        {"path": "guide.md", "line": 3, "text": "GMAIL authorization."}
    ]
    assert second.data["has_more"] is False
    literal = await registry.dispatch("search_skill_resources", {"query": ".*"}, ctx)
    assert literal.content == "No matches."


async def test_search_confines_paths_and_requires_current_active_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "skill"
    skills = _bundle(bundle)
    ctx = _ctx(tmp_path)
    tools = ToolRegistry([SearchSkillResourcesTool(skills)])
    result = await tools.dispatch("search_skill_resources", {"query": "gmail"}, ctx)
    assert result.is_error
    assert skills.activate(ctx.session, "guide").ok
    outside = tmp_path / "outside.md"
    outside.write_text("outside-marker\n")
    (bundle / "escape.md").symlink_to(outside)
    (bundle / "escape-dir").symlink_to(tmp_path, target_is_directory=True)
    for path in ("../outside.md", str(outside), "escape.md", "missing.md", "."):
        result = await tools.dispatch(
            "search_skill_resources", {"query": "marker", "path": path}, ctx
        )
        assert result.is_error, path
        assert "outside-marker" not in result.content
    result = await tools.dispatch("search_skill_resources", {"query": "outside-marker"}, ctx)
    assert result.content == "No matches."
    stale = ToolRegistry([SearchSkillResourcesTool(SkillRegistry())])
    result = await stale.dispatch("search_skill_resources", {"query": "gmail"}, ctx)
    assert result.is_error
    assert not (tmp_path / "user").exists()
    assert not (tmp_path / "project").exists()


async def test_unicode_excerpt_keeps_the_match_and_bounds_long_lines(tmp_path: Path) -> None:
    skills = _bundle(tmp_path / "skill", "ß" * 500 + "Gmail" + "x" * 500)
    ctx = _ctx(tmp_path)
    assert skills.activate(ctx.session, "guide").ok
    result = await ToolRegistry([SearchSkillResourcesTool(skills)]).dispatch(
        "search_skill_resources", {"query": "gmail"}, ctx
    )
    assert "Gmail" in result.content
    assert len(result.content) < 450


async def test_internal_skill_definition_symlink_preserves_the_bundle_root(tmp_path: Path) -> None:
    bundle = tmp_path / "skill"
    _bundle(bundle)
    nested = bundle / "references"
    nested.mkdir()
    (bundle / "SKILL.md").rename(nested / "instructions.md")
    (bundle / "SKILL.md").symlink_to(nested / "instructions.md")
    skills = SkillRegistry(
        [parse_skill_markdown(bundle / "SKILL.md", profile="personal", bundle_path=bundle)]
    )
    ctx = _ctx(tmp_path)
    assert skills.activate(ctx.session, "guide").ok
    result = await ToolRegistry([SearchSkillResourcesTool(skills)]).dispatch(
        "search_skill_resources", {"query": "gmail"}, ctx
    )
    assert "guide.md:2:" in result.content


async def test_scan_limits_and_invalid_queries_are_explicit(tmp_path: Path, monkeypatch) -> None:
    from ricky.skills import search

    skills = _bundle(tmp_path / "skill")
    ctx = _ctx(tmp_path)
    assert skills.activate(ctx.session, "guide").ok
    tools = ToolRegistry([SearchSkillResourcesTool(skills)])
    for query in (" ", "\n", "x\ny"):
        result = await tools.dispatch("search_skill_resources", {"query": query}, ctx)
        assert result.is_error
    monkeypatch.setattr(search, "_BYTE_LIMIT", 10)
    result = await tools.dispatch("search_skill_resources", {"query": "gmail"}, ctx)
    assert isinstance(result.data, dict) and result.data["incomplete"] is True
    assert "incomplete" in result.content


async def test_search_can_be_cancelled_between_files(tmp_path: Path, monkeypatch) -> None:
    from ricky.skills import search

    skills = _bundle(tmp_path / "skill")
    ctx = _ctx(tmp_path)
    assert skills.activate(ctx.session, "guide").ok
    reached = asyncio.Event()

    async def pause(delay: float) -> None:
        reached.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(search.asyncio, "sleep", pause)
    task = asyncio.create_task(
        SearchSkillResourcesTool(skills).run(SearchSkillResourcesTool.Params(query="gmail"), ctx)
    )
    await reached.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_pinned_execution_search_uses_snapshot_instead_of_live_docs(tmp_path: Path) -> None:
    live = _bundle(tmp_path / "live", "Live changed settings.\n")
    pinned = _bundle(tmp_path / "snapshot", "Pinned settings.\n")
    tools = _rebind_skill_tools(ToolRegistry([SearchSkillResourcesTool(live)]), pinned)
    ctx = _ctx(tmp_path)
    assert pinned.activate(ctx.session, "guide").ok
    result = await tools.dispatch("search_skill_resources", {"query": "settings"}, ctx)
    assert "Pinned settings" in result.content
    assert "Live changed" not in result.content


@pytest.mark.real_bundled_resources
async def test_docs_skill_is_discoverable_searchable_and_readable_without_project(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    ctx = _ctx(tmp_path)
    skills = discover_skills(settings=ctx.settings, profile_scope=ctx.session.profile_scope)
    assert skills.activate(ctx.session, "bundled/ricky-docs").ok
    tools = ToolRegistry([SearchSkillResourcesTool(skills), ReadSkillResourceTool(skills)])
    result = await tools.dispatch(
        "search_skill_resources",
        {"query": "schedule refresh", "path": "references/docs/jobs-and-schedules.md"},
        ctx,
    )
    assert not result.is_error
    assert isinstance(result.data, dict) and result.data["matches"]
    read = await tools.dispatch(
        "read_skill_resource", {"path": "references/docs/configuration.md", "limit": 20}, ctx
    )
    assert "bootstrap" in read.content
