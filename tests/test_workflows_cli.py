"""Domain-neutral Workflow CLI lifecycle tests."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import cast

from typer.testing import CliRunner

import ricky.interfaces.cli.workflows as cli_workflows
from ricky.agent.context import assemble_context
from ricky.agent.events import WorkflowEvent
from ricky.agent.session import AgentSession
from ricky.agent.workflow import WorkflowService
from ricky.config import RickySettings
from ricky.interfaces.cli.app import app
from ricky.llm import TextPart
from ricky.profiles import ProfileResourceRef
from ricky.tools import ToolContext, ToolRegistry
from ricky.workflows.registry import LoadedWorkflow, WorkflowRegistry
from ricky.workflows.run import WorkflowRun
from ricky.workflows.run_store import WorkflowRunStore
from ricky.workflows.spec import WorkflowSpec, parse_workflow_toml
from ricky.workflows.tool import StartWorkflowParams, StartWorkflowTool


def _bundle(bundled_root: Path) -> None:
    bundle = bundled_root / "workflows" / "generic"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "workflow.toml").write_text(
        """
version = 2
name = "generic"
description = "Exercise the version-aware CLI."

[args.count]
type = "integer"
description = "Synthetic count."

[args.enabled]
type = "boolean"
description = "Synthetic flag."

[[steps]]
id = "review"
kind = "approval"
mode = "select"
prompt = "Review one synthetic item."
collection = [{ id = "item-1" }]
item_key = { ref = "item.source.id" }

[[steps]]
id = "report"
kind = "message"
needs = ["review"]

[steps.message]
format = "count={count}; enabled={enabled}"
values = { count = { ref = "trigger.count" }, enabled = { ref = "trigger.enabled" } }
""",
        encoding="utf-8",
    )


class BlockingRunStore(WorkflowRunStore):
    """Keep a run active so the test can observe the first event."""

    def __init__(self, settings: RickySettings) -> None:
        super().__init__(settings)
        self.release = asyncio.Event()

    async def save(self, run: WorkflowRun) -> None:
        _ = run
        await self.release.wait()


def test_cli_validate_show_and_fixture_free_dryrun(
    tmp_path: Path, bundled_root: Path, monkeypatch
) -> None:
    _bundle(bundled_root)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    validated = runner.invoke(app, ["workflow", "validate", "generic"])
    shown = runner.invoke(app, ["workflow", "show", "generic"])
    dryrun = runner.invoke(
        app,
        [
            "workflow",
            "dryrun",
            "generic",
            "--args",
            "count=2 enabled=true",
        ],
    )

    assert validated.exit_code == 0, validated.output
    assert "is valid" in validated.output
    assert shown.exit_code == 0, shown.output
    assert "version: 2" in shown.output
    assert "roots: review" in shown.output
    assert dryrun.exit_code == 0, dryrun.output
    assert "Dry run auto-denied this approval" in dryrun.output
    assert "count=2; enabled=true" in dryrun.output
    assert "No mutating tool ran" in dryrun.output


def test_workflow_help_lists_lifecycle_commands() -> None:
    result = CliRunner().invoke(app, ["workflow", "--help"])

    assert result.exit_code == 0
    for command in ("run", "status", "resume", "abandon", "reconcile", "dryrun"):
        assert command in result.output


def test_cli_run_status_resume_and_completed_abandon_refusal(
    tmp_path: Path, bundled_root: Path, monkeypatch
) -> None:
    _bundle(bundled_root)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    started = runner.invoke(
        app,
        [
            "workflow",
            "run",
            "generic",
            "--args",
            "count=2 enabled=true",
        ],
        input="none\n",
    )
    match = re.search(r"Workflow run (workflow_[a-z0-9]+): completed", started.output)

    assert started.exit_code == 0, started.output
    assert match is not None, started.output
    run_id = match.group(1)

    status = runner.invoke(app, ["workflow", "status", run_id])
    resumed = runner.invoke(app, ["workflow", "resume", run_id])
    abandoned = runner.invoke(app, ["workflow", "abandon", run_id])

    assert status.exit_code == 0, status.output
    assert "status: completed" in status.output
    assert resumed.exit_code == 0, resumed.output
    assert f"Workflow run {run_id}: completed" in resumed.output
    assert abandoned.exit_code == 2
    assert "cannot abandon completed" in abandoned.output


def test_cli_run_and_resume_use_persisted_profile_workflow_ceilings(
    tmp_path: Path,
    bundled_root: Path,
    monkeypatch,
) -> None:
    _bundle(bundled_root)
    monkeypatch.chdir(tmp_path)
    work_config = tmp_path / "user-data" / "profiles" / "work" / "ricky.toml"
    work_config.parent.mkdir(parents=True)
    work_config.write_text(
        """[workflow]
