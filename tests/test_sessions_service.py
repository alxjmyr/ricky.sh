"""Bounded-runtime, sanitization, uncertainty, and cancellation tests."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ricky.agent import AgentSession
from ricky.agent.events import (
    LlmRequestStartedEvent,
    LlmResponseFinishedEvent,
    TextDeltaEvent,
    ToolCallStartedEvent,
    TurnFinishedEvent,
)
from ricky.agent.session import PermissionGrant
from ricky.config import RickySettings, SessionSettings
from ricky.durable_tasks.types import TaskLease
from ricky.llm import Message
from ricky.profiles import ProfileScope
from ricky.sessions import PersistentTurnError, PersistentTurnService, SessionStore
from ricky.sessions.store import SessionLeaseError

SCOPE = ProfileScope.create("shared")


class FakeDurableTasks:
    def __init__(self) -> None:
        self.released: list[str] = []

    async def release_session_leases(self, session_id: str) -> list[str]:
        self.released.append(session_id)
        return []


class FakeLoop:
    def __init__(self, factory: RuntimeFactory) -> None:
        self.factory = factory

    async def run_turn(self, session: AgentSession, user_input: str) -> AsyncIterator[Any]:
        self.factory.history_before.append(list(session.history))
        self.factory.grants_seen.append(list(session.permission_grants))
        self.factory.task_leases_seen.append(dict(session.active_task_leases))
        if self.factory.phase == "streaming":
            yield LlmRequestStartedEvent(
                turn_id="agent_turn",
                iteration=1,
                model=session.model,
                message_count=1,
                tool_count=0,
            )
            self.factory.reached.set()
            await self.factory.hold.wait()
            return
        if self.factory.phase == "observable_stream":
            yield TextDeltaEvent(turn_id="agent_turn", delta="partial")
            self.factory.reached.set()
            await self.factory.hold.wait()
            return
        if self.factory.phase == "tool":
            yield ToolCallStartedEvent(
                turn_id="agent_turn",
                call_id="call_one",
                tool_name="read_file",
            )
            self.factory.reached.set()
            await self.factory.hold.wait()
            return
        session.history.extend(
            [Message.text("user", user_input), Message.text("assistant", f"reply:{user_input}")]
        )
        yield LlmResponseFinishedEvent(
            turn_id="agent_turn",
            iteration=1,
            stop_reason="stop",
            text_chars=len(user_input) + 6,
            thinking_chars=0,
            tool_call_count=0,
            empty=False,
        )
        yield TurnFinishedEvent(turn_id="agent_turn", iterations=1)


class RuntimeFactory:
    def __init__(self, phase: str = "success") -> None:
        self.phase = phase
        self.reached = asyncio.Event()
        self.hold = asyncio.Event()
        self.closed = False
        self.tasks = FakeDurableTasks()
        self.history_before: list[list[Message]] = []
        self.grants_seen: list[list[PermissionGrant]] = []
        self.task_leases_seen: list[dict[str, TaskLease]] = []

    def __call__(self, settings: RickySettings, **kwargs: Any) -> Any:
        del settings
        return self.context(kwargs["session"])

    @asynccontextmanager
    async def context(self, session: AgentSession) -> AsyncIterator[Any]:
        if self.phase == "construction":
            try:
                self.reached.set()
                await self.hold.wait()
            finally:
                self.closed = True
        try:
            yield SimpleNamespace(
                durable_tasks=self.tasks,
                agent_loop=FakeLoop(self),
            )
        finally:
            if self.phase == "close":
                self.reached.set()
                try:
                    await self.hold.wait()
                finally:
                    self.closed = True
            else:
                self.closed = True
        _ = session


async def _setup(tmp_path: Path) -> tuple[RickySettings, SessionStore, AgentSession]:
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    store = SessionStore(settings)
    await store.initialize()
    session = AgentSession.create(settings, profile_scope=SCOPE)
    await store.create(session, scope=SCOPE)
    return settings, store, session


async def test_process_restart_continues_canonical_history_and_sanitizes_authority(
    tmp_path: Path,
) -> None:
    settings, store, session = await _setup(tmp_path)
    lease = TaskLease(
        id="task-lease",
        holder_session_id=session.id,
        epoch=1,
        acquired_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    payload = (await store.get(session.id, scope=SCOPE)).session.model_dump(mode="json")
    payload["permission_grants"] = [
        PermissionGrant(tool_name="run_shell", label="all shell").model_dump(mode="json")
    ]
    payload["active_task_leases"] = {"task_one": lease.model_dump(mode="json")}
    connection = sqlite3.connect(store.db_path)
    connection.execute(
        "UPDATE sessions SET session_json = ? WHERE id = ?",
        (json.dumps(payload, sort_keys=True, separators=(",", ":")), session.id),
    )
    connection.commit()
    connection.close()

    first_runtime = RuntimeFactory()
    first = PersistentTurnService(
        settings, store, profile_scope=SCOPE, runtime_builder=first_runtime
    )
    one = await first.run_turn(session.id, "one", owner="process-one")

    second_runtime = RuntimeFactory()
    second = PersistentTurnService(
        settings, store, profile_scope=SCOPE, runtime_builder=second_runtime
    )
    two = await second.run_turn(session.id, "two", owner="process-two")

    assert one.revision == 1
    assert two.revision == 2
    assert second_runtime.history_before == [
        [Message.text("user", "one"), Message.text("assistant", "reply:one")]
    ]
    assert first_runtime.grants_seen == [[]]
    assert first_runtime.task_leases_seen == [{}]
    assert first_runtime.tasks.released == [session.id]
    assert two.session.permission_grants == []
    assert two.session.active_task_leases == {}
    assert first_runtime.closed and second_runtime.closed


@pytest.mark.parametrize(
    ("phase", "expected_session_status", "expected_turn_status"),
    [
        ("construction", "active", "failed"),
        ("streaming", "active", "failed"),
        ("observable_stream", "uncertain", "uncertain"),
        ("tool", "uncertain", "uncertain"),
        ("close", "uncertain", "uncertain"),
    ],
)
async def test_cancellation_at_owned_phases_leaves_valid_turn_state(
    tmp_path: Path,
    phase: str,
    expected_session_status: str,
    expected_turn_status: str,
) -> None:
    settings, store, session = await _setup(tmp_path)
    factory = RuntimeFactory(phase)
    service = PersistentTurnService(settings, store, profile_scope=SCOPE, runtime_builder=factory)
    task = asyncio.create_task(service.run_turn(session.id, "hello", owner="worker"))
    await asyncio.wait_for(factory.reached.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    stored = await store.get(session.id, scope=SCOPE)
    turns = await store.turns(session.id, scope=SCOPE)
    assert stored.status == expected_session_status
    assert turns[0].status == expected_turn_status
    assert factory.closed


async def test_observable_sink_failure_marks_turn_uncertain(tmp_path: Path) -> None:
    settings, store, session = await _setup(tmp_path)
    factory = RuntimeFactory()
    service = PersistentTurnService(settings, store, profile_scope=SCOPE, runtime_builder=factory)

    def sink(event: Any) -> None:
        if isinstance(event, LlmResponseFinishedEvent):
            raise RuntimeError("renderer disappeared")

    with pytest.raises(RuntimeError, match="renderer disappeared"):
        await service.run_turn(session.id, "hello", owner="worker", event_sink=sink)

    assert (await store.get(session.id, scope=SCOPE)).status == "uncertain"
    assert (await store.turns(session.id, scope=SCOPE))[0].status == "uncertain"


class CommitGateStore(SessionStore):
    """Expose cancellation after SQLite commits but before the caller resumes."""

    def __init__(self, settings: RickySettings) -> None:
        super().__init__(settings)
        self.commit_finished = asyncio.Event()
        self.hold_result = asyncio.Event()

    async def commit(self, *args: Any, **kwargs: Any) -> Any:
        result = await super().commit(*args, **kwargs)
        self.commit_finished.set()
        await self.hold_result.wait()
        return result


class RenewalFailureStore(SessionStore):
    async def renew(self, lease: Any) -> Any:
        del lease
        raise RuntimeError("synthetic session renewal failure")


class FenceRenewalFailureStore(SessionStore):
    async def renew(self, lease: Any) -> Any:
        del lease
        raise SessionLeaseError("synthetic stale fence during renewal")


@pytest.mark.parametrize(
    ("phase", "expected_session_status", "expected_turn_status"),
    [
        ("streaming", "active", "failed"),
        ("tool", "uncertain", "uncertain"),
        ("close", "uncertain", "uncertain"),
    ],
)
async def test_renewal_failure_promptly_cancels_and_joins_owned_turn(
    tmp_path: Path,
    phase: str,
    expected_session_status: str,
    expected_turn_status: str,
) -> None:
    settings = RickySettings(
        user_data_dir=str(tmp_path / "user"),
        sessions=SessionSettings(lease_seconds=1, turn_wall_seconds=10),
    )
    store = RenewalFailureStore(settings)
    await store.initialize()
    session = AgentSession.create(settings, profile_scope=SCOPE)
    await store.create(session, scope=SCOPE)
    factory = RuntimeFactory(phase)
    service = PersistentTurnService(settings, store, profile_scope=SCOPE, runtime_builder=factory)

    with pytest.raises(RuntimeError, match="synthetic session renewal failure"):
        await asyncio.wait_for(
            service.run_turn(session.id, "hello", owner="worker"),
            timeout=2,
        )

    stored = await store.get(session.id, scope=SCOPE)
    turns = await store.turns(session.id, scope=SCOPE)
    assert stored.status == expected_session_status
    assert turns[0].status == expected_turn_status
    assert factory.closed


async def test_fence_renewal_failure_promptly_cancels_and_joins_provider_stream(
    tmp_path: Path,
) -> None:
    settings = RickySettings(
        user_data_dir=str(tmp_path / "user"),
        sessions=SessionSettings(lease_seconds=1, turn_wall_seconds=10),
    )
    store = FenceRenewalFailureStore(settings)
    await store.initialize()
    session = AgentSession.create(settings, profile_scope=SCOPE)
    await store.create(session, scope=SCOPE)
    factory = RuntimeFactory("streaming")
    service = PersistentTurnService(settings, store, profile_scope=SCOPE, runtime_builder=factory)

    with pytest.raises(PersistentTurnError, match="stale fence"):
        await asyncio.wait_for(
            service.run_turn(session.id, "hello", owner="worker"),
            timeout=2,
        )

    assert factory.closed
    assert (await store.get(session.id, scope=SCOPE)).status == "active"
    assert (await store.turns(session.id, scope=SCOPE))[0].status == "failed"


async def test_renewal_failure_cancels_and_joins_blocked_event_sink(
    tmp_path: Path,
) -> None:
    settings = RickySettings(
        user_data_dir=str(tmp_path / "user"),
        sessions=SessionSettings(lease_seconds=1, turn_wall_seconds=10),
    )
    store = RenewalFailureStore(settings)
    await store.initialize()
    session = AgentSession.create(settings, profile_scope=SCOPE)
    await store.create(session, scope=SCOPE)
    factory = RuntimeFactory()
    sink_entered = asyncio.Event()
    sink_exited = asyncio.Event()

    async def sink(event: Any) -> None:
        if not isinstance(event, LlmResponseFinishedEvent):
            return
        sink_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            sink_exited.set()

    service = PersistentTurnService(settings, store, profile_scope=SCOPE, runtime_builder=factory)
    turn = asyncio.create_task(
        service.run_turn(session.id, "hello", owner="worker", event_sink=sink)
    )
    await asyncio.wait_for(sink_entered.wait(), timeout=2)

    with pytest.raises(RuntimeError, match="renewal failure"):
        await asyncio.wait_for(turn, timeout=2)

    assert sink_exited.is_set()
    assert factory.closed
    assert (await store.get(session.id, scope=SCOPE)).status == "uncertain"
    assert (await store.turns(session.id, scope=SCOPE))[0].status == "uncertain"


async def test_cancellation_at_store_commit_resolves_the_committed_revision(
    tmp_path: Path,
) -> None:
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    store = CommitGateStore(settings)
    await store.initialize()
    session = AgentSession.create(settings, profile_scope=SCOPE)
    await store.create(session, scope=SCOPE)
    factory = RuntimeFactory()
    service = PersistentTurnService(settings, store, profile_scope=SCOPE, runtime_builder=factory)
    task = asyncio.create_task(service.run_turn(session.id, "hello", owner="worker"))
    await asyncio.wait_for(store.commit_finished.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    stored = await store.get(session.id, scope=SCOPE)
    turns = await store.turns(session.id, scope=SCOPE)
    assert stored.status == "active"
    assert stored.revision == 1
    assert turns[0].status == "committed"
