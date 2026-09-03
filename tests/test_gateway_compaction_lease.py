"""Manual gateway compaction lease renewal and cancellation drills."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import ricky.gateway.conversations as conversations_module
from ricky.agent import AgentSession
from ricky.agent.events import ContextCompactionFinishedEvent
from ricky.config import RickySettings, SessionSettings
from ricky.gateway.conversations import ConversationCoordinator
from ricky.gateway.types import Conversation, ConversationKey
from ricky.messaging.types import InboundMessage
from ricky.profiles import ProfileScope
from ricky.sessions import SessionStore
from ricky.sessions.store import SessionLeaseError
from ricky.sessions.types import SessionLease, StoredSession, StoredTurn

NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)
SCOPE = ProfileScope.create("personal")


class CompactionRuntime:
    """A compact-context runtime with observable ownership boundaries."""

    def __init__(self, *, delay_seconds: float = 0, block: bool = False) -> None:
        self.delay_seconds = delay_seconds
        self.block = block
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.closed = False
        self.released_session_ids: list[str] = []
        self.agent_loop = self
        self.durable_tasks = self

    async def compact_context(
        self,
        session: AgentSession,
    ) -> AsyncIterator[ContextCompactionFinishedEvent]:
        del session
        self.entered.set()
        try:
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            if self.block:
                await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        yield ContextCompactionFinishedEvent(
            operation_id="compaction_lease_drill",
            checkpoint_id="checkpoint_" + "d" * 32,
            source_digest="a" * 64,
            covered_message_count=1,
            newly_covered_message_count=1,
            retained_message_count=0,
            summary_chars=10,
            estimated_tokens_before=100,
            estimated_tokens_after=10,
        )

    async def release_session_leases(self, session_id: str) -> list[str]:
        self.released_session_ids.append(session_id)
        return []


class CountingRenewStore(SessionStore):
    def __init__(self, settings: RickySettings) -> None:
        super().__init__(settings)
        self.renewals = 0

    async def renew(self, lease: SessionLease) -> SessionLease:
        renewed = await super().renew(lease)
        self.renewals += 1
        return renewed


class FailingRenewStore(SessionStore):
    def __init__(self, settings: RickySettings, failure: BaseException) -> None:
        super().__init__(settings)
        self.failure = failure

    async def renew(self, lease: SessionLease) -> SessionLease:
        del lease
        raise self.failure


class CommitBoundaryStore(CountingRenewStore):
    def __init__(self, settings: RickySettings) -> None:
        super().__init__(settings)
        self.commit_entered = asyncio.Event()
        self.allow_commit = asyncio.Event()
        self.renewed_during_commit = asyncio.Event()
        self.commit_calls = 0

    async def renew(self, lease: SessionLease) -> SessionLease:
        renewed = await super().renew(lease)
        if self.commit_entered.is_set():
            self.renewed_during_commit.set()
        return renewed

    async def commit(
        self,
        lease: SessionLease,
        expected_revision: int,
        session: AgentSession,
        turn: StoredTurn,
    ) -> StoredSession:
        self.commit_calls += 1
        self.commit_entered.set()
        await self.allow_commit.wait()
        return await super().commit(lease, expected_revision, session, turn)


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings(
        user_data_dir=str(tmp_path / "user"),
        sessions=SessionSettings(lease_seconds=1, turn_wall_seconds=5),
    )


async def _setup(
    settings: RickySettings,
    store: SessionStore,
) -> tuple[ConversationCoordinator, InboundMessage, Conversation, AgentSession]:
    await store.initialize()
    session = AgentSession.create(settings, profile_scope=SCOPE)
    await store.create(session, scope=SCOPE)
    coordinator = object.__new__(ConversationCoordinator)
    coordinator.settings = settings
    coordinator.sessions = store
    coordinator.dispatcher = cast(Any, SimpleNamespace())
    coordinator.provider_factory = None
    coordinator.event_sink = None
    inbound = InboundMessage(
        id="inbound_" + "a" * 32,
        transport="telegram",
        account="personal",
        update_id="1",
        destination_id="200",
        sender_id="100",
        platform_message_id="300",
        text="/compact",
        received_at=NOW,
        status="pending",
    )
    conversation = Conversation(
        id="conversation_" + "b" * 32,
        key=ConversationKey(
            transport="telegram",
            account="personal",
            destination_id="200",
        ),
        session_id=session.id,
        route_name="owner",
        provider=session.provider,
        model=session.model,
        profile_scope=session.profile_scope,
        project_root=None,
        status="active",
        revision=0,
        created_at=NOW,
        updated_at=NOW,
    )
    return coordinator, inbound, conversation, session


def _install_runtime(
    monkeypatch: pytest.MonkeyPatch,
    runtime: CompactionRuntime,
) -> None:
    @asynccontextmanager
    async def build_runtime(*args: Any, **kwargs: Any) -> AsyncIterator[CompactionRuntime]:
        del args, kwargs
        try:
            yield runtime
        finally:
            runtime.closed = True

    monkeypatch.setattr(conversations_module, "build_gateway_runtime", build_runtime)


async def test_compaction_longer_than_lease_renews_until_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store = CountingRenewStore(settings)
    coordinator, inbound, conversation, session = await _setup(settings, store)
    runtime = CompactionRuntime(delay_seconds=1.2)
    _install_runtime(monkeypatch, runtime)

    response, revision = await coordinator._compact(inbound, conversation)

    assert "Context compacted" in response
    assert revision == 1
    assert store.renewals >= 3
    assert runtime.closed
    assert runtime.released_session_ids == [session.id]
    assert (await store.turns(session.id, scope=SCOPE))[0].status == "committed"


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("synthetic compaction renewal failure"),
        SessionLeaseError("synthetic compaction fence renewal failure"),
    ],
    ids=["generic", "fence"],
)
async def test_compaction_renewal_failure_cancels_and_joins_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    settings = _settings(tmp_path)
    store = FailingRenewStore(settings, failure)
    coordinator, inbound, conversation, session = await _setup(settings, store)
    runtime = CompactionRuntime(block=True)
    _install_runtime(monkeypatch, runtime)

    with pytest.raises(type(failure), match="renewal failure"):
        await asyncio.wait_for(coordinator._compact(inbound, conversation), timeout=2)

    assert runtime.cancelled
    assert runtime.closed
    assert runtime.released_session_ids == []
    stored = await store.get(session.id, scope=SCOPE)
    assert stored.revision == 0
    assert (await store.turns(session.id, scope=SCOPE))[0].status == "failed"


async def test_compaction_cancellation_during_provider_stream_joins_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store = CountingRenewStore(settings)
    coordinator, inbound, conversation, session = await _setup(settings, store)
    runtime = CompactionRuntime(block=True)
    _install_runtime(monkeypatch, runtime)
    compaction = asyncio.create_task(coordinator._compact(inbound, conversation))
    await asyncio.wait_for(runtime.entered.wait(), timeout=2)

    compaction.cancel()
    with pytest.raises(asyncio.CancelledError):
        await compaction

    assert runtime.cancelled
    assert runtime.closed
    assert runtime.released_session_ids == []
    assert (await store.get(session.id, scope=SCOPE)).revision == 0
    assert (await store.turns(session.id, scope=SCOPE))[0].status == "failed"


async def test_compaction_commit_is_covered_at_a_renewal_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store = CommitBoundaryStore(settings)
    coordinator, inbound, conversation, session = await _setup(settings, store)
    runtime = CompactionRuntime()
    _install_runtime(monkeypatch, runtime)
    compaction = asyncio.create_task(coordinator._compact(inbound, conversation))
    await asyncio.wait_for(store.commit_entered.wait(), timeout=2)
    await asyncio.wait_for(store.renewed_during_commit.wait(), timeout=2)

    store.allow_commit.set()
    _, revision = await compaction

    assert revision == 1
    assert store.commit_calls == 1
    assert store.renewals >= 1
    assert runtime.released_session_ids == [session.id]
    assert (await store.turns(session.id, scope=SCOPE))[0].status == "committed"