max_parallel_steps = 1
agent_iterations = 2
model_attempts = 1
max_result_chars = 1000
""",
        encoding="utf-8",
    )
    observed: list[tuple[int, int, int, int]] = []
    original_runner = cli_workflows.WorkflowRunner

    def capture_runner(*args, **kwargs):
        runtime_settings = kwargs["settings"]
        observed.append(
            (
                runtime_settings.workflow.max_parallel_steps,
                runtime_settings.workflow.agent_iterations,
                runtime_settings.workflow.model_attempts,
                runtime_settings.workflow.max_result_chars,
            )
        )
        return original_runner(*args, **kwargs)

    monkeypatch.setattr(cli_workflows, "WorkflowRunner", capture_runner)
    runner = CliRunner()
    started = runner.invoke(
        app,
        [
            "workflow",
            "run",
            "generic",
            "--profile",
            "work",
            "--args",
            "count=2 enabled=true",
        ],
        input="none\n",
    )
    match = re.search(r"Workflow run (workflow_[a-z0-9]+): completed", started.output)
    assert started.exit_code == 0, started.output
    assert match is not None

    resumed = runner.invoke(
        app,
        [
            "workflow",
            "resume",
            match.group(1),
            "--profile",
            "personal",
            "--access-profile",
            "work",
        ],
    )

    assert resumed.exit_code == 0, resumed.output
    assert observed == [(1, 2, 1, 1000), (1, 2, 1, 1000)]


async def test_start_workflow_queues_typed_invocation(tmp_path: Path) -> None:
    settings = RickySettings()
    spec = WorkflowSpec.model_validate(
        {
            "version": 2,
            "name": "queued",
            "description": "Queue typed arguments.",
            "args": {
                "count": {
                    "type": "integer",
                    "description": "Synthetic count.",
                }
            },
            "steps": [{"id": "done", "kind": "message", "message": "done"}],
        }
    )
    tool = StartWorkflowTool(
        WorkflowRegistry(
            [
                LoadedWorkflow(
                    spec=spec,
                    bundle_path=tmp_path,
                    resource=ProfileResourceRef(profile="personal", name=spec.name),
                )
            ]
        )
    )
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="synthetic",
    )
    ctx = ToolContext(cwd=tmp_path, settings=settings, session=session)

    invalid = await tool.run(StartWorkflowParams(name="queued", args={"count": "2"}), ctx)
    valid = await tool.run(StartWorkflowParams(name="queued", args={"count": 2}), ctx)

    assert invalid.is_error is True
    assert valid.is_error is False
    assert session.active_workflow is not None
    assert session.active_workflow.args == {"count": 2}
    restored = AgentSession.model_validate_json(session.model_dump_json())
    assert restored.active_workflow == session.active_workflow


async def test_service_streams_events_before_run_completion(
    tmp_path: Path, bundled_root: Path
) -> None:
    _bundle(bundled_root)
    settings = RickySettings()
    spec = parse_workflow_toml(
        (bundled_root / "workflows" / "generic" / "workflow.toml").read_text()
    )
    registry = WorkflowRegistry(
        [
            LoadedWorkflow(
                spec=spec,
                bundle_path=bundled_root / "workflows" / "generic",
                resource=ProfileResourceRef(profile="personal", name=spec.name),
            )
        ]
    )
    store = BlockingRunStore(settings)
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="synthetic",
    )
    service = WorkflowService(
        provider=None,
        tool_registry=ToolRegistry([]),
        settings=settings,
        workflow_registry=registry,
        run_store=store,
        cwd=tmp_path,
    )

    stream = service.start(
        session,
        "generic",
        {"count": 2, "enabled": True},
    )
    first = await asyncio.wait_for(anext(stream), timeout=1)

    assert isinstance(first, WorkflowEvent)
    assert first.action == "run_created"
    assert session.active_workflow is not None

    await cast(AsyncGenerator[object, None], stream).aclose()

    assert session.active_workflow is None


async def test_service_records_completed_workflow_result_in_session_history(
    tmp_path: Path,
    bundled_root: Path,
) -> None:
    bundle = bundled_root / "workflows" / "terminal"
    bundle.mkdir(parents=True, exist_ok=True)
    source = bundle / "workflow.toml"
    source.write_text(
        """
