"""Domain-neutral Workflow scheduler tests."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from ricky.agent.session import AgentSession
from ricky.agent.workflow import WorkflowRunner
from ricky.config import RickySettings
from ricky.profiles import ProfileResourceRef
from ricky.tools import Risk, ToolContext, ToolRegistry, ToolResult
from ricky.workflows.compile import CompiledGraph, compile_workflow
from ricky.workflows.run import WorkflowRun, WorkflowSourceIdentity
from ricky.workflows.spec import WorkflowSpec


class ReadParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    label: str
    delay: float = 0
    fail: bool = False


class ReadResult(BaseModel):
    value: list[str]


class ControlledReadTool:
    name: ClassVar[str] = "controlled_read"
    description: ClassVar[str] = "Return a synthetic typed value."
    Params: ClassVar[type[BaseModel]] = ReadParams
    Result: ClassVar[type[BaseModel]] = ReadResult
    risk: ClassVar[Risk] = "read_only"
    capability_id = None
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None
    safe_replay: ClassVar[bool] = True

    def __init__(self) -> None:
        self.active = 0
        self.maximum_active = 0
        self.started = asyncio.Event()

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        _ = ctx
        parsed = ReadParams.model_validate(params)
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.started.set()
        try:
            if parsed.delay:
                await asyncio.sleep(parsed.delay)
            if parsed.fail:
                return ToolResult(content="synthetic failure", is_error=True)
            return ToolResult(content="display text", data={"value": [parsed.label]})
        finally:
            self.active -= 1


def _settings() -> RickySettings:
    return RickySettings(user_data_dir=".test-ricky")


def _runner(
    raw: dict[str, object], tool: ControlledReadTool
) -> tuple[WorkflowRunner, CompiledGraph]:
    spec = WorkflowSpec.model_validate(raw)
    settings = _settings()
    registry = ToolRegistry([tool])
    result = compile_workflow(
        spec,
        tool_registry=registry,
        settings=settings.workflow,
    )
    assert result.graph is not None, result.errors
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="synthetic",
    )
    runner = WorkflowRunner(
        graph=result.graph,
        provider=None,
        tool_registry=registry,
        settings=settings,
        session=session,
        source=WorkflowSourceIdentity(
            resource=ProfileResourceRef(profile="personal", name="fixture"),
            path=str(Path(".ricky/workflows/synthetic/workflow.toml").resolve()),
            scope="fixture",
            content_digest="synthetic",
        ),
    )
    return runner, result.graph


async def test_fan_out_runs_in_parallel_and_fan_in_waits() -> None:
    tool = ControlledReadTool()
    runner, _ = _runner(
        {
            "version": 2,
            "name": "fan-graph",
            "description": "Test fan-out and fan-in.",
            "max_parallel_steps": 2,
            "steps": [
                {
                    "id": "left",
                    "kind": "tool",
                    "tool": "controlled_read",
                    "args": {"label": "left", "delay": 0.02},
                },
                {
                    "id": "right",
                    "kind": "tool",
                    "tool": "controlled_read",
                    "args": {"label": "right", "delay": 0.02},
                },
                {
                    "id": "third",
                    "kind": "tool",
                    "tool": "controlled_read",
                    "args": {"label": "third", "delay": 0.02},
                },
                {
                    "id": "join",
                    "kind": "data",
                    "operator": "merge",
                    "needs": ["left", "right", "third"],
                    "args": {
                        "values": [
                            {"ref": "steps.left.output.value"},
                            {"ref": "steps.right.output.value"},
                            {"ref": "steps.third.output.value"},
                        ],
                        "mode": "lists",
                    },
                },
            ],
        },
        tool,
    )

    run = await runner.start({})

    assert run.status == "completed"
    assert tool.maximum_active == 2
    assert run.steps["join"].output == {"value": ["left", "right", "third"]}
    started = [
        event.step_id
        for event in runner.events
        if event.kind == "workflow" and event.action == "step_started"
    ]
    assert started[:2] == ["left", "right"]
    assert started[2] == "third"
    assert started[-1] == "join"


async def test_large_unrelated_state_does_not_count_as_a_step_binding() -> None:
    tool = ControlledReadTool()
    runner, _ = _runner(
        {
            "version": 2,
            "name": "scoped-binding",
            "description": "Bind only the declared small branch.",
            "steps": [
                {
                    "id": "large",
                    "kind": "tool",
                    "tool": "controlled_read",
                    "args": {"label": "large"},
                },
                {
                    "id": "small",
                    "kind": "tool",
                    "tool": "controlled_read",
                    "args": {"label": "small"},
                },
                {
                    "id": "join",
                    "kind": "data",
                    "operator": "merge",
                    "needs": ["large", "small"],
                    "args": {
                        "values": [{"ref": "steps.small.output.value"}],
                        "mode": "lists",
                    },
                },
            ],
        },
        tool,
    )
    runner.settings.workflow.max_binding_chars = 1_000
    runner.fixtures["large"] = {"value": ["x" * 5_000]}

    run = await runner.start({})

    assert run.status == "completed"
    assert run.steps["join"].output == {"value": ["small"]}


async def test_oversized_declared_step_binding_fails_closed() -> None:
    tool = ControlledReadTool()
    runner, _ = _runner(
        {
            "version": 2,
            "name": "oversized-binding",
            "description": "Reject one declared oversized binding.",
            "steps": [
                {
                    "id": "large",
                    "kind": "tool",
                    "tool": "controlled_read",
                    "args": {"label": "large"},
                },
                {
                    "id": "consume",
                    "kind": "data",
                    "operator": "merge",
                    "needs": ["large"],
                    "args": {
                        "values": [{"ref": "steps.large.output.value"}],
                        "mode": "lists",
                    },
                },
            ],
        },
        tool,
    )
    runner.settings.workflow.max_binding_chars = 1_000
    runner.fixtures["large"] = {"value": ["x" * 5_000]}

    run = await runner.start({})

    assert run.status == "failed"
    assert run.steps["consume"].status == "failed"
    assert run.steps["consume"].error is not None
    assert "workflow step binding" in run.steps["consume"].error.message


async def test_skip_block_and_terminal_policy_are_explicit() -> None:
    tool = ControlledReadTool()
    runner, _ = _runner(
        {
            "version": 2,
            "name": "failure-graph",
            "description": "Test conditional and failure policies.",
            "steps": [
                {
                    "id": "source",
                    "kind": "tool",
                    "tool": "controlled_read",
                    "args": {"label": "source"},
                },
                {
                    "id": "skipped",
                    "kind": "message",
                    "needs": ["source"],
                    "when": {
                        "ref": "steps.source.status",
                        "equals": "failed",
                    },
                    "message": "must not emit",
                },
                {
                    "id": "fails",
                    "kind": "tool",
                    "needs": ["source"],
                    "tool": "controlled_read",
                    "args": {"label": "bad", "fail": True},
                    "on_error": "continue",
                },
                {
                    "id": "blocked",
                    "kind": "message",
                    "needs": ["fails"],
                    "message": "must not emit",
                },
                {
                    "id": "report",
                    "kind": "message",
                    "needs": ["fails"],
                    "dependency_policy": "terminal",
                    "message": {
                        "format": "failed status: {status}",
                        "values": {"status": {"ref": "steps.fails.status"}},
                    },
                },
            ],
        },
        tool,
    )

    run = await runner.start({})

    assert run.status == "completed_with_errors"
    assert run.steps["skipped"].status == "skipped"
    assert run.steps["fails"].status == "failed"
    assert run.steps["blocked"].status == "blocked"
    assert run.steps["blocked"].blocked_by == ["fails"]
    assert run.steps["report"].output == {"text": "failed status: failed"}


async def test_cancellation_awaits_owned_steps_and_leaves_no_running_record() -> None:
    tool = ControlledReadTool()
    runner, _ = _runner(
        {
            "version": 2,
            "name": "cancel-graph",
            "description": "Test owned cancellation.",
            "steps": [
                {
                    "id": "slow-left",
                    "kind": "tool",
                    "tool": "controlled_read",
                    "args": {"label": "left", "delay": 30},
                },
                {
                    "id": "slow-right",
                    "kind": "tool",
                    "tool": "controlled_read",
                    "args": {"label": "right", "delay": 30},
                },
            ],
        },
        tool,
    )
    snapshots: list[WorkflowRun] = []

    async def checkpoint(run: WorkflowRun) -> None:
        snapshots.append(run.model_copy(deep=True))

    runner.checkpoint = checkpoint
    task = asyncio.create_task(runner.start({}))
    while tool.active < 2:
        await asyncio.sleep(0)

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    interrupted = [
        event
        for event in runner.events
        if event.kind == "workflow" and event.action == "step_interrupted"
    ]
    assert len(interrupted) == 2
    assert {event.execution_address for event in interrupted} == {
        "slow-left",
        "slow-right",
    }
    assert tool.active == 0
    final = snapshots[-1]
    assert final.status == "interrupted"
    assert {record.status for record in final.steps.values()} == {"interrupted"}


async def test_cancellation_during_running_transition_settles_checkpoint() -> None:
    tool = ControlledReadTool()
    runner, _ = _runner(
        {
            "version": 2,
            "name": "cancel-running-transition",
            "description": "Test cancellation while persisting the running state.",
            "steps": [{"id": "report", "kind": "message", "message": "done"}],
        },
        tool,
    )
    running_save_started = asyncio.Event()
    block_first_running_save = True
    snapshots: list[WorkflowRun] = []

    async def checkpoint(run: WorkflowRun) -> None:
        nonlocal block_first_running_save
        if run.status == "running" and block_first_running_save:
            block_first_running_save = False
            running_save_started.set()
            await asyncio.Event().wait()
        snapshots.append(run.model_copy(deep=True))

    runner.checkpoint = checkpoint
    task = asyncio.create_task(runner.start({}))
    await running_save_started.wait()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert snapshots[-1].status == "interrupted"
    assert snapshots[-1].steps["report"].status == "pending"


async def test_fail_fast_cancels_and_awaits_independent_owned_branch() -> None:
    tool = ControlledReadTool()
    runner, _ = _runner(
        {
            "version": 2,
            "name": "fail-fast-graph",
            "description": "Test fail-fast branch cleanup.",
            "steps": [
                {
                    "id": "fails",
                    "kind": "tool",
                    "tool": "controlled_read",
                    "args": {"label": "failure", "delay": 0.01, "fail": True},
                    "on_error": "fail_workflow",
                },
                {
                    "id": "slow",
                    "kind": "tool",
                    "tool": "controlled_read",
                    "args": {"label": "slow", "delay": 30},
                },
            ],
        },
        tool,
    )

    run = await runner.start({})

    assert run.status == "failed"
    assert run.steps["fails"].status == "failed"
    assert run.steps["slow"].status == "interrupted"
    assert tool.active == 0
