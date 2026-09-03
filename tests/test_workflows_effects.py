"""Workflow permission, journal, dry-run, and in-doubt tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel, ConfigDict

from ricky.agent.events import PermissionRequestedEvent
from ricky.agent.session import AgentSession
from ricky.agent.workflow import (
    ApprovalRequest,
    ApprovalResponse,
    WorkflowRunner,
)
from ricky.config import RickySettings
from ricky.permissions import PermissionResponse
from ricky.profiles import ProfileResourceRef, ProfileScope
from ricky.tools import (
    EffectIdentity,
    EffectReceipt,
    PreparedEffect,
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
from ricky.workflows.spec import WorkflowSpec


class EffectParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str


class EffectResult(BaseModel):
    accepted: bool


class SyntheticEffectTool:
    name: ClassVar[str] = "synthetic_effect"
    description: ClassVar[str] = "Perform a synthetic observable effect."
    Params: ClassVar[type[BaseModel]] = EffectParams
    Result: ClassVar[type[BaseModel]] = EffectResult
    risk: ClassVar[Risk] = "mutating"
    capability_id = None
    effect_kind = "external"
    unattended = "allowed"
    state_guard_id = None
    idempotent_replay: ClassVar[bool] = True

    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.maximum_active = 0

    @staticmethod
    def idempotency_key(args: dict[str, object]) -> str:
        return f"synthetic:{args.get('value', '')}"

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        del ctx
        value = str(args.get("value", ""))
        return EffectIdentity(
            operation=self.name,
            target="test",
            occurrence=value,
            summary=f"Synthetic effect {value}",
            action_key="a" * 64,
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        _ = EffectParams.model_validate(params), ctx
        self.calls += 1
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            await asyncio.sleep(0.01)
            return ToolResult(
                content="accepted",
                data={"accepted": True},
                effect_receipt=EffectReceipt(disposition="performed"),
            )
        finally:
            self.active -= 1


class MissingReceiptEffectTool(SyntheticEffectTool):
    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        _ = EffectParams.model_validate(params), ctx
        self.calls += 1
        return ToolResult(content="provider returned no outcome evidence")


@dataclass(frozen=True)
class _PreparedSyntheticEffect:
    tool_name: str
    identity: EffectIdentity
    permission_summary: str | None
    marker: object


class PreparedSyntheticEffectTool(SyntheticEffectTool):
    review_mode = "fresh"

    def __init__(self) -> None:
        super().__init__()
        self.prepare_calls = 0
        self.prepared_dispatches = 0
        self.prepared_marker: object | None = None
        self.dispatched_marker: object | None = None

    async def prepare_effect(
        self,
        args: dict[str, object],
        ctx: ToolContext,
    ) -> PreparedEffect:
        self.prepare_calls += 1
        marker = object()
        self.prepared_marker = marker
        return _PreparedSyntheticEffect(
            tool_name=self.name,
            identity=self.effect_identity(args, ctx),
            permission_summary=f"Review {args['value']}",
            marker=marker,
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del params, ctx
        raise AssertionError("workflow must dispatch the effect prepared before review")

    async def run_prepared(
        self,
        params: BaseModel,
        prepared: PreparedEffect,
        ctx: ToolContext,
    ) -> ToolResult:
        _ = EffectParams.model_validate(params), ctx
        assert isinstance(prepared, _PreparedSyntheticEffect)
        self.prepared_dispatches += 1
        self.dispatched_marker = prepared.marker
        return ToolResult(
            content="accepted",
            data={"accepted": True},
            effect_receipt=EffectReceipt(disposition="performed"),
        )


class SequencedReceiptEffectTool(SyntheticEffectTool):
    def __init__(self, results: list[ToolResult]) -> None:
        super().__init__()
        self._results = results

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        _ = EffectParams.model_validate(params), ctx
        self.calls += 1
        return self._results.pop(0)


class NestedEffectValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class NestedEffectParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    payload: NestedEffectValue


class NestedSyntheticEffectTool(SyntheticEffectTool):
    Params: ClassVar[type[BaseModel]] = NestedEffectParams

    @staticmethod
    def idempotency_key(args: dict[str, object]) -> str:
        payload = args.get("payload")
        return f"synthetic:{payload}"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        parsed = NestedEffectParams.model_validate(params)
        return await super().run(EffectParams(value=parsed.payload.value), ctx)


def _build(
    tool: SyntheticEffectTool,
    *,
    dry_run: bool = False,
    max_attempts: int = 1,
    args: dict[str, object] | None = None,
) -> WorkflowRunner:
    settings = RickySettings(user_data_dir=".test-ricky")
    registry = ToolRegistry([tool])
    spec = WorkflowSpec.model_validate(
        {
            "version": 2,
            "name": "effect-graph",
            "description": "Test an explicit effect.",
            "steps": [
                {
                    "id": "effect",
                    "kind": "tool",
                    "tool": "synthetic_effect",
                    "args": args or {"value": "synthetic"},
                    "retry": {
                        "max_attempts": max_attempts,
                        "on": ["tool_error"] if max_attempts > 1 else [],
                    },
                }
            ],
        }
    )
    compiled = compile_workflow(spec, tool_registry=registry, settings=settings.workflow)
    assert compiled.graph is not None, compiled.errors

    async def allow(_event):
        return PermissionResponse(decision="allow")

    return WorkflowRunner(
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
            path=str(Path(".ricky/workflows/effect/workflow.toml").resolve()),
            scope="fixture",
            content_digest="synthetic",
        ),
        permission_responder=allow,
        dry_run=dry_run,
    )


async def test_effect_journal_closes_known_dispatch() -> None:
    tool = SyntheticEffectTool()
    runner = _build(tool)

    run = await runner.start({})

    assert run.status == "completed"
    assert tool.calls == 1
    assert [entry.status for entry in run.effect_journal] == ["succeeded"]
    actions = [event.action for event in runner.events if event.kind == "workflow"]
    assert actions.index("effect_prepared") < actions.index("effect_dispatched")


async def test_workflow_dispatches_the_exact_fresh_review_prepared_effect_once() -> None:
    tool = PreparedSyntheticEffectTool()
    runner = _build(tool)

    run = await runner.start({})

    assert run.status == "completed"
    assert tool.prepare_calls == 1
    assert tool.calls == 0
    assert tool.prepared_dispatches == 1
    assert tool.dispatched_marker is tool.prepared_marker
    permission_requests = [
        event for event in runner.events if isinstance(event, PermissionRequestedEvent)
    ]
    assert len(permission_requests) == 1
    assert permission_requests[0].offered_grants == []


async def test_workflow_effect_identity_and_dispatch_share_canonical_arguments() -> None:
    tool = NestedSyntheticEffectTool()
    runner = _build(tool, args={"payload": '{"value":"synthetic"}'})

    run = await runner.start({})

    assert run.status == "completed"
    assert run.effect_journal[0].normalized_args == {"payload": {"value": "synthetic"}}
    normalized = [event for event in runner.events if event.kind == "tool_call_normalized"]
    assert len(normalized) == 1
    assert normalized[0].paths == ["payload"]


async def test_dry_run_records_would_dispatch_without_permission_or_call() -> None:
    tool = SyntheticEffectTool()
    runner = _build(tool, dry_run=True)

    run = await runner.start({})

    assert run.status == "completed"
    assert tool.calls == 0
    assert run.effect_journal[0].status == "would_dispatch"
    assert run.steps["effect"].output == {
        "would_dispatch": True,
        "tool": "synthetic_effect",
        "args": {"value": "synthetic"},
    }


async def test_dry_run_never_requests_permission() -> None:
    tool = SyntheticEffectTool()
    runner = _build(tool, dry_run=True)
    permission_requests = 0

    async def unexpected_permission(_event):
        nonlocal permission_requests
        permission_requests += 1
        return PermissionResponse(decision="allow")

    runner.permission_responder = unexpected_permission

    run = await runner.start({})

    assert run.status == "completed"
    assert permission_requests == 0
    assert tool.calls == 0
    assert run.effect_journal[0].status == "would_dispatch"


async def test_resume_converts_dispatched_effect_to_in_doubt_without_replay() -> None:
    tool = SyntheticEffectTool()
    runner = _build(tool)
    run = WorkflowRun(
        profile_scope=ProfileScope.create("personal"),
        workflow_name="effect-graph",
        source=runner.source,
        provider="openrouter",
        model="synthetic",
        graph_fingerprint=runner.graph.fingerprint,
        status="running",
        steps={
            "effect": StepRecord(
                step_id="effect",
                execution_address="effect",
                kind="tool",
                status="running",
            )
        },
        effect_journal=[
            EffectJournalEntry(
                step_id="effect",
                execution_address="effect",
                tool_name="synthetic_effect",
                risk="mutating",
                status="dispatched",
            )
        ],
    )

    resumed = await runner.resume(run)

    assert resumed.status == "in_doubt"
    assert resumed.steps["effect"].status == "in_doubt"
    assert resumed.effect_journal[0].status == "in_doubt"
    assert tool.calls == 0


async def test_workflow_missing_external_receipt_finishes_in_doubt() -> None:
    tool = MissingReceiptEffectTool()
    runner = _build(tool)

    run = await runner.start({})

    assert run.status == "in_doubt"
    assert run.steps["effect"].status == "in_doubt"
    assert run.effect_journal[0].status == "in_doubt"
    error = run.steps["effect"].error
    assert error is not None
    assert "returned no external-effect receipt" in error.message


async def test_resume_reuses_prepared_effect_before_dispatch() -> None:
    tool = SyntheticEffectTool()
    runner = _build(tool)
    run = WorkflowRun(
        profile_scope=ProfileScope.create("personal"),
        workflow_name="effect-graph",
        source=runner.source,
        provider="openrouter",
        model="synthetic",
        graph_fingerprint=runner.graph.fingerprint,
        status="running",
        steps={
            "effect": StepRecord(
                step_id="effect",
                execution_address="effect",
                kind="tool",
                status="running",
            )
        },
        effect_journal=[
            EffectJournalEntry(
                step_id="effect",
                execution_address="effect",
                tool_name="synthetic_effect",
                normalized_args={"value": "synthetic"},
                risk="mutating",
                status="prepared",
            )
        ],
    )

    resumed = await runner.resume(run)

    assert resumed.status == "completed"
    assert tool.calls == 1
    assert len(resumed.effect_journal) == 1
    assert resumed.effect_journal[0].status == "succeeded"


async def test_denied_effect_does_not_use_declared_retry() -> None:
    tool = SyntheticEffectTool()
    runner = _build(tool, max_attempts=3)
    permission_requests = 0

    async def deny(_event):
        nonlocal permission_requests
        permission_requests += 1
        return PermissionResponse(decision="deny")

    runner.permission_responder = deny

    run = await runner.start({})

    assert run.status == "failed"
    assert run.steps["effect"].error is not None
    assert run.steps["effect"].error.category == "permission_denied"
    assert len(run.steps["effect"].attempts) == 1
    assert permission_requests == 1
    assert tool.calls == 0


async def test_not_performed_effect_uses_declared_safe_retry() -> None:
    tool = SequencedReceiptEffectTool(
        [
            ToolResult(
                content="provider rejected the first attempt",
                is_error=True,
                effect_receipt=EffectReceipt(disposition="not_performed"),
            ),
            ToolResult(
                content="accepted",
                data={"accepted": True},
                effect_receipt=EffectReceipt(disposition="performed"),
            ),
        ]
    )
    runner = _build(tool, max_attempts=2)

    run = await runner.start({})

    assert run.status == "completed"
    assert tool.calls == 2
    assert len(run.steps["effect"].attempts) == 2
    assert run.effect_journal[0].status == "succeeded"


async def test_performed_error_never_uses_declared_retry() -> None:
    tool = SequencedReceiptEffectTool(
        [
            ToolResult(
                content="provider performed the effect but returned invalid output",
                is_error=True,
                effect_receipt=EffectReceipt(disposition="performed"),
            )
        ]
    )
    runner = _build(tool, max_attempts=3)

    run = await runner.start({})

    assert run.status == "failed"
    assert tool.calls == 1
    assert len(run.steps["effect"].attempts) == 1
    assert run.effect_journal[0].status == "succeeded"


async def test_resume_does_not_repeat_completed_effect() -> None:
    tool = SyntheticEffectTool()
    runner = _build(tool)
    run = WorkflowRun(
        profile_scope=ProfileScope.create("personal"),
        workflow_name="effect-graph",
        source=runner.source,
        provider="openrouter",
        model="synthetic",
        graph_fingerprint=runner.graph.fingerprint,
        status="interrupted",
        steps={
            "effect": StepRecord(
                step_id="effect",
                execution_address="effect",
                kind="tool",
                status="completed",
                output={"accepted": True},
            )
        },
        effect_journal=[
            EffectJournalEntry(
                step_id="effect",
                execution_address="effect",
                tool_name="synthetic_effect",
                risk="mutating",
                status="succeeded",
            )
        ],
    )

    resumed = await runner.resume(run)

    assert resumed.status == "completed"
    assert resumed.steps["effect"].status == "completed"
    assert resumed.effect_journal[0].status == "succeeded"
    assert tool.calls == 0


async def test_resume_refuses_changed_graph_with_both_source_identities() -> None:
    tool = SyntheticEffectTool()
    runner = _build(tool)
    run = WorkflowRun(
        profile_scope=ProfileScope.create("personal"),
        workflow_name="effect-graph",
        source=WorkflowSourceIdentity(
            resource=ProfileResourceRef(profile="personal", name="fixture"),
            path="/old/workflow.toml",
            scope="fixture",
            content_digest="old-digest",
        ),
        provider="openrouter",
        model="synthetic",
        graph_fingerprint="old-fingerprint",
        steps={},
    )

    with pytest.raises(ValueError) as failure:
        await runner.resume(run)

    message = str(failure.value)
    assert "stored=old-fingerprint" in message
    assert "/old/workflow.toml@old-digest" in message
    assert f"{runner.source.path}@{runner.source.content_digest}" in message


async def test_effects_are_globally_exclusive_across_parallel_foreach() -> None:
    tool = SyntheticEffectTool()
    settings = RickySettings(user_data_dir=".test-ricky")
    registry = ToolRegistry([tool])
    spec = WorkflowSpec.model_validate(
        {
            "version": 2,
            "name": "parallel-effects",
            "description": "Prove global effect exclusivity.",
            "steps": [
                {
                    "id": "left",
                    "kind": "foreach",
                    "collection": [{"id": "left"}],
                    "item_key": {"ref": "item.source.id"},
                    "body": [
                        {
                            "id": "left-effect",
                            "kind": "tool",
                            "tool": "synthetic_effect",
                            "args": {"value": {"ref": "item.source.id"}},
                        }
                    ],
                },
                {
                    "id": "right",
                    "kind": "foreach",
                    "collection": [{"id": "right"}],
                    "item_key": {"ref": "item.source.id"},
                    "body": [
                        {
                            "id": "right-effect",
                            "kind": "tool",
                            "tool": "synthetic_effect",
                            "args": {"value": {"ref": "item.source.id"}},
                        }
                    ],
                },
            ],
        }
    )
    compiled = compile_workflow(spec, tool_registry=registry, settings=settings.workflow)
    assert compiled.graph is not None, compiled.errors

    async def allow(_event):
        return PermissionResponse(decision="allow")

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
            path=str(Path(".ricky/workflows/effects/workflow.toml").resolve()),
            scope="fixture",
            content_digest="synthetic",
        ),
        permission_responder=allow,
    )

    run = await runner.start({})

    assert run.status == "completed"
    assert tool.calls == 2
    assert tool.maximum_active == 1


async def test_approval_steps_are_globally_exclusive() -> None:
    settings = RickySettings(user_data_dir=".test-ricky")
    registry = ToolRegistry([])
    spec = WorkflowSpec.model_validate(
        {
            "version": 2,
            "name": "parallel-approvals",
            "description": "Prove global approval exclusivity.",
            "steps": [
                {
                    "id": "first",
                    "kind": "approval",
                    "mode": "confirm",
                    "prompt": "Approve first?",
                    "proposal": "first",
                },
                {
                    "id": "second",
                    "kind": "approval",
                    "mode": "confirm",
                    "prompt": "Approve second?",
                    "proposal": "second",
                },
            ],
        }
    )
    compiled = compile_workflow(spec, tool_registry=registry, settings=settings.workflow)
    assert compiled.graph is not None, compiled.errors
    active = 0
    maximum_active = 0
    prompts: list[str] = []

    async def approve(request: ApprovalRequest) -> ApprovalResponse:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        prompts.append(request.prompt)
        try:
            await asyncio.sleep(0.01)
            return ApprovalResponse(approved=True)
        finally:
            active -= 1

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
            path=str(Path(".ricky/workflows/approvals/workflow.toml").resolve()),
            scope="fixture",
            content_digest="synthetic",
        ),
        approval_responder=approve,
    )

    run = await runner.start({})

    assert run.status == "completed"
    assert maximum_active == 1
    assert prompts == ["Approve first?", "Approve second?"]


async def test_select_approval_fixture_uses_stable_keys_without_prompt() -> None:
    settings = RickySettings(user_data_dir=".test-ricky")
    registry = ToolRegistry([])
    spec = WorkflowSpec.model_validate(
        {
            "version": 2,
            "name": "fixture-approval",
            "description": "Select one fixture item by stable key.",
            "steps": [
                {
                    "id": "select",
                    "kind": "approval",
                    "mode": "select",
                    "prompt": "Select an item.",
                    "collection": [{"id": "a"}, {"id": "b"}],
                    "item_key": {"ref": "item.source.id"},
                }
            ],
        }
    )
    compiled = compile_workflow(spec, tool_registry=registry, settings=settings.workflow)
    assert compiled.graph is not None, compiled.errors

    async def must_not_prompt(_request: ApprovalRequest) -> ApprovalResponse:
        raise AssertionError("fixture approval called the responder")

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
            path=str(Path(".ricky/workflows/fixture-approval/workflow.toml").resolve()),
            scope="fixture",
            content_digest="synthetic",
        ),
        approval_responder=must_not_prompt,
        fixtures={"select": {"selected_keys": ["b"]}},
        dry_run=True,
    )

    run = await runner.start({})

    assert run.status == "completed"
    assert run.steps["select"].output == {
        "approved": [{"id": "b"}],
        "rejected": [{"id": "a"}],
        "selected_keys": ["b"],
    }
