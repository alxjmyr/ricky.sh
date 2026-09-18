"""Session schema migration preserves legacy turns without invented handoffs."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ricky.agent.session import AgentSession
from ricky.config import RickySettings
from ricky.profiles import ProfileScope
from ricky.sessions.store import SessionSchemaError, SessionStore
from ricky.sessions.types import StoredTurn
from ricky.sessions.upgrade import SessionsUpgradeAdapter, inspect_sessions_store


@pytest.mark.asyncio
async def test_v1_session_upgrade_preserves_history_and_defaults_handoff_evidence(
    tmp_path: Path,
) -> None:
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    scope = ProfileScope.create("shared")
    store = SessionStore(settings)
    await store.initialize()
    session = AgentSession.create(settings, profile_scope=scope)
    await store.create(session, scope=scope)
    lease = await store.acquire(session.id, "writer", scope=scope)
    turn = StoredTurn(
        id="turn_legacy",
        session_id=session.id,
        profile_label=scope.label(),
        inbound_ref="inbound_legacy",
        base_revision=0,
        status="running",
        started_at=datetime.now(UTC),
    )
    await store.commit(lease, 0, session, turn)
    await store.release(lease)
    with sqlite3.connect(store.db_path) as connection:
        original = connection.execute("SELECT session_json FROM sessions").fetchone()[0]
        connection.execute("ALTER TABLE turns DROP COLUMN background_handoffs")
        connection.execute("ALTER TABLE turns DROP COLUMN handoff_acknowledgement")
        connection.execute("UPDATE store_metadata SET value = '1' WHERE key = 'schema_version'")
    inspection = inspect_sessions_store(store.db_path)
    assert inspection.state == "migration_required"
    assert inspection.found_schema_version == 1
    with pytest.raises(SessionSchemaError):
        await store.initialize()
    adapter = SessionsUpgradeAdapter((store.db_path,))
    [step] = adapter.plan_steps(source_data_generation=1, target_data_generation=1)
    assert step.source_schema_version == 1 and step.target_schema_version == 2
    adapter.apply(step)
    adapter.apply(step)
    assert inspect_sessions_store(store.db_path).state == "current"
    await store.initialize()
    assert (await store.get(session.id, scope=scope)).session == session
    legacy = await store.turn_for_inbound(session.id, "inbound_legacy", scope=scope)
    assert legacy is not None and legacy.status == "committed"
    assert legacy.background_handoffs == []
    assert legacy.handoff_acknowledgement is None
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT session_json FROM sessions").fetchone()[0] == original


@pytest.mark.asyncio
async def test_partial_session_migration_is_rejected(tmp_path: Path) -> None:
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    store = SessionStore(settings)
    await store.initialize()
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("UPDATE store_metadata SET value = '1' WHERE key = 'schema_version'")
    assert inspect_sessions_store(store.db_path).state == "corrupt"
