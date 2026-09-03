"""Workflow validation-tool and authoring-skill tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from ricky.agent.session import AgentSession
from ricky.builtins import bundled_workflows_dir
from ricky.config import RickySettings
from ricky.profiles import ProfileScope
from ricky.skills.registry import discover_skills
from ricky.tools.base import ToolContext
from ricky.tools.registry import ToolRegistry
from ricky.workflows.compile import compile_workflow
from ricky.workflows.registry import WorkflowRegistry
from ricky.workflows.spec import parse_workflow_toml
from ricky.workflows.tool import StartWorkflowTool, ValidateWorkflowTool

VALID_BUNDLE = """
version = 2
name = "receipt"
description = "Record a typed receipt."

[args.thread]
type = "string"
description = "Thread identifier."

[[steps]]
id = "done"
kind = "message"
message = "receipt recorded"
"""


def _write_bundle(root: Path, name: str, body: str) -> Path:
    bundle = root / name
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "workflow.toml").write_text(body, encoding="utf-8")
    return bundle


def _context(tmp_path: Path) -> ToolContext:
    settings = RickySettings()
    return ToolContext(
        cwd=tmp_path,
        settings=settings,
        session=AgentSession.create(settings, profile_scope=settings.resolve_profile_scope()),
    )


def _validator(
    tmp_path: Path,
    workflow_registry: WorkflowRegistry | None = None,
) -> ValidateWorkflowTool:
    return ValidateWorkflowTool(
        skill_names=set(),
        tool_registry=ToolRegistry([]),
        workflow_registry=workflow_registry,
    )


async def test_validate_workflow_reports_a_fresh_valid_bundle(
    tmp_path: Path, bundled_root: Path
) -> None:
    tool = _validator(tmp_path)
    workflows = bundled_root / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    _write_bundle(workflows, "receipt", VALID_BUNDLE)

    result = await tool.run(tool.Params.model_validate({"name": "receipt"}), _context(tmp_path))

    assert not result.is_error
    assert "workflow: receipt" in result.content
    assert result.content.endswith("valid")


async def test_validate_workflow_rejects_a_legacy_bundle(
    tmp_path: Path, bundled_root: Path
) -> None:
    workflows = bundled_root / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    _write_bundle(
        workflows,
        "legacy",
        'name = "legacy"\ndescription = "old"\nentry = "done"\n',
    )

    result = await _validator(tmp_path).run(
        ValidateWorkflowTool.Params.model_validate({"name": "legacy"}),
        _context(tmp_path),
    )

    assert not result.is_error
    assert "is not version 2" in result.content


async def test_validate_workflow_unknown_bundle_is_an_error(
    tmp_path: Path, bundled_root: Path
) -> None:
    (bundled_root / "workflows").mkdir(parents=True, exist_ok=True)

    result = await _validator(tmp_path).run(
        ValidateWorkflowTool.Params.model_validate({"name": "ghost"}),
        _context(tmp_path),
    )

    assert result.is_error
    assert "No workflow bundle named 'ghost'" in result.content


async def test_validate_workflow_rejects_paths_and_escaping_symlinks(
    tmp_path: Path,
    bundled_root: Path,
) -> None:
    workflows = bundled_root / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    external = _write_bundle(
        outside,
        "escape",
        'version = 2\nname = "escape"\ndescription = "outside secret"\n'
        '[[steps]]\nid = "done"\nkind = "message"\nmessage = "secret"\n',
    )
    source_link = _write_bundle(
        outside,
        "file-link",
        'version = 2\nname = "file-link"\ndescription = "outside file secret"\n'
        '[[steps]]\nid = "done"\nkind = "message"\nmessage = "secret"\n',
    )
    (workflows / "escape").symlink_to(external, target_is_directory=True)
    (workflows / "file-link").mkdir()
    (workflows / "file-link" / "workflow.toml").symlink_to(source_link / "workflow.toml")
    tool = _validator(tmp_path)
    registry = ToolRegistry([tool])
    ctx = _context(tmp_path)

    for name in ("../outside", str(external.resolve())):
        result = await registry.dispatch("validate_workflow", {"name": name}, ctx)
        assert result.is_error

    escaped = await registry.dispatch("validate_workflow", {"name": "escape"}, ctx)
    assert escaped.is_error
    assert "escapes workflows directory" in escaped.content
    assert "outside secret" not in escaped.content

    linked = await registry.dispatch("validate_workflow", {"name": "file-link"}, ctx)
    assert linked.is_error
    assert "workflow definition escapes its bundle" in linked.content
    assert "outside file secret" not in linked.content


async def test_validating_a_fresh_bundle_reloads_the_shared_registry(
    tmp_path: Path, bundled_root: Path
) -> None:
    workflows = bundled_root / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    registry = WorkflowRegistry()
    _write_bundle(workflows, "receipt", VALID_BUNDLE)
    ctx = _context(tmp_path)

    result = await _validator(tmp_path, registry).run(
        ValidateWorkflowTool.Params.model_validate({"name": "receipt"}), ctx
    )

    assert not result.is_error
    assert registry.get("receipt") is not None
    queued = await StartWorkflowTool(registry).run(
        StartWorkflowTool.Params.model_validate({"name": "receipt", "args": {"thread": "t-1"}}),
        ctx,
    )
    assert not queued.is_error
    assert ctx.session.active_workflow is not None
    assert ctx.session.active_workflow.name == "bundled/receipt"


@pytest.mark.real_bundled_resources
def test_author_workflow_skill_uses_the_current_contract() -> None:
    skill = discover_skills(profile_scope=ProfileScope.create("personal")).get("author-workflow")

    assert skill is not None
    assert skill.name == "author-workflow"
    assert "validate_workflow" in skill.body
    assert "version = 2" in skill.body
    assert "<resolved-user-data-dir>/profiles/<owner>/workflows/<name>" in skill.body
    assert "<owner>/<name> <scope-flags>" in skill.body
    assert "Do not add a `profile` key" in skill.body
    assert "profile-qualified id exposed" in skill.body
    assert "Do not use an unqualified local name as a default" in skill.body
    assert "uv run ricky config" in skill.body
    assert "workflow.max_binding_chars" in skill.body
    section = skill.body.split("## Minimal V2 skeleton", 1)[1]
    skeleton = section.split("```toml", 1)[1].split("```", 1)[0].strip()
    spec = parse_workflow_toml(skeleton, source="author-workflow skeleton")
    compiled = compile_workflow(spec, tool_registry=ToolRegistry([]))
    assert compiled.graph is not None, compiled.errors


@pytest.mark.real_bundled_resources
def test_email_triage_requires_a_profile_qualified_google_account() -> None:
    workflow = bundled_workflows_dir() / "email-triage" / "workflow.toml"

    spec = parse_workflow_toml(workflow.read_text(encoding="utf-8"), source=str(workflow))

    account = spec.args["account"]
    assert account.required
    assert account.default is None
    assert "profile-qualified Google account id" in account.description
    assert "personal/personal" in account.description


@pytest.mark.real_bundled_resources
def test_update_workflow_skill_preserves_current_graph_invariants() -> None:
    registry = discover_skills(profile_scope=ProfileScope.create("personal"))
    author = registry.get("author-workflow")
    update = registry.get("update-workflow")

    assert author is not None
    assert update is not None
    assert "existing `version = 2` workflow" in update.body
    assert "Call `validate_workflow` with `<owner>/<name>` before editing" in update.body
    assert "<user_data_dir>/profiles/<owner>/workflows/<name>/" in update.body
    assert "explicit ownership, authority, and saved-run" in update.body
    assert "stale unqualified defaults" in update.body
    assert "changed graph fingerprint cannot resume automatically" in update.body
    assert "must not dispatch mutating or" in update.body
    assert "create" in author.description.lower()
    assert "existing" in update.description.lower()
