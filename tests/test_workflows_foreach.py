"""Workflow persistent foreach tests."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, JsonValue

from ricky.agent.session import AgentSession
from ricky.agent.workflow import WorkflowRunner
from ricky.config import RickySettings
from ricky.profiles import ProfileResourceRef
from ricky.tools import Risk, ToolContext, ToolRegistry, ToolResult
from ricky.workflows.compile import compile_workflow
from ricky.workflows.run import WorkflowRun, WorkflowSourceIdentity
from ricky.workflows.spec import WorkflowSpec


class SourceParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pass


class SourceResult(BaseModel):
    items: list[dict[str, str]]


class SourceTool:
    name: ClassVar[str] = "synthetic_source"
    description: ClassVar[str] = "Return synthetic records."
    Params: ClassVar[type[BaseModel]] = SourceParams
    Result: ClassVar[type[BaseModel]] = SourceResult
    risk: ClassVar[Risk] = "read_only"
    capability_id = None
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        _ = params, ctx
        return ToolResult(
            content="changeable display",
            data={"items": [{"id": "a"}, {"id": "b"}, {"id": "c"}]},
        )


class EchoParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    record_id: str


class EchoResult(BaseModel):
    record_id: str


class EchoTool:
    name: ClassVar[str] = "synthetic_echo"
    description: ClassVar[str] = "Echo one synthetic record id."
    Params: ClassVar[type[BaseModel]] = EchoParams
    Result: ClassVar[type[BaseModel]] = EchoResult
    risk: ClassVar[Risk] = "read_only"
    capability_id = None
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        _ = ctx
        parsed = EchoParams.model_validate(params)
        return ToolResult(content="other display", data={"record_id": parsed.record_id})


class InterruptibleEchoTool(EchoTool):
    """Complete one item and block the other items until resume."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.slow_started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        _ = ctx
        parsed = EchoParams.model_validate(params)
        self.calls[parsed.record_id] = self.calls.get(parsed.record_id, 0) + 1
        if parsed.record_id != "a":
            self.slow_started.set()
            await self.release.wait()
        return ToolResult(content="other display", data={"record_id": parsed.record_id})


class DelayedEchoTool(EchoTool):
    """Measure concurrent item tool calls."""

    def __init__(self) -> None:
        self.active = 0
        self.maximum_active = 0

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        _ = ctx
        parsed = EchoParams.model_validate(params)
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            await asyncio.sleep(0.01)
            return ToolResult(content="other display", data={"record_id": parsed.record_id})
        finally:
            self.active -= 1


async def test_foreach_preserves_order_and_item_context_isolation() -> None:
    spec = WorkflowSpec.model_validate(
        {
            "version": 2,
            "name": "collection-graph",
            "description": "Test item graphs.",
            "steps": [
                {
                    "id": "source",
                    "kind": "tool",
                    "tool": "synthetic_source",
                },
                {
                    "id": "each",
                    "kind": "foreach",
                    "needs": ["source"],
                    "collection": {"ref": "steps.source.output.items"},
                    "item_key": {"ref": "item.source.id"},
                    "on_item_error": "collect",
                    "body": [
                        {
                            "id": "echo",
                            "kind": "tool",
                            "tool": "synthetic_echo",
                            "args": {"record_id": {"ref": "item.source.id"}},
                        },
                        {
                            "id": "report",
                            "kind": "message",
                            "needs": ["echo"],
                            "message": {
                                "format": "item {id}",
                                "values": {"id": {"ref": "item.steps.echo.output.record_id"}},
                            },
                        },
                    ],
                },
            ],
        }
    )
    settings = RickySettings(user_data_dir=".test-ricky")
    registry = ToolRegistry([SourceTool(), EchoTool()])
    compiled = compile_workflow(
        spec,
        tool_registry=registry,
        settings=settings.workflow,
    )
    assert compiled.graph is not None, compiled.errors
    runner = WorkflowRunner(
        graph=compiled.graph,
        provider=None,
        tool_registry=registry,
        settings=settings,
        session=AgentSession.create(
            settings,
            profile_scope=settings.resolve_profile_scope(),
            provider="openrouter",
            model="synthetic",
        ),
        source=WorkflowSourceIdentity(
            resource=ProfileResourceRef(profile="personal", name="fixture"),
            path=str(Path(".ricky/workflows/collection/workflow.toml").resolve()),
            scope="fixture",
            content_digest="synthetic",
        ),
    )

    run = await runner.start({})

    assert run.status == "completed"
    items = run.item_runs["each"]
    assert [item.key for item in items] == ["a", "b", "c"]
    assert [item.steps["echo"].output for item in items] == [
        {"record_id": "a"},
        {"record_id": "b"},
        {"record_id": "c"},
    ]
    assert [item.steps["report"].output for item in items] == [
        {"text": "item a"},
        {"text": "item b"},
        {"text": "item c"},
    ]
    assert isinstance(run.steps["each"].output, list)
    output = run.steps["each"].output
    assert all(isinstance(entry, dict) for entry in output)
    assert [entry["key"] for entry in output if isinstance(entry, dict)] == [
        "a",
        "b",
        "c",
    ]


