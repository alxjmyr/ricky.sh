"""Fresh-session durable-task continuation scenarios."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

from ricky.agent import AgentLoop, AgentSession
from ricky.config import RickySettings
from ricky.durable_tasks.artifacts import TaskArtifactStore
from ricky.durable_tasks.store import DurableTaskStore
from ricky.durable_tasks.tools import durable_task_policy, durable_task_tools
from ricky.durable_tasks.types import DurableTask, TaskLease, TaskSearchQuery
from ricky.llm import (
    CompletionRequest,
    Message,
    MessageDone,
    StreamEvent,
    TextPart,
    ToolCallPart,
    Usage,
)
from ricky.permissions import PermissionEngine
from ricky.tools import ToolRegistry


def _lease(task: DurableTask) -> TaskLease:
    assert task.lease is not None
    return task.lease


class ScriptedProvider:
    name = "scripted"

    def __init__(self, scripts: list[list[StreamEvent]]) -> None:
        self.scripts = scripts

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        del request
        for event in self.scripts.pop(0):
            yield event

    async def aclose(self) -> None:
        pass


def _tool(call_id: str, name: str, args: dict[str, object]) -> MessageDone:
    return MessageDone(
        message=Message(role="assistant", content=[ToolCallPart(id=call_id, name=name, args=args)]),
        usage=Usage(),
        stop_reason="tool_calls",
    )


def _final(text: str) -> MessageDone:
    return MessageDone(
        message=Message(role="assistant", content=[TextPart(text=text)]),
        usage=Usage(),
        stop_reason="stop",
    )


async def _turn(loop: AgentLoop, session: AgentSession, prompt: str) -> None:
    _ = [event async for event in loop.run_turn(session, prompt)]


async def test_agent_owned_task_continues_in_a_fresh_session(tmp_path: Path) -> None:
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    session_a = await DurableTaskStore.create(settings, profile="personal")
    artifacts_a = TaskArtifactStore(session_a)
    created = await session_a.create_task(
        title="Schedule review",
        objective="Schedule a review meeting",
        closure_criteria="A calendar event is finalized",
        execution_mode="agent",
        authority="agent_autonomy",
        executor_id="session_a",
    )
    claimed_a = await session_a.claim(
        created.id,
        holder_session_id="session_a",
        authority="agent_autonomy",
        executor_id="session_a",
    )
    plan = await artifacts_a.write(
        created.id,
        "PLAN.md",
        "1. Ask for availability\n2. Finalize the event\n",
        lease=_lease(claimed_a),
        expected_revision=claimed_a.revision,
        expected_sha256=None,
        authority="agent_autonomy",
        executor_id="session_a",
    )
    progressed = await session_a.progress(
        created.id,
        lease=_lease(plan.task),
        expected_revision=plan.task.revision,
        current_summary="Availability requested",
        next_action="Use the reply to schedule",
        authority="agent_autonomy",
        executor_id="session_a",
    )
    await session_a.release(
        created.id,
        lease=_lease(progressed),
        expected_revision=progressed.revision,
        authority="agent_autonomy",
        executor_id="session_a",
    )

    session_b = await DurableTaskStore.create(settings, profile="personal")
    artifacts_b = TaskArtifactStore(session_b)
    found = await session_b.search(TaskSearchQuery(text="Schedule review"))
    assert [task.id for task in found] == [created.id]
    assert "Finalize the event" in (await artifacts_b.read(created.id, "PLAN.md")).content
    claimed_b = await session_b.claim(
        created.id,
        holder_session_id="session_b",
        authority="agent_autonomy",
        executor_id="session_b",
    )
    completed = await session_b.complete(
        created.id,
        lease=_lease(claimed_b),
        expected_revision=claimed_b.revision,
        completion_summary="Calendar event finalized",
        authority="agent_autonomy",
        executor_id="session_b",
    )
    assert completed.status == "completed"


async def test_joint_human_edit_and_user_bookkeeping_are_preserved(tmp_path: Path) -> None:
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    store_a = await DurableTaskStore.create(settings, profile="personal")
    artifacts_a = TaskArtifactStore(store_a)
    joint = await store_a.create_task(
        title="Review response",
        objective="Finalize a response with Alex",
        closure_criteria="Alex's reviewed response is final",
        execution_mode="joint",
        authority="joint_work",
        executor_id="session_a",
    )
    claimed = await store_a.claim(
        joint.id,
        holder_session_id="session_a",
        authority="joint_work",
        executor_id="session_a",
    )
    draft = await artifacts_a.write(
        joint.id,
        "draft.md",
        "Initial draft",
        lease=_lease(claimed),
        expected_revision=claimed.revision,
        expected_sha256=None,
        authority="joint_work",
        executor_id="session_a",
    )
    waiting = await store_a.wait(
        joint.id,
        lease=_lease(draft.task),
        expected_revision=draft.task.revision,
        waiting_on="user",
        current_summary="Draft ready for Alex",
        next_action="Review draft.md",
        authority="joint_work",
        executor_id="session_a",
    )
    await store_a.release(
        joint.id,
        lease=_lease(waiting),
        expected_revision=waiting.revision,
        authority="joint_work",
        executor_id="session_a",
    )
    human_path = store_a.artifact_root / joint.id / "draft.md"
    human_path.write_text("Alex's reviewed draft", encoding="utf-8")

    store_b = await DurableTaskStore.create(settings, profile="personal")
    human_read = await TaskArtifactStore(store_b).read(joint.id, "draft.md")
    assert human_read.content == "Alex's reviewed draft"
    assert human_read.entry.sha256 != draft.entry.sha256

    todo = await store_b.create_task(
        title="Submit form",
        objective="Alex submits the form",
        closure_criteria="Alex confirms submission",
        execution_mode="user",
        authority="direct_user_instruction",
        executor_id="session_b",
    )
    # Reading or suggesting next work does not mutate a user-owned task.
    assert (await store_b.get_task(todo.id)).revision == 1
    user_claim = await store_b.claim(
        todo.id,
        holder_session_id="session_b",
        authority="direct_user_instruction",
        executor_id="session_b",
    )
    user_done = await store_b.complete(
        todo.id,
        lease=_lease(user_claim),
        expected_revision=user_claim.revision,
        completion_summary="Alex directly reported the form submitted",
        authority="direct_user_instruction",
        executor_id="session_b",
    )
    assert user_done.status == "completed"
    assert (await store_b.activities(todo.id))[0].authority == "direct_user_instruction"

    columns = {
        row[1] for row in sqlite3.connect(store_b.db_path).execute("PRAGMA table_info(tasks)")
    }
    assert not {"transcript", "prompt", "steps", "workflow_run"} & columns


async def test_actual_agent_loop_continues_task_across_fresh_sessions(
    tmp_path: Path,
) -> None:
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    store_a = await DurableTaskStore.create(settings, profile="personal")
    registry_a = ToolRegistry(durable_task_tools(store_a, TaskArtifactStore(store_a)))
    session_a = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="synthetic",
    )
    loop_a = AgentLoop(
        provider=ScriptedProvider(
            [
                [
                    _tool(
                        "create",
                        "create_durable_task",
                        {
                            "title": "Agent-loop handoff",
                            "objective": "Finish work in a later session",
                            "closure_criteria": "The later session records completion",
                            "execution_mode": "agent",
                        },
                    )
                ],
                [_final("Task created for continuation")],
            ]
        ),
        registry=registry_a,
        settings=settings,
        permission_engine=PermissionEngine(durable_task_policy()),
        cwd=tmp_path,
    )
    await _turn(loop_a, session_a, "Track this work across sessions")
    [created] = await store_a.search(TaskSearchQuery(text="Agent-loop handoff"))

    store_b = await DurableTaskStore.create(settings, profile="personal")
    registry_b = ToolRegistry(durable_task_tools(store_b, TaskArtifactStore(store_b)))
    session_b = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="synthetic",
    )
    loop_b = AgentLoop(
        provider=ScriptedProvider(
            [
                [_tool("search", "search_durable_tasks", {"text": "Agent-loop handoff"})],
                [_tool("read", "read_durable_task", {"task_id": created.id})],
                [_tool("claim", "claim_durable_task", {"task_id": created.id})],
                [
                    _tool(
                        "complete",
                        "complete_durable_task",
                        {
                            "task_id": created.id,
                            "completion_summary": "Fresh agent session completed the outcome",
                        },
                    )
                ],
                [_final("Continuation complete")],
            ]
        ),
        registry=registry_b,
        settings=settings,
        permission_engine=PermissionEngine(durable_task_policy()),
        cwd=tmp_path,
    )
    await _turn(loop_b, session_b, "Continue the durable task")

    completed = await store_b.get_task(created.id)
    assert completed.status == "completed"
    assert completed.lease is None
    assert session_a.history != session_b.history


async def test_scripted_autonomous_suggestion_does_not_mutate_user_task(
    tmp_path: Path,
) -> None:
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    store = await DurableTaskStore.create(settings, profile="personal")
    task = await store.create_task(
        title="User-only todo",
        objective="Alex completes the todo",
        closure_criteria="Alex directly reports completion",
        execution_mode="user",
        authority="direct_user_instruction",
        executor_id="fixture",
    )
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="synthetic",
    )
    registry = ToolRegistry(durable_task_tools(store, TaskArtifactStore(store)))
    suggestion_loop = AgentLoop(
        provider=ScriptedProvider([[_final("You could complete the todo next.")]]),
        registry=registry,
        settings=settings,
        permission_engine=PermissionEngine(durable_task_policy()),
        cwd=tmp_path,
    )
    await _turn(suggestion_loop, session, "What should I work on?")
    assert (await store.get_task(task.id)).revision == 1

    instruction_loop = AgentLoop(
        provider=ScriptedProvider(
            [
                [_tool("claim", "claim_durable_task", {"task_id": task.id})],
                [
                    _tool(
                        "complete",
                        "complete_durable_task",
                        {
                            "task_id": task.id,
                            "completion_summary": "Alex directly reported completion",
                        },
                    )
                ],
                [_final("Marked complete on Alex's instruction")],
            ]
        ),
        registry=registry,
        settings=settings,
        permission_engine=PermissionEngine(durable_task_policy()),
        cwd=tmp_path,
    )
    await _turn(instruction_loop, session, "I did it; mark the task complete")
    assert (await store.get_task(task.id)).status == "completed"
    assert (await store.activities(task.id))[0].authority == "direct_user_instruction"
