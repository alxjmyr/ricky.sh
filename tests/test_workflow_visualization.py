"""Design inspection, chat tools, CLI, and export isolation regression coverage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ricky.agent.session import AgentSession
from ricky.attachments import AttachmentInput, load_attachments
from ricky.config import RickySettings, user_data_path
from ricky.interfaces.cli.app import app
from ricky.profiles import ProfileScope
from ricky.skills.registry import discover_skills
from ricky.tools.base import ToolContext
from ricky.tools.registry import ToolRegistry
from ricky.tools.testing import assert_tool_contract
from ricky.workflows.inspection import WorkflowInspection, inspect_workflow, render_ascii
from ricky.workflows.inspection_tools import InspectWorkflowTool, RenderWorkflowTool
from ricky.workflows.visualization import (
    export_visualization,
    open_visualization,
    render_html,
    validate_visualization_path,
)
from workflow_visualization_support import GUIDANCE, INSTRUCTION, write_design


@pytest.fixture
def design(bundled_root: Path):
    bundle = write_design(bundled_root)
    settings = RickySettings()
    scope = settings.resolve_profile_scope()
    return bundle, settings, scope


def inspect(design) -> WorkflowInspection:
    _, settings, scope = design
    return inspect_workflow("email-triage", settings=settings, scope=scope, tools=ToolRegistry([]))


def test_inspector_preserves_prompts_bindings_conditions_and_nested_graph(design) -> None:
    view = inspect(design)
    assert WorkflowInspection.model_validate_json(view.model_dump_json()) == view
    classifier = next(s for s in view.steps if s.id == "classify")
    assert classifier.instruction == INSTRUCTION
    assert classifier.instruction_source == "classify.md"
    assert classifier.skill_body == GUIDANCE
    assert classifier.skill == "bundled/mail-guidance"
    assert classifier.parent == "classify-emails"
    assert json.loads(classifier.model_dump_json())["output_schema"]["properties"]["category"][
        "values"
    ] == [
        "billing",
        "support",
        "other",
    ]
    edges = {(e.source, e.target, e.kind, e.reference, e.destination) for e in view.edges}
    assert (
        "classify",
        "classify-emails",
        "data",
        "item.steps.classify.output.category",
        "outputs.category",
    ) in edges
    assert ("review", "report", "condition", "steps.review.output.approved", "when") in edges
    assert ("draft", "report", "data", "steps.draft.output.body", "message.values.body") in edges
    assert ("review", "report", "dependency", "", "") in edges
    overview = render_ascii(view)
    assert "classify-emails, context -> [draft] model" in overview
    assert INSTRUCTION not in overview
    assert "[classify] agent" in render_ascii(view, step="classify-emails", section="body")
    instructions = render_ascii(view, step="classify", section="instructions")
    assert INSTRUCTION in instructions and GUIDANCE in instructions
    assert "do not inherit chat history" in instructions
    assert "steps.draft.output.body -> report.message.values.body" in render_ascii(
        view, step="draft", section="outputs"
    )
    assert "Inbox owner" in render_ascii(view, step="context", section="inputs")
    assert "is_true" in render_ascii(view, step="report", section="policy")
    with pytest.raises(ValueError, match="Unknown step"):
        render_ascii(view, step="missing")


def test_inspection_refreshes_files_skills_and_rejects_invalid_design(design, bundled_root) -> None:
    bundle, _, _ = design
    first = inspect(design)
    (bundle / "classify.md").write_text("Updated classification rules.")
    second = inspect(design)
    assert second.fingerprint != first.fingerprint
    assert "Updated classification rules." in render_ascii(second, step="classify")
    skill = bundled_root / "skills/mail-guidance/SKILL.md"
    skill.write_text(skill.read_text() + "\nA new rule.")
    assert inspect(design).fingerprint != second.fingerprint
    spec = bundle / "workflow.toml"
    spec.write_text(spec.read_text().replace('needs = ["review"]', 'needs = ["missing"]'))
    with pytest.raises(ValueError, match="Invalid workflow"):
        inspect(design)


def test_instruction_resource_escape_is_rejected(design, tmp_path) -> None:
    bundle, _, _ = design
    outside = tmp_path / "outside.md"
    outside.write_text("not a bundle resource")
    (bundle / "classify.md").unlink()
    (bundle / "classify.md").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes bundle"):
        inspect(design)


def test_html_is_offline_and_embeds_text_without_script_injection(design) -> None:
    view = inspect(design)
    hostile = "</script><script>window.injected=true</script><img src=x onerror=alert(1)>"
    view.steps[0].instruction = hostile
    html = render_html(view)
    assert hostile not in html
    payload = html.split('<script id="workflow-data" type="application/json">')[1].split(
        "</script>"
    )[0]
    assert json.loads(payload)["steps"][0]["instruction"] == hostile
    assert "connect-src 'none'" in html
    assert "<script src=" not in html


def test_export_is_scoped_to_user_data_and_integrity_checked(design, tmp_path) -> None:
    _, settings, scope = design
    view = inspect(design)
    project = tmp_path / "project"
    project.mkdir()
    path = export_visualization(view, settings=settings, scope=scope)
    assert path.is_relative_to(user_data_path(settings))
    assert list(project.iterdir()) == []
    assert export_visualization(view, settings=settings, scope=scope) == path
    assert validate_visualization_path(path, settings=settings, scope=scope) == path
    other = ProfileScope(primary="shared", profiles=("shared",))
    with pytest.raises(ValueError, match="scope"):
        validate_visualization_path(path, settings=settings, scope=other)
    attachments = load_attachments(
        [AttachmentInput(path=str(path))],
        cwd=project,
        settings=settings,
        profile_scope=scope,
        count_limit=1,
        file_byte_limit=1_000_000,
        total_byte_limit=1_000_000,
    )
    assert attachments[0].filename.endswith(".html")
    with pytest.raises(ValueError, match="private state"):
        load_attachments(
            [AttachmentInput(path=str(path))],
            cwd=project,
            settings=settings,
            profile_scope=other,
            count_limit=1,
            file_byte_limit=1_000_000,
            total_byte_limit=1_000_000,
        )
    path.write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        validate_visualization_path(path, settings=settings, scope=scope)


async def test_chat_tool_contracts_and_no_workflow_invocation(design, tmp_path) -> None:
    _, settings, scope = design
    ctx = ToolContext(
        cwd=tmp_path, settings=settings, session=AgentSession.create(settings, profile_scope=scope)
    )
    inspection = await assert_tool_contract(
        InspectWorkflowTool(ToolRegistry([])), valid_args={"name": "email-triage"}, ctx=ctx
    )
    assert "[draft] model" in inspection.content
    export = await assert_tool_contract(
        RenderWorkflowTool(ToolRegistry([])), valid_args={"name": "email-triage"}, ctx=ctx
    )
    assert "HTML file:" in export.content
    assert ctx.session.active_workflow is None
    bad = await InspectWorkflowTool(ToolRegistry([])).run(
        InspectWorkflowTool.Params.model_validate({"name": "other/private"}), ctx
    )
    assert bad.is_error


def test_cli_simple_focused_html_and_unchanged_show(design) -> None:
    runner = CliRunner()
    overview = runner.invoke(app, ["workflow", "visualize", "email-triage"])
    assert overview.exit_code == 0, overview.output
    assert "classify-emails, context -> [draft] model" in overview.output
    detail = runner.invoke(
        app,
        [
            "workflow",
            "visualize",
            "email-triage",
            "--step",
            "classify",
            "--section",
            "instructions",
        ],
    )
    assert detail.exit_code == 0, detail.output
    assert INSTRUCTION in detail.output and GUIDANCE in detail.output
    result = runner.invoke(app, ["workflow", "visualize", "email-triage", "--html"])
    assert result.exit_code == 0, result.output
    assert Path(result.output.strip()).is_file()
    old = runner.invoke(app, ["workflow", "show", "email-triage"])
    assert old.exit_code == 0
    assert "workflow: email-triage" in old.output
    invalid = runner.invoke(app, ["workflow", "visualize", "email-triage", "--section", "typo"])
    assert invalid.exit_code != 0


async def test_chrome_open_hands_off_only_verified_file(design, monkeypatch) -> None:
    import ricky.workflows.visualization as module

    _, settings, scope = design
    path = export_visualization(inspect(design), settings=settings, scope=scope)
    calls = []

    async def chrome(_settings):
        return Path("/test/google-chrome")

    class Desktop:
        def __init__(self, command, **kwargs):
            calls.extend(command)
            assert kwargs["stdin"] == module.subprocess.DEVNULL
            assert kwargs["stdout"] == module.subprocess.DEVNULL
            assert kwargs["stderr"] == module.subprocess.DEVNULL
            assert kwargs["close_fds"] and kwargs["start_new_session"]

        def poll(self):
            return None

    monkeypatch.setattr(module, "require_chrome", chrome)
    monkeypatch.setattr(module.subprocess, "Popen", Desktop)
    assert await open_visualization(path, settings=settings, scope=scope)
    assert calls == ["/test/google-chrome", path.as_uri()]
    path.write_text("tampered")
    with pytest.raises(ValueError, match="changed"):
        await open_visualization(path, settings=settings, scope=scope)
    assert len(calls) == 2


@pytest.mark.real_bundled_resources
def test_bundled_visualization_skill_is_discoverable() -> None:
    settings = RickySettings()
    registry = discover_skills(settings=settings, profile_scope=settings.resolve_profile_scope())
    assert registry.get("bundled/visualize-workflow") is not None


def test_literal_reference_shaped_data_is_not_an_edge(design) -> None:
    bundle, _, _ = design
    spec = bundle / "workflow.toml"
    spec.write_text(
        spec.read_text().replace(
            'audience = "Inbox owner"',
            'audience = {kind = "literal", value = {ref = "steps.ghost.output"}}',
        )
    )
    view = inspect(design)
    assert not any(edge.reference == "steps.ghost.output" for edge in view.edges)
    assert "steps.ghost.output" in render_ascii(view, step="context", section="inputs")


def test_export_scope_symlink_and_interrupted_write_are_rejected(design, tmp_path, monkeypatch):
    import ricky.workflows.visualization as module

    _, settings, scope = design
    view = inspect(design)
    root = module.visualization_root(settings, scope)
    other = tmp_path / "other"
    other.mkdir()
    root.parent.mkdir(parents=True)
    root.symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        export_visualization(view, settings=settings, scope=scope)
    assert not list(other.iterdir())
    root.unlink()

    def fail_publish(*args):
        raise OSError("simulated interrupted publication")

    monkeypatch.setattr(module.os, "link", fail_publish)
    with pytest.raises(OSError, match="interrupted"):
        export_visualization(view, settings=settings, scope=scope)
    assert not list(root.iterdir())


def test_cli_open_requests_chrome_and_reports_missing_browser(design, monkeypatch):
    import ricky.interfaces.cli.workflows as cli
    import ricky.workflows.visualization as module
    from ricky.browser.chrome import ChromeDiscoveryError

    calls = []

    async def opened(path, *, settings, scope):
        calls.append(validate_visualization_path(path, settings=settings, scope=scope))
        return True

    monkeypatch.setattr(cli, "open_visualization", opened)
    result = CliRunner().invoke(app, ["workflow", "visualize", "email-triage", "--open-chrome"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1 and calls[0].is_file()

    async def unavailable(settings):
        raise ChromeDiscoveryError("Google Chrome Stable is unavailable")

    monkeypatch.setattr(cli, "open_visualization", module.open_visualization)
    monkeypatch.setattr(module, "require_chrome", unavailable)
    result = CliRunner().invoke(app, ["workflow", "open-view", str(calls[0])])
    assert result.exit_code != 0
    assert "Google Chrome Stable is unavailable" in result.output
    assert calls[0].is_file()


def test_invocation_node_does_not_collide_with_a_step_named_trigger(design):
    bundle, _, _ = design
    spec = bundle / "workflow.toml"
    spec.write_text(
        spec.read_text()
        .replace('"context"', '"trigger"')
        .replace("steps.context.output", "steps.trigger.output")
    )
    view = inspect(design)
    assert any(step.id == "trigger" for step in view.steps)
    assert any(
        edge.source == "$trigger" and edge.reference == "trigger.emails" for edge in view.edges
    )
    assert any(
        edge.source == "trigger" and edge.reference == "steps.trigger.output.body"
        for edge in view.edges
    )
