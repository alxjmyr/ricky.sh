"""SQLite lifecycle, schema, search, and activity tests."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ricky.config import RickySettings
from ricky.durable_tasks.store import (
    DurableTaskStore,
    TaskLeaseError,
    TaskSchemaError,
    TaskStoreError,
)
from ricky.durable_tasks.types import TaskSearchQuery


async def _store(tmp_path: Path) -> DurableTaskStore:
    return await DurableTaskStore.create(
        RickySettings(user_data_dir=str(tmp_path / "user")), profile="personal"
    )


async def test_concurrent_first_open_creates_one_current_store(tmp_path: Path) -> None:
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))

    first, second = await asyncio.gather(
        DurableTaskStore.create(settings, profile="personal"),
        DurableTaskStore.create(settings, profile="personal"),
    )

    assert first.db_path == second.db_path
    with sqlite3.connect(first.db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)


async def test_full_lifecycle_is_revisioned_and_audited(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    task = await store.create_task(
        title="Schedule follow-up",
        objective="Schedule a meeting",
        closure_criteria="A calendar event exists",
        execution_mode="agent",
        priority=5,
        due_at=datetime(2026, 8, 1, tzinfo=UTC),
        authority="agent_autonomy",
        executor_id="session_a",
        session_id="session_a",
    )
    claimed = await store.claim(
        task.id,
        holder_session_id="session_a",
        authority="agent_autonomy",
        executor_id="session_a",
    )
    assert claimed.status == "in_progress"
    assert claimed.lease is not None
    progressed = await store.progress(
        task.id,
        lease=claimed.lease,
        expected_revision=claimed.revision,
        current_summary="Requested availability",
        next_action="Wait for a reply",
        authority="agent_autonomy",
        executor_id="session_a",
    )
    assert progressed.lease is not None
    waiting = await store.wait(
        task.id,
        lease=progressed.lease,
        expected_revision=progressed.revision,
        waiting_on="external",
        current_summary="Availability request sent",
        next_action="Process the reply",
        authority="agent_autonomy",
        executor_id="session_a",
    )
    assert waiting.lease is not None
    released = await store.release(
        task.id,
        lease=waiting.lease,
        expected_revision=waiting.revision,
        authority="agent_autonomy",
        executor_id="session_a",
    )
    claimed_b = await store.claim(
        task.id,
        holder_session_id="session_b",
        authority="agent_autonomy",
        executor_id="session_b",
    )
    assert claimed_b.revision == released.revision + 1
    assert claimed_b.lease is not None
    completed = await store.complete(
        task.id,
        lease=claimed_b.lease,
        expected_revision=claimed_b.revision,
        completion_summary="Created the agreed calendar event",
        authority="agent_autonomy",
        executor_id="session_b",
    )

    assert completed.status == "completed"
    assert completed.lease is None
    assert completed.completed_at is not None
    assert await store.search() == []
    assert [item.id for item in await store.search(TaskSearchQuery(include_closed=True))] == [
        task.id
    ]
    activity = await store.activities(task.id)
    assert [item.kind for item in reversed(activity)] == [
        "created",
        "claimed",
        "progressed",
        "waiting",
        "released",
        "claimed",
        "completed",
    ]
    assert all(item.task_revision >= 1 for item in activity)

    reopened = await store.reopen(
        task.id,
        reason="The attendee requested a change",
        authority="deterministic_user_command",
        executor_id="ricky_task_cli",
    )
    assert reopened.status == "open"
    assert reopened.completed_at is None
    assert reopened.completion_summary is None

    claimed_c = await store.claim(
        task.id,
        holder_session_id="session_c",
        authority="agent_autonomy",
        executor_id="session_c",
    )
    assert claimed_c.lease is not None
    cancelled = await store.cancel(
        task.id,
        lease=claimed_c.lease,
        expected_revision=claimed_c.revision,
        reason="The meeting is no longer needed",
        authority="agent_autonomy",
        executor_id="session_c",
    )
    assert cancelled.status == "cancelled"
    assert await store.search() == []
    assert [item.id for item in await store.search(TaskSearchQuery(include_closed=True))] == [
        task.id
    ]
    assert [item.id for item in await store.search(TaskSearchQuery(statuses=["cancelled"]))] == [
        task.id
    ]


async def test_invalid_transition_is_atomic(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    task = await store.create_task(
        title="Personal todo",
        objective="Submit a form",
        closure_criteria="Submission receipt exists",
        execution_mode="user",
        authority="direct_user_instruction",
        executor_id="session_a",
    )

    with pytest.raises(TaskLeaseError):
        await store.complete(
            task.id,
            lease=None,  # type: ignore[arg-type]
            expected_revision=task.revision,
            completion_summary="Done",
            authority="direct_user_instruction",
            executor_id="session_a",
        )

    unchanged = await store.get_task(task.id)
    assert unchanged.revision == 1
    assert [item.kind for item in await store.activities(task.id)] == ["created"]


async def test_search_is_bounded_filtered_and_deterministic(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    for title, priority in (("Alpha planning", 1), ("Alpha urgent", 9), ("Beta", 50)):
        await store.create_task(
            title=title,
            objective=f"Finish {title}",
            closure_criteria="Finished",
            execution_mode="joint",
            priority=priority,
            authority="joint_work",
            executor_id="session_a",
        )

    tasks = await store.search(TaskSearchQuery(text="alpha", limit=2))
    assert [task.title for task in tasks] == ["Alpha urgent", "Alpha planning"]


async def test_personal_and_work_use_physical_roots(tmp_path: Path) -> None:
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    personal = await DurableTaskStore.create(settings, profile="personal")
    work = await DurableTaskStore.create(settings, profile="work")
    assert personal.db_path != work.db_path
    assert personal.artifact_root != work.artifact_root
    assert personal.db_path.parent.parent.name == "personal"
    assert work.db_path.parent.parent.name == "work"


async def test_unknown_schema_version_fails_closed(tmp_path: Path) -> None:
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    root = tmp_path / "user" / "profiles" / "personal" / "tasks"
    root.mkdir(parents=True)
    connection = sqlite3.connect(root / "tasks.sqlite3")
    connection.execute("PRAGMA user_version = 99")
    connection.close()

    with pytest.raises(TaskSchemaError, match="unsupported"):
        await DurableTaskStore.create(settings, profile="personal")


async def test_corrupt_database_and_invalid_rows_fail_without_sql_disclosure(
    tmp_path: Path,
) -> None:
    corrupt_settings = RickySettings(user_data_dir=str(tmp_path / "corrupt"))
    corrupt_root = tmp_path / "corrupt" / "profiles" / "personal" / "tasks"
    corrupt_root.mkdir(parents=True)
    (corrupt_root / "tasks.sqlite3").write_bytes(b"not a sqlite database")
    with pytest.raises(TaskStoreError) as error:
        await DurableTaskStore.create(corrupt_settings, profile="personal")
    assert "SELECT" not in str(error.value)
    assert "PRAGMA" not in str(error.value)

    store = await _store(tmp_path / "invalid")
    task = await store.create_task(
        title="Invalid row probe",
        objective="Detect corrupt state",
        closure_criteria="Invalid rows fail closed",
        execution_mode="agent",
        authority="agent_autonomy",
        executor_id="fixture",
    )
    connection = sqlite3.connect(store.db_path)
    connection.execute("PRAGMA ignore_check_constraints = ON")
    connection.execute("UPDATE tasks SET status = 'unknown' WHERE id = ?", (task.id,))
    connection.commit()
    connection.close()
    with pytest.raises(TaskSchemaError, match="invalid task row"):
        await store.get_task(task.id)


@pytest.mark.skipif(os.name != "posix", reason="private store modes are POSIX file modes")
async def test_reopening_a_loosened_store_restores_private_modes(tmp_path: Path) -> None:
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    store = await DurableTaskStore.create(settings, profile="personal")
    # A live WAL connection materializes the sidecars, which inherit whatever
    # mode the database file carried when SQLite created them.
    holder = sqlite3.connect(store.db_path)
    try:
        holder.execute("SELECT COUNT(*) FROM tasks").fetchone()
        sidecars = [Path(f"{store.db_path}-wal"), Path(f"{store.db_path}-shm")]
        assert [path for path in sidecars if path.is_file()] == sidecars
        directories = [store.user_root, store.root.parent, store.root, store.artifact_root]
        for directory in directories:
            directory.chmod(0o755)
        for path in (store.db_path, *sidecars):
            path.chmod(0o644)

        await DurableTaskStore.create(settings, profile="personal")

        for directory in directories:
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory
        for path in (store.db_path, *sidecars):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    finally:
        holder.close()