async def test_foreach_projection_keeps_full_resume_state_out_of_step_output() -> None:
    spec = WorkflowSpec.model_validate(
        {
            "version": 2,
            "name": "projected-collection",
            "description": "Keep durable item records and expose compact outputs.",
            "steps": [
                {"id": "source", "kind": "tool", "tool": "synthetic_source"},
                {
                    "id": "each",
                    "kind": "foreach",
                    "needs": ["source"],
                    "collection": {"ref": "steps.source.output.items"},
                    "item_key": {"ref": "item.source.id"},
                    "outputs": {
                        "echo": {"ref": "item.steps.echo.output"},
                    },
                    "body": [
                        {
                            "id": "echo",
                            "kind": "tool",
                            "tool": "synthetic_echo",
                            "args": {"record_id": {"ref": "item.source.id"}},
                        }
                    ],
                },
            ],
        }
    )
    settings = RickySettings(user_data_dir=".test-ricky")
    registry = ToolRegistry([SourceTool(), EchoTool()])
    compiled = compile_workflow(
        spec,
        tool_registry=registry,
        settings=settings.workflow,
    )
    assert compiled.graph is not None, compiled.errors
    runner = WorkflowRunner(
        graph=compiled.graph,
        provider=None,
        tool_registry=registry,
        settings=settings,
        session=AgentSession.create(
            settings,
            profile_scope=settings.resolve_profile_scope(),
            provider="openrouter",
            model="synthetic",
        ),
        source=WorkflowSourceIdentity(
            resource=ProfileResourceRef(profile="personal", name="fixture"),
            path=str(Path(".ricky/workflows/projected/workflow.toml").resolve()),
            scope="fixture",
            content_digest="synthetic",
        ),
    )

    run = await runner.start({})

    assert run.status == "completed"
    assert run.item_runs["each"][0].source == {"id": "a"}
    assert run.item_runs["each"][0].steps["echo"].output == {"record_id": "a"}
    assert run.steps["each"].output == [
        {
            "key": key,
            "index": index,
            "status": "completed",
            "error": None,
            "output": {"echo": {"record_id": key}},
        }
        for index, key in enumerate(["a", "b", "c"])
    ]


async def test_large_item_records_remain_resumable_without_becoming_context() -> None:
    spec = WorkflowSpec.model_validate(
        {
            "version": 2,
            "name": "large-item-state",
            "description": "Separate large recovery state from small bindings.",
            "steps": [
                {"id": "source", "kind": "tool", "tool": "synthetic_source"},
                {
                    "id": "each",
                    "kind": "foreach",
                    "needs": ["source"],
                    "collection": {"ref": "steps.source.output.items"},
                    "item_key": {"ref": "item.source.id"},
                    "outputs": {
                        "classification": {"ref": "item.steps.classify.output"},
                    },
                    "body": [
                        {"id": "read", "kind": "message", "message": "read"},
                        {
                            "id": "classify",
                            "kind": "message",
                            "needs": ["read"],
                            "message": "classified",
                        },
                    ],
                },
                {
                    "id": "done",
                    "kind": "message",
                    "needs": ["each"],
                    "message": "done",
                },
            ],
        }
    )
    settings = RickySettings(user_data_dir=".test-ricky")
    settings.workflow.max_binding_chars = 1_000
    registry = ToolRegistry([SourceTool()])
    compiled = compile_workflow(
        spec,
        tool_registry=registry,
        settings=settings.workflow,
    )
    assert compiled.graph is not None, compiled.errors
    snapshots: list[WorkflowRun] = []

    async def checkpoint(run: WorkflowRun) -> None:
        snapshots.append(WorkflowRun.model_validate_json(run.model_dump_json()))

    large_output: JsonValue = {"text": "x" * 5_000}
    runner = WorkflowRunner(
        graph=compiled.graph,
        provider=None,
        tool_registry=registry,
        settings=settings,
        session=AgentSession.create(
            settings,
            profile_scope=settings.resolve_profile_scope(),
            provider="openrouter",
            model="synthetic",
        ),
        source=WorkflowSourceIdentity(
            resource=ProfileResourceRef(profile="personal", name="fixture"),
            path=str(Path(".ricky/workflows/large-state/workflow.toml").resolve()),
            scope="fixture",
            content_digest="synthetic",
        ),
        checkpoint=checkpoint,
        fixtures={f'each/"{key}"/read': large_output for key in ("a", "b", "c")},
    )

    run = await runner.start({})

    assert run.status == "completed"
    assert snapshots[-1] == run
    assert run.item_runs["each"][0].steps["read"].output == large_output
    assert run.steps["each"].output == [
        {
            "key": key,
            "index": index,
            "status": "completed",
            "error": None,
            "output": {"classification": {"text": "classified"}},
        }
        for index, key in enumerate(["a", "b", "c"])
    ]