version = 2
name = "terminal"
description = "Emit a terminal result."

[[steps]]
id = "summary"
kind = "message"
message = "20 messages moved to Trash."
""",
        encoding="utf-8",
    )
    settings = RickySettings()
    registry = WorkflowRegistry(
        [
            LoadedWorkflow(
                spec=parse_workflow_toml(source.read_text()),
                bundle_path=bundle,
                resource=ProfileResourceRef(profile="personal", name="terminal"),
            )
        ]
    )
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="synthetic",
    )
    service = WorkflowService(
        provider=None,
        tool_registry=ToolRegistry([]),
        settings=settings,
        workflow_registry=registry,
        cwd=tmp_path,
    )

    events = [event async for event in service.start(session, "terminal", {})]

    assert any(
        isinstance(event, WorkflowEvent)
        and event.action == "run_completed"
        and event.details["status"] == "completed"
        for event in events
    )
    assert session.active_workflow is None
    completion = session.history[-1]
    assert completion.role == "assistant"
    assert isinstance(completion.content[0], TextPart)
    assert "- workflow: terminal" in completion.content[0].text
    assert "- status: completed" in completion.content[0].text
    assert "20 messages moved to Trash." in completion.content[0].text

    assembly = assemble_context(
        session,
        ToolRegistry([]),
        turn_id="turn_after_workflow",
        iteration=1,
        user_input="nice work",
        cwd=tmp_path,
    )
    assert completion in assembly.request.messages


async def test_service_records_failed_workflow_status_in_session_history(
    tmp_path: Path, bundled_root: Path
) -> None:
    bundle = bundled_root / "workflows" / "terminal-failure"
    bundle.mkdir(parents=True, exist_ok=True)
    source = bundle / "workflow.toml"
    source.write_text(
        """
version = 2
name = "terminal-failure"
description = "Fail without a provider."

[schemas.answer]
type = "object"
required = ["answer"]

[schemas.answer.properties.answer]
type = "string"

[[steps]]
id = "fail"
kind = "model"
instruction = "Return an answer."
result_schema = "answer"
""",
        encoding="utf-8",
    )
    settings = RickySettings()
    registry = WorkflowRegistry(
        [
            LoadedWorkflow(
                spec=parse_workflow_toml(source.read_text()),
                bundle_path=bundle,
                resource=ProfileResourceRef(profile="personal", name="terminal-failure"),
            )
        ]
    )
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="synthetic",
    )
    service = WorkflowService(
        provider=None,
        tool_registry=ToolRegistry([]),
        settings=settings,
        workflow_registry=registry,
        cwd=tmp_path,
    )

    events = [event async for event in service.start(session, "terminal-failure", {})]

    assert any(
        isinstance(event, WorkflowEvent)
        and event.action == "run_completed"
        and event.details["status"] == "failed"
        for event in events
    )
    completion = session.history[-1]
    assert completion.role == "assistant"
    assert isinstance(completion.content[0], TextPart)
    assert "- status: failed" in completion.content[0].text
