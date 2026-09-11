"""Regression for task bookkeeping after a successful background effect."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict

from ricky.agent import AgentLoop, AgentSession
from ricky.agent.events import ToolCallFinishedEvent, TurnFinishedEvent
from ricky.config import RickySettings
from ricky.durable_tasks.artifacts import TaskArtifactStore
from ricky.durable_tasks.state_guard import DurableTaskStateGuard
from ricky.durable_tasks.store import DurableTaskStore
from ricky.durable_tasks.tools import durable_task_policy, durable_task_tools
from ricky.executions.dispatcher import ExecutionDispatcher
from ricky.executions.types import ExecutionRequest
from ricky.llm import CompletionRequest, Message, MessageDone, StreamEvent, ToolCallPart
from ricky.permissions import PermissionEngine, Policy, PolicyRule
from ricky.profiles import ProfileScope
from ricky.tools import EffectReceipt, Risk, ToolContext, ToolRegistry, ToolResult


class _NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _TrashTool:
    name: ClassVar[str] = "test_trash"
    description: ClassVar[str] = "Move the identified test message to Trash."
    Params: ClassVar[type[BaseModel]] = _NoArgs
    risk: ClassVar[Risk] = "mutating"
    effect_kind = "external"

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        self.calls += 1
        return ToolResult(
            content="Moved message test-message to Trash.",
            effect_receipt=EffectReceipt(
                disposition="performed", provider_reference="test-message"
            ),
        )


class _ScriptedProvider:
    name = "openrouter"

    def __init__(self, calls: list[tuple[str, dict[str, object]]]) -> None:
        self.calls = calls
        self.requests: list[CompletionRequest] = []

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        index = len(self.requests)
        self.requests.append(request)
        if index < len(self.calls):
            name, args = self.calls[index]
            message = Message(
                role="assistant",
                content=[ToolCallPart(id=f"call_{index}", name=name, args=args)],
            )
        else:
            message = Message.text("assistant", "The message was trashed and the task completed.")
        yield MessageDone(message=message)

    async def aclose(self) -> None:
        pass


@pytest.mark.parametrize("stale_claims", [0, 1, 3])
async def test_dispatch_snapshot_allows_completion_or_bounds_stale_claims(
    tmp_path: Path, stale_claims: int
) -> None:
    settings = RickySettings(
        user_data_dir=str(tmp_path / "user"), project_data_dir=str(tmp_path / "project")
    )
    scope = ProfileScope.create("personal")
    tasks = await DurableTaskStore.create(settings, profile="personal")
    task = await tasks.create_task(
        title="Trash a promotion",
        objective="Move the identified promotion to Trash",
        closure_criteria="The message is trashed and the outcome recorded",
        execution_mode="agent",
        authority="deterministic_user_command",
        executor_id="test",
    )
    request = ExecutionRequest(
        id=f"execution_{uuid4().hex}",
        kind="ad_hoc",
        status="queued",
        goal=task.objective,
        contract_id=f"contract_{uuid4().hex}",
        contract_digest="a" * 64,
        task_id=task.id,
        task_revision=task.revision,
        profile_scope=scope,
        notification_route="owner",
        request_key="test",
        created_at=datetime.now(UTC),
    )
    dispatcher = ExecutionDispatcher(settings, project_root=tmp_path)
    _, sections = await dispatcher._dispatch_context(request)
    snapshot = sections["linked_durable_task"]
    assert "historical, not live state" in snapshot
    assert "supersede this snapshot" in snapshot
    assert '"lease"' not in snapshot
    assert '"revision": 1' in snapshot

    session = AgentSession.create(settings, profile_scope=scope, model="test-model")
    guard = DurableTaskStateGuard(tasks)
    task_tools = [
        guard.wrap(tool) if getattr(tool, "state_guard_id", None) else tool
        for tool in durable_task_tools(tasks, TaskArtifactStore(tasks))
    ]
    trash = _TrashTool()
    claim: tuple[str, dict[str, object]] = (
        "claim_durable_task",
        {"task_id": task.id, "expected_revision": 1},
    )
    provider = _ScriptedProvider(
        [claim, (trash.name, {}), *([claim] * stale_claims)]
        + [
            (
                "complete_durable_task",
                {"task_id": task.id, "completion_summary": "Message test-message moved to Trash"},
            )
        ]
    )
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([*task_tools, trash]),
        settings=settings,
        cwd=tmp_path,
        permission_engine=PermissionEngine(
            durable_task_policy(Policy(rules=[PolicyRule(tool_name=trash.name, decision="allow")]))
        ),
    )
    try:
        events = [
            event
            async for event in loop.run_turn(
                session, task.objective, extra_system_sections=sections, max_iterations=100
            )
        ]
        final = events[-1]
        assert isinstance(final, TurnFinishedEvent)
        assert trash.calls == 1
        current = await tasks.get_task(task.id)
        assert request.task_revision == 1
        assert sections["linked_durable_task"] == snapshot
        if stale_claims < 3:
            assert final.error is None
            assert current.status == "completed"
            assert current.completion_summary == "Message test-message moved to Trash"
            assert task.id not in session.active_task_leases
        else:
            assert final.error is not None
            assert "repeatedly failed against unchanged state" in final.error
            assert final.iterations == 5
            assert current.status == "in_progress"
        results = [event for event in events if isinstance(event, ToolCallFinishedEvent)]
        assert sum(result.is_error for result in results) == stale_claims
        assert not (tmp_path / "project").exists()
    finally:
        await tasks.release_session_leases(session.id)
        await provider.aclose()
