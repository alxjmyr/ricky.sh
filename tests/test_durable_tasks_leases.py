"""Concurrency, expiry, renewal, revision, and epoch fencing tests."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ricky.config import DurableTaskSettings, RickySettings
from ricky.durable_tasks.store import (
    DurableTaskStore,
    TaskConflictError,
    TaskLeaseError,
)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 25, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


async def _task(store: DurableTaskStore) -> str:
    task = await store.create_task(
        title="Lease probe",
        objective="Exercise one writer",
        closure_criteria="The lease behavior is verified",
        execution_mode="agent",
        authority="agent_autonomy",
        executor_id="fixture",
    )
    return task.id


async def test_competing_claims_have_one_winner(tmp_path: Path) -> None:
    store = await DurableTaskStore.create(
        RickySettings(user_data_dir=str(tmp_path)), profile="personal"
    )
    task_id = await _task(store)

    results = await asyncio.gather(
        store.claim(
            task_id,
            holder_session_id="a",
            authority="agent_autonomy",
            executor_id="a",
        ),
        store.claim(
            task_id,
            holder_session_id="b",
            authority="agent_autonomy",
            executor_id="b",
        ),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    error = next(result for result in results if isinstance(result, Exception))
    assert isinstance(error, TaskLeaseError)
    assert "leased by" in str(error)
    assert (await store.get_task(task_id)).title == "Lease probe"


async def test_expiry_recovers_with_higher_epoch_and_fences_stale_holder(
    tmp_path: Path,
) -> None:
    clock = Clock()
    settings = RickySettings(
        user_data_dir=str(tmp_path),
        durable_tasks=DurableTaskSettings(lease_seconds=10),
    )
    store = await DurableTaskStore.create(settings, profile="personal", clock=clock)
    task_id = await _task(store)
    first = await store.claim(
        task_id,
        holder_session_id="a",
        authority="agent_autonomy",
        executor_id="a",
    )
    assert first.lease is not None
    clock.value += timedelta(seconds=11)
    second = await store.claim(
        task_id,
        holder_session_id="b",
        authority="agent_autonomy",
        executor_id="b",
    )
    assert second.lease is not None
    assert second.lease.epoch == first.lease.epoch + 1

    with pytest.raises(TaskLeaseError, match="belongs|stale"):
        await store.progress(
            task_id,
            lease=first.lease,
            expected_revision=second.revision,
            current_summary="Stale write",
            next_action=None,
            authority="agent_autonomy",
            executor_id="a",
        )
    activity = await store.activities(task_id)
    assert "lease_expired" in [item.kind for item in activity]


async def test_renewal_and_stale_revision_fail_closed(tmp_path: Path) -> None:
    clock = Clock()
    store = await DurableTaskStore.create(
        RickySettings(
            user_data_dir=str(tmp_path),
            durable_tasks=DurableTaskSettings(lease_seconds=10),
        ),
        profile="personal",
        clock=clock,
    )
    task_id = await _task(store)
    claimed = await store.claim(
        task_id,
        holder_session_id="a",
        authority="agent_autonomy",
        executor_id="a",
    )
    assert claimed.lease is not None
    clock.value += timedelta(seconds=5)
    renewed = await store.renew(
        task_id,
        lease=claimed.lease,
        expected_revision=claimed.revision,
        authority="agent_autonomy",
        executor_id="a",
    )
    assert renewed.lease is not None
    assert renewed.lease.expires_at == clock.value + timedelta(seconds=10)

    with pytest.raises(TaskConflictError, match="revision conflict"):
        await store.progress(
            task_id,
            lease=renewed.lease,
            expected_revision=claimed.revision,
            current_summary="Stale revision",
            next_action=None,
            authority="agent_autonomy",
            executor_id="a",
        )


async def test_cancellation_waits_for_owned_sqlite_work_to_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await DurableTaskStore.create(
        RickySettings(user_data_dir=str(tmp_path)), profile="personal"
    )
    task_id = await _task(store)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original = store._get_task

    def blocked_get(value: str):
        started.set()
        assert release.wait(timeout=2)
        try:
            return original(value)
        finally:
            finished.set()

    monkeypatch.setattr(store, "_get_task", blocked_get)
    request = asyncio.create_task(store.get_task(task_id))
    assert await asyncio.to_thread(started.wait, 2)
    request.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert finished.is_set()
    assert original(task_id).id == task_id