async def test_foreach_cancellation_preserves_completed_items_for_resume() -> None:
    spec = WorkflowSpec.model_validate(
        {
            "version": 2,
            "name": "resumable-collection",
            "description": "Test item interruption and resume.",
            "steps": [
                {"id": "source", "kind": "tool", "tool": "synthetic_source"},
                {
                    "id": "each",
                    "kind": "foreach",
                    "needs": ["source"],
                    "collection": {"ref": "steps.source.output.items"},
                    "item_key": {"ref": "item.source.id"},
                    "body": [
                        {
                            "id": "echo",
                            "kind": "tool",
                            "tool": "synthetic_echo",
                            "args": {"record_id": {"ref": "item.source.id"}},
                        }
                    ],
                },
            ],
        }
    )
    settings = RickySettings(user_data_dir=".test-ricky")
    echo = InterruptibleEchoTool()
    registry = ToolRegistry([SourceTool(), echo])
    compiled = compile_workflow(
        spec,
        tool_registry=registry,
        settings=settings.workflow,
    )
    assert compiled.graph is not None, compiled.errors
    snapshots: list[WorkflowRun] = []

    async def checkpoint(run: WorkflowRun) -> None:
        snapshots.append(run.model_copy(deep=True))

    runner = WorkflowRunner(
        graph=compiled.graph,
        provider=None,
        tool_registry=registry,
        settings=settings,
        session=AgentSession.create(
            settings,
            profile_scope=settings.resolve_profile_scope(),
            provider="openrouter",
            model="synthetic",
        ),
        source=WorkflowSourceIdentity(
            resource=ProfileResourceRef(profile="personal", name="fixture"),
            path=str(Path(".ricky/workflows/resumable/workflow.toml").resolve()),
            scope="fixture",
            content_digest="synthetic",
        ),
        checkpoint=checkpoint,
    )
    task = asyncio.create_task(runner.start({}))
    await echo.slow_started.wait()
    while not any(
        run.item_runs.get("each") and run.item_runs["each"][0].status == "completed"
        for run in snapshots
    ):
        await asyncio.sleep(0)

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    interrupted = snapshots[-1]
    items = {item.key: item for item in interrupted.item_runs["each"]}
    assert items["a"].status == "completed"
    assert items["b"].status == "interrupted"
    assert items["c"].status == "interrupted"

    echo.release.set()
    resumed = await runner.resume(interrupted)

    assert resumed.status == "completed"
    assert [item.status for item in resumed.item_runs["each"]] == [
        "completed",
        "completed",
        "completed",
    ]
    assert echo.calls == {"a": 1, "b": 2, "c": 2}


async def test_foreach_respects_item_parallelism_limit() -> None:
    spec = WorkflowSpec.model_validate(
        {
            "version": 2,
            "name": "bounded-collection",
            "description": "Test the item concurrency limit.",
            "steps": [
                {"id": "source", "kind": "tool", "tool": "synthetic_source"},
                {
                    "id": "each",
                    "kind": "foreach",
                    "needs": ["source"],
                    "collection": {"ref": "steps.source.output.items"},
                    "item_key": {"ref": "item.source.id"},
                    "max_parallel_items": 2,
                    "body": [
                        {
                            "id": "echo",
                            "kind": "tool",
                            "tool": "synthetic_echo",
                            "args": {"record_id": {"ref": "item.source.id"}},
                        }
                    ],
                },
            ],
        }
    )
    settings = RickySettings(user_data_dir=".test-ricky")
    echo = DelayedEchoTool()
    registry = ToolRegistry([SourceTool(), echo])
    compiled = compile_workflow(
        spec,
        tool_registry=registry,
        settings=settings.workflow,
    )
    assert compiled.graph is not None, compiled.errors
    runner = WorkflowRunner(
        graph=compiled.graph,
        provider=None,
        tool_registry=registry,
        settings=settings,
        session=AgentSession.create(
            settings,
            profile_scope=settings.resolve_profile_scope(),
            provider="openrouter",
            model="synthetic",
        ),
        source=WorkflowSourceIdentity(
            resource=ProfileResourceRef(profile="personal", name="fixture"),
            path=str(Path(".ricky/workflows/bounded/workflow.toml").resolve()),
            scope="fixture",
            content_digest="synthetic",
        ),
    )

    run = await runner.start({})

    assert run.status == "completed"
    assert echo.maximum_active == 2
