"""Context-neutral workflow framework acceptance references A through E."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, JsonValue

from ricky.agent.events import ToolCallFinishedEvent
from ricky.agent.session import AgentSession
from ricky.agent.workflow import (
    ApprovalRequest,
    ApprovalResponse,
    WorkflowRunner,
)
from ricky.config import RickySettings
from ricky.llm import (
    CompletionRequest,
    Message,
    MessageDone,
    StreamEvent,
    TextPart,
    ToolCallPart,
    Usage,
)
from ricky.permissions import PermissionResponse
from ricky.profiles import ProfileResourceRef, ProfileScope
from ricky.tools import (
    EffectIdentity,
    EffectReceipt,
    Risk,
    ToolContext,
    ToolRegistry,
    ToolResult,
)
from ricky.workflows.compile import compile_workflow
from ricky.workflows.run import (
    EffectJournalEntry,
    StepRecord,
    WorkflowRun,
    WorkflowSourceIdentity,
)
from ricky.workflows.run_store import WorkflowRunStore
from ricky.workflows.spec import WorkflowSpec


class SyntheticParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: JsonValue = None


class SyntheticResult(BaseModel):
    value: JsonValue


class SyntheticReadTool:
    name: ClassVar[str] = "reference_read"
    description: ClassVar[str] = "Return explicit synthetic data."
    Params: ClassVar[type[BaseModel]] = SyntheticParams
    Result: ClassVar[type[BaseModel]] = SyntheticResult
    risk: ClassVar[Risk] = "read_only"
    capability_id = None
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None
    safe_replay: ClassVar[bool] = True

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        _ = ctx
        parsed = SyntheticParams.model_validate(params)
        return ToolResult(content="display is not data", data={"value": parsed.value})


class SyntheticEffectTool(SyntheticReadTool):
    name: ClassVar[str] = "reference_effect"
    description: ClassVar[str] = "Record one synthetic effect."
    risk: ClassVar[Risk] = "mutating"
    effect_kind = "external"

    def __init__(self) -> None:
        self.values: list[JsonValue] = []

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        del ctx
        value = str(args.get("value"))
        return EffectIdentity(
            operation=self.name,
            target="test",
            occurrence=value,
            summary=f"Record synthetic effect {value}",
            action_key="a" * 64,
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        _ = ctx
        parsed = SyntheticParams.model_validate(params)
        self.values.append(parsed.value)
        return ToolResult(
            content="effect complete",
            data={"value": parsed.value},
            effect_receipt=EffectReceipt(disposition="performed"),
        )


class ScriptedProvider:
    name = "scripted"

    def __init__(self, messages: list[Message]) -> None:
        self.messages = messages
        self.requests: list[CompletionRequest] = []

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        yield MessageDone(
            message=self.messages.pop(0),
            usage=Usage(prompt_tokens=1, completion_tokens=1),
        )

    async def aclose(self) -> None:
        pass


def _text(value: str) -> Message:
    return Message(role="assistant", content=[TextPart(text=value)])


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings(
        project_data_dir=str(tmp_path / "project-data"),
        user_data_dir=str(tmp_path / "user-data"),
    )


async def _allow_permission(_event) -> PermissionResponse:
    return PermissionResponse(decision="allow")


async def _approve_all(request: ApprovalRequest) -> ApprovalResponse:
    if request.mode == "confirm":
        return ApprovalResponse(approved=True)
    return ApprovalResponse(approved=True, selected_keys=request.item_keys)


def _runner(
    tmp_path: Path,
    raw: dict[str, object],
    *,
    provider: ScriptedProvider | None = None,
    effect: SyntheticEffectTool | None = None,
    approval=_approve_all,
) -> WorkflowRunner:
    settings = _settings(tmp_path)
    tools = [SyntheticReadTool(), effect or SyntheticEffectTool()]
    registry = ToolRegistry(tools)
    spec = WorkflowSpec.model_validate(raw)
    compiled = compile_workflow(spec, tool_registry=registry, settings=settings.workflow)
    assert compiled.graph is not None, compiled.errors
    return WorkflowRunner(
        graph=compiled.graph,
        provider=provider,
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
            path=str(tmp_path / "workflow.toml"),
            scope="fixture",
            content_digest="synthetic",
        ),
        permission_responder=_allow_permission,
        approval_responder=approval,
        checkpoint=WorkflowRunStore(settings, project_root=tmp_path).save,
    )


async def test_reference_a_linear_typed_pipeline(tmp_path: Path) -> None:
    effect = SyntheticEffectTool()
    provider = ScriptedProvider([_text('{"accepted":true,"label":"record-r1"}')])
    runner = _runner(
        tmp_path,
        {
            "version": 2,
            "name": "reference-a",
            "description": "Linear typed pipeline.",
            "schemas": {
                "extraction": {
                    "type": "object",
                    "required": ["accepted", "label"],
                    "properties": {
                        "accepted": {"type": "boolean"},
                        "label": {"type": "string"},
                    },
                }
            },
            "steps": [
                {
                    "id": "source",
                    "kind": "tool",
                    "tool": "reference_read",
                    "args": {"value": {"id": "r1", "score": 3}},
                },
                {
                    "id": "extract",
                    "kind": "model",
                    "needs": ["source"],
                    "instruction": "Extract the synthetic record.",
                    "inputs": {"record": {"ref": "steps.source.output.value"}},
                    "result_schema": "extraction",
                },
                {
                    "id": "check",
                    "kind": "check",
                    "needs": ["extract"],
                    "check": {
                        "ref": "steps.extract.output.accepted",
                        "is_true": True,
                    },
                },
                {
                    "id": "approve",
                    "kind": "approval",
                    "needs": ["check"],
                    "mode": "confirm",
                    "prompt": "Approve the proposal?",
                    "proposal": {"ref": "steps.extract.output"},
                },
                {
                    "id": "effect",
                    "kind": "tool",
                    "needs": ["approve"],
                    "tool": "reference_effect",
                    "args": {"value": {"ref": "steps.extract.output.label"}},
                },
                {
                    "id": "report",
                    "kind": "message",
                    "needs": ["effect"],
                    "message": "reference A complete",
                },
            ],
        },
        provider=provider,
        effect=effect,
    )

    run = await runner.start({})

    assert run.status == "completed"
    assert effect.values == ["record-r1"]
    assert run.steps["extract"].output == {"accepted": True, "label": "record-r1"}
    assert provider.requests[0].tools == []
    assert runner.session.history == []
    finished = [event for event in runner.events if isinstance(event, ToolCallFinishedEvent)]
    assert [event.tool_name for event in finished] == [
        "reference_read",
        "reference_effect",
    ]
    assert all(event.content_chars > 0 for event in finished)
    assert all(event.data_chars > 0 for event in finished)
    assert all(event.result_model == "SyntheticResult" for event in finished)


async def test_reference_b_fan_out_and_fan_in(tmp_path: Path) -> None:
    provider = ScriptedProvider([_text('{"text":"A"}'), _text('{"text":"B"}')])
    schema = {
        "type": "object",
        "required": ["text"],
        "properties": {"text": {"type": "string"}},
    }
    runner = _runner(
        tmp_path,
        {
            "version": 2,
            "name": "reference-b",
            "description": "Fan-out and fan-in.",
            "schemas": {"text": schema},
            "steps": [
                {
                    "id": "root",
                    "kind": "tool",
                    "tool": "reference_read",
                    "args": {"value": "seed"},
                },
                {
                    "id": "model-a",
                    "kind": "model",
                    "needs": ["root"],
                    "instruction": "Return A.",
                    "result_schema": "text",
                },
                {
                    "id": "model-b",
                    "kind": "model",
                    "needs": ["root"],
                    "instruction": "Return B.",
                    "result_schema": "text",
                },
                {
                    "id": "read-c",
                    "kind": "tool",
                    "needs": ["root"],
                    "tool": "reference_read",
                    "args": {"value": {"c": True}},
                },
                {
                    "id": "merge",
                    "kind": "data",
                    "needs": ["model-a", "model-b", "read-c"],
                    "operator": "merge",
                    "args": {
                        "values": [
                            {"ref": "steps.model-a.output"},
                            {"ref": "steps.model-b.output"},
                            {"ref": "steps.read-c.output.value"},
                        ],
                        "mode": "objects",
                    },
                },
                {
                    "id": "report",
                    "kind": "message",
                    "needs": ["merge"],
                    "message": "reference B complete",
                },
            ],
        },
        provider=provider,
    )

    run = await runner.start({})

    assert run.status == "completed"
    assert run.steps["merge"].output == {"value": {"text": "B", "c": True}}
    assert run.steps["merge"].started_at is not None
    for dependency in ("model-a", "model-b", "read-c"):
        finished = run.steps[dependency].finished_at
        started = run.steps["merge"].started_at
        assert finished is not None and started is not None
        assert finished <= started


async def test_reference_c_conditional_agent_graph(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            Message(
                role="assistant",
                content=[
                    ToolCallPart(
                        id="read-1",
                        name="reference_read",
                        args={"value": "fact"},
                    )
                ],
            ),
            _text('{"summary":"fact"}'),
            _text('{"route":"a"}'),
        ]
    )
    runner = _runner(
        tmp_path,
        {
            "version": 2,
            "name": "reference-c",
            "description": "Conditional agent graph.",
            "schemas": {
                "investigation": {
                    "type": "object",
                    "required": ["summary"],
                    "properties": {"summary": {"type": "string"}},
                },
                "decision": {
                    "type": "object",
                    "required": ["route"],
                    "properties": {"route": {"type": "string", "values": ["a", "b"]}},
                },
            },
            "steps": [
                {
                    "id": "investigate",
                    "kind": "agent",
                    "instruction": "Read one synthetic fact.",
                    "result_schema": "investigation",
                    "tools": ["reference_read"],
                },
                {
                    "id": "decide",
                    "kind": "model",
                    "needs": ["investigate"],
                    "instruction": "Choose route a or b.",
                    "inputs": {"fact": {"ref": "steps.investigate.output.summary"}},
                    "result_schema": "decision",
                },
                {
                    "id": "task-a",
                    "kind": "message",
                    "needs": ["decide"],
                    "when": {
                        "ref": "steps.decide.output.route",
                        "equals": "a",
                    },
                    "message": "A",
                },
                {
                    "id": "task-b",
                    "kind": "message",
                    "needs": ["decide"],
                    "when": {
                        "ref": "steps.decide.output.route",
                        "equals": "b",
                    },
                    "message": "B",
                },
                {
                    "id": "report",
                    "kind": "message",
                    "needs": ["task-a", "task-b"],
                    "dependency_policy": "terminal",
                    "message": "reference C complete",
                },
            ],
        },
        provider=provider,
    )

    run = await runner.start({})

    assert run.status == "completed"
    assert run.steps["task-a"].status == "completed"
    assert run.steps["task-b"].status == "skipped"
    assert run.steps["report"].status == "completed"
    assert runner.session.history == []


async def test_reference_d_collection_selection_preserves_identity(tmp_path: Path) -> None:
    effect = SyntheticEffectTool()

    async def select_middle(request: ApprovalRequest) -> ApprovalResponse:
        assert request.item_keys == ["a", "b", "c"]
        return ApprovalResponse(approved=True, selected_keys=["b"])

    runner = _runner(
        tmp_path,
        {
            "version": 2,
            "name": "reference-d",
            "description": "Collection selection and effects.",
            "steps": [
                {
                    "id": "source",
                    "kind": "tool",
                    "tool": "reference_read",
                    "args": {
                        "value": [
                            {"id": "a"},
                            {"id": "b"},
                            {"id": "c"},
                        ]
                    },
                },
                {
                    "id": "select",
                    "kind": "approval",
                    "needs": ["source"],
                    "mode": "select",
                    "prompt": "Select records.",
                    "collection": {"ref": "steps.source.output.value"},
                    "item_key": {"ref": "item.source.id"},
                },
                {
                    "id": "effects",
                    "kind": "foreach",
                    "needs": ["select"],
                    "collection": {"ref": "steps.select.output.approved"},
                    "item_key": {"ref": "item.source.id"},
                    "body": [
                        {
                            "id": "apply",
                            "kind": "tool",
                            "tool": "reference_effect",
                            "args": {"value": {"ref": "item.source.id"}},
                        }
                    ],
                },
            ],
        },
        effect=effect,
        approval=select_middle,
    )

    run = await runner.start({})

    assert run.status == "completed"
    assert effect.values == ["b"]
    assert run.steps["select"].output == {
        "approved": [{"id": "b"}],
        "rejected": [{"id": "a"}, {"id": "c"}],
        "selected_keys": ["b"],
    }
    assert run.item_runs["effects"][0].key == "b"


async def test_reference_e_recovery_requires_reconciliation(tmp_path: Path) -> None:
    effect = SyntheticEffectTool()
    runner = _runner(
        tmp_path,
        {
            "version": 2,
            "name": "reference-e",
            "description": "Effect recovery.",
            "steps": [
                {"id": "pure", "kind": "message", "message": "ready"},
                {
                    "id": "effect",
                    "kind": "tool",
                    "needs": ["pure"],
                    "tool": "reference_effect",
                    "args": {"value": "one"},
                },
                {
                    "id": "report",
                    "kind": "message",
                    "needs": ["effect"],
                    "message": "done",
                },
            ],
        },
        effect=effect,
    )
    run = WorkflowRun(
        profile_scope=ProfileScope.create("personal"),
        workflow_name="reference-e",
        source=runner.source,
        provider="openrouter",
        model="synthetic",
        graph_fingerprint=runner.graph.fingerprint,
        status="running",
        steps={
            "pure": StepRecord(
                step_id="pure",
                execution_address="pure",
                kind="message",
                status="completed",
                output={"text": "ready"},
            ),
            "effect": StepRecord(
                step_id="effect",
                execution_address="effect",
                kind="tool",
                status="running",
            ),
            "report": StepRecord(
                step_id="report",
                execution_address="report",
                kind="message",
            ),
        },
        effect_journal=[
            EffectJournalEntry(
                step_id="effect",
                execution_address="effect",
                tool_name="reference_effect",
                risk="mutating",
                status="dispatched",
            )
        ],
    )

    resumed = await runner.resume(run)

    assert resumed.status == "in_doubt"
    assert resumed.steps["pure"].status == "completed"
    assert resumed.steps["effect"].status == "in_doubt"
    assert resumed.steps["report"].status == "pending"
    assert effect.values == []
