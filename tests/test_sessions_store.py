"""Fencing, CAS, schema, and lifecycle tests for persistent sessions."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ricky.agent import AgentSession
from ricky.agent.session import PermissionGrant
from ricky.config import RickySettings
from ricky.llm import Message
from ricky.profiles import ProfileScope
from ricky.sessions import (
    SessionConflictError,
    SessionLeaseError,
    SessionNotFoundError,
    SessionSchemaError,
    SessionStateError,
    SessionStore,
    StoredTurn,
)


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 11, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


SCOPE = ProfileScope.create("shared")


async def _store(
    tmp_path: Path, clock: MutableClock | None = None
) -> tuple[SessionStore, RickySettings]:
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    store = SessionStore(settings, clock=clock)
    await store.initialize()
    return store, settings


def _running(session_id: str, revision: int, now: datetime, suffix: str) -> StoredTurn:
    return StoredTurn(
        id=f"turn_{suffix}",
        session_id=session_id,
        inbound_ref=None,
        base_revision=revision,
        status="running",
        profile_label=SCOPE.label(),
        started_at=now,
    )


async def test_create_round_trip_preserves_context_but_not_runtime_authority(
    tmp_path: Path,
) -> None:
    store, settings = await _store(tmp_path)
    session = AgentSession.create(settings, profile_scope=SCOPE)
    session.history = [Message.text("user", "remember this")]
    session.permission_grants.append(PermissionGrant(tool_name="run_shell", label="all shell"))

    created = await store.create(session, scope=SCOPE)
    loaded = await store.get(session.id, scope=SCOPE)

    assert loaded == created
    assert loaded.session.history == session.history
    assert loaded.session.provider == session.provider
    assert loaded.session.model == session.model
    assert loaded.session.profile_scope == session.profile_scope
    assert loaded.session.settings_snapshot == session.settings_snapshot
    assert loaded.session.permission_grants == []
    assert loaded.session.active_task_leases == {}
    assert store.db_path == tmp_path / "user" / "sessions" / "sessions.sqlite3"
    assert store.artifact_root(session.id, scope=SCOPE) == (
        tmp_path / "user" / "sessions" / session.id / "artifacts"
    )


async def test_session_reads_and_lists_enforce_profile_scope(tmp_path: Path) -> None:
    store, settings = await _store(tmp_path)
    work = ProfileScope.create("work")
    personal = ProfileScope.create("personal")
    cross_profile = ProfileScope.create("personal", access_profiles=["work"])
    session = AgentSession.create(settings, profile_scope=work)
    await store.create(session, scope=work)

    with pytest.raises(SessionNotFoundError):
        await store.get(session.id, scope=personal)
    with pytest.raises(SessionNotFoundError):
        await store.acquire(session.id, "personal-worker", scope=personal)
    assert await store.list(scope=personal) == []
    assert (await store.get(session.id, scope=cross_profile)).session == session


async def test_lease_is_exclusive_and_expired_worker_is_fenced(tmp_path: Path) -> None:
    clock = MutableClock()
    store, settings = await _store(tmp_path, clock)
    session = AgentSession.create(settings, profile_scope=SCOPE)
    await store.create(session, scope=SCOPE)
    first = await store.acquire(session.id, "worker-a", scope=SCOPE, lease_seconds=10)
    with pytest.raises(SessionLeaseError, match="busy"):
        await store.acquire(session.id, "worker-b", scope=SCOPE, lease_seconds=10)

    clock.now += timedelta(seconds=11)
    second = await store.acquire(session.id, "worker-b", scope=SCOPE, lease_seconds=10)
    assert second.fence == first.fence + 1
    turn = _running(session.id, 0, clock.now, "stale")
    with pytest.raises(SessionLeaseError, match="stale|foreign"):
        await store.commit(first, 0, session, turn)
    assert (await store.get(session.id, scope=SCOPE)).revision == 0


async def test_commit_is_revisioned_and_cas_conflict_is_atomic(tmp_path: Path) -> None:
    clock = MutableClock()
    store, settings = await _store(tmp_path, clock)
    session = AgentSession.create(settings, profile_scope=SCOPE)
    await store.create(session, scope=SCOPE)
    lease = await store.acquire(session.id, "worker", scope=SCOPE)
    turn = _running(session.id, 0, clock.now, "one")
    await store.begin_turn(lease, turn)
    session.history.append(Message.text("user", "first"))
    committed = await store.commit(lease, 0, session, turn)
    assert committed.revision == 1
    assert committed.last_turn_id == turn.id
    assert (await store.turns(session.id, scope=SCOPE))[0].status == "committed"

    stale = _running(session.id, 0, clock.now, "two")
    with pytest.raises(SessionConflictError, match="base revision|revision changed"):
        await store.begin_turn(lease, stale)
    current = await store.get(session.id, scope=SCOPE)
    assert current.revision == 1
    assert current.session.history == [Message.text("user", "first")]


async def test_uncertain_failure_blocks_resume_and_archive_is_provider_free(tmp_path: Path) -> None:
    clock = MutableClock()
    store, settings = await _store(tmp_path, clock)
    session = AgentSession.create(settings, profile_scope=SCOPE)
    await store.create(session, scope=SCOPE)
    lease = await store.acquire(session.id, "worker", scope=SCOPE)
    turn = _running(session.id, 0, clock.now, "uncertain")
    await store.begin_turn(lease, turn)
    failed = await store.fail_turn(lease, turn.id, "output may have escaped", uncertain=True)
    assert failed.status == "uncertain"
    assert (await store.get(session.id, scope=SCOPE)).status == "uncertain"
    await store.release(lease)
    with pytest.raises(SessionStateError, match="not resumable"):
        await store.acquire(session.id, "other", scope=SCOPE)

    archived = await store.archive(session.id, expected_revision=0, scope=SCOPE)
    assert archived.status == "archived"
    assert archived.revision == 1
    assert [
        item.session.id for item in await store.list(scope=SCOPE, status="archived", limit=10)
    ] == [session.id]


async def test_different_sessions_advance_concurrently(tmp_path: Path) -> None:
    clock = MutableClock()
    store, settings = await _store(tmp_path, clock)
    sessions = [
        AgentSession.create(settings, profile_scope=SCOPE),
        AgentSession.create(settings, profile_scope=SCOPE),
    ]
    await asyncio.gather(*(store.create(session, scope=SCOPE) for session in sessions))

    async def advance(session: AgentSession, suffix: str) -> int:
        lease = await store.acquire(session.id, f"worker-{suffix}", scope=SCOPE)
        turn = _running(session.id, 0, clock.now, suffix)
        await store.begin_turn(lease, turn)
        session.history.append(Message.text("user", suffix))
        return (await store.commit(lease, 0, session, turn)).revision

    assert await asyncio.gather(advance(sessions[0], "a"), advance(sessions[1], "b")) == [1, 1]


async def test_unknown_json_field_and_store_schema_fail_closed(tmp_path: Path) -> None:
    store, settings = await _store(tmp_path)
    session = AgentSession.create(settings, profile_scope=SCOPE)
    await store.create(session, scope=SCOPE)
    connection = sqlite3.connect(store.db_path)
    payload = json.loads(session.model_dump_json())
    payload["future_field"] = True
    connection.execute(
        "UPDATE sessions SET session_json = ? WHERE id = ?",
        (json.dumps(payload), session.id),
    )
    connection.commit()
    connection.close()
    with pytest.raises(SessionSchemaError, match="unknown fields"):
        await store.get(session.id, scope=SCOPE)

    connection = sqlite3.connect(store.db_path)
    connection.execute("UPDATE store_metadata SET value = '99' WHERE key = 'schema_version'")
    connection.commit()
    connection.close()
    with pytest.raises(SessionSchemaError, match="unsupported"):
        await store.initialize()


@pytest.mark.skipif(os.name != "posix", reason="private store modes are POSIX file modes")
async def test_reopening_a_loosened_store_restores_private_modes(tmp_path: Path) -> None:
    store, settings = await _store(tmp_path)
    session = AgentSession.create(settings, profile_scope=SCOPE)
    await store.create(session, scope=SCOPE)
    # A live WAL connection materializes the sidecars, which inherit whatever
    # mode the database file carried when SQLite created them.
    holder = sqlite3.connect(store.db_path)
    try:
        holder.execute("SELECT COUNT(*) FROM sessions").fetchone()
        sidecars = [Path(f"{store.db_path}-wal"), Path(f"{store.db_path}-shm")]
        assert [path.name for path in sidecars if path.is_file()] == [
            path.name for path in sidecars
        ]
        store.root.chmod(0o755)
        for path in (store.db_path, *sidecars):
            path.chmod(0o644)

        await SessionStore(settings).initialize()

        assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
        for path in (store.db_path, *sidecars):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    finally:
        holder.close()
