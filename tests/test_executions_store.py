"""Transactional state-machine tests for the durable execution queue."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from ricky.config import ExecutionSettings, RickySettings
from ricky.executions.store import (
    SCHEMA_VERSION,
    ExecutionFenceError,
    ExecutionNotFoundError,
    ExecutionStore,
    ExecutionStoreError,
)
from ricky.executions.types import ExecutionRequest
from ricky.executions.upgrade import ExecutionsUpgradeAdapter
from ricky.profiles import ProfileScope

SCOPE = ProfileScope.create("personal")


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings(
        user_data_dir=str(tmp_path / "user"),
        executions=ExecutionSettings(
            claim_seconds=10,
            heartbeat_seconds=2,
            concurrency=2,
        ),
    )


def _request(now: datetime, *, key: str = "turn-1") -> ExecutionRequest:
    return ExecutionRequest(
        id=f"execution_{uuid4().hex}",
        kind="named_job",
        status="queued",
        named_job="personal/brief",
        job_digest="a" * 64,
        profile_scope=SCOPE,
        notification_route="owner",
        request_key=key,
        created_at=now,
    )


async def test_submit_deduplicates_and_two_workers_cannot_claim_same_request(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    store = ExecutionStore(_settings(tmp_path))
    await store.initialize()
    first = await store.submit(_request(now), scope=SCOPE)
    duplicate = await store.submit(_request(now), scope=SCOPE)
    assert duplicate.id == first.id
    left, right = await __import__("asyncio").gather(
        store.claim(worker_id="a", scope=SCOPE, limit=1, now=now),
        store.claim(worker_id="b", scope=SCOPE, limit=1, now=now),
    )
    assert len(left) + len(right) == 1


async def test_legacy_schema_requires_explicit_profile_migration(tmp_path: Path) -> None:
    store = ExecutionStore(_settings(tmp_path))
    await store.initialize()
    with sqlite3.connect(store.db_path) as database:
        database.execute("ALTER TABLE execution_requests DROP COLUMN project_root_ref")
        database.execute("PRAGMA user_version = 5")

    with pytest.raises(ExecutionStoreError, match="unsupported execution schema version"):
        await store.initialize()


async def test_phase7_browser_approval_schema_migrates_additively(tmp_path: Path) -> None:
    store = ExecutionStore(_settings(tmp_path))
    await store.initialize()
    request = await store.submit(_request(datetime(2026, 8, 11, 12, tzinfo=UTC)), scope=SCOPE)
    with sqlite3.connect(store.db_path) as database:
        database.execute("DROP TABLE execution_browser_attestations")
        database.execute("DROP TABLE execution_browser_approvals")
        database.execute("PRAGMA user_version = 7")

    with pytest.raises(ExecutionStoreError, match="requires migration"):
        await store.initialize()

    adapter = ExecutionsUpgradeAdapter((store.db_path,))
    target = adapter.discover(user_data_dir=store.user_root)[0]
    inspection = adapter.inspect(target)
    assert inspection.state == "migration_required"
    preflight = adapter.preflight(inspection)
    assert preflight.backup_paths == (str(store.db_path),)
    step = adapter.plan_steps(source_data_generation=1, target_data_generation=1)[0]
    adapter.apply(step)
    adapter.apply(step)
    assert adapter.verify(target).state == "current"
    await store.initialize()
    assert await store.get(request.id, scope=SCOPE) == request

    with sqlite3.connect(store.db_path) as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        names = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {
        "execution_browser_approvals",
        "execution_browser_attestations",
    } <= names


async def test_pre_run_expiry_requeues_but_post_start_expiry_is_uncertain(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    store = ExecutionStore(_settings(tmp_path))
    await store.initialize()
    await store.submit(_request(now, key="pre"), scope=SCOPE)
    await store.claim(worker_id="a", scope=SCOPE, limit=1, now=now)
    recovered = await store.recover_expired(scope=SCOPE, now=now + timedelta(seconds=11))
    assert recovered[0].status == "queued"

    claimed = (
        await store.claim(worker_id="b", scope=SCOPE, limit=1, now=now + timedelta(seconds=11))
    )[0]
    assert claimed.claim_token is not None
    running = await store.start(
        claimed.id,
        scope=SCOPE,
        token=claimed.claim_token,
        fence=claimed.claim_fence,
        run_id="jobrun_visible",
    )
    recovered = await store.recover_expired(scope=SCOPE, now=now + timedelta(seconds=22))
    assert recovered[0].status == "uncertain"
    assert recovered[0].run_id == running.run_id
    with pytest.raises(ExecutionFenceError):
        await store.finish(
            running.id,
            scope=SCOPE,
            token=claimed.claim_token,
            fence=claimed.claim_fence,
            status="succeeded",
        )
    resolved = await store.resolve(
        running.id,
        scope=SCOPE,
        disposition="confirmed_not_completed",
        actor="owner",
        note="Verified no result exists.",
    )
    assert resolved.status == "failed"
    assert (await store.resolutions(running.id, scope=SCOPE))[
        0
    ].note == "Verified no result exists."


async def test_fence_release_cancel_retry_and_resolution_are_append_only(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    store = ExecutionStore(_settings(tmp_path))
    await store.initialize()
    original = await store.submit(_request(now), scope=SCOPE)
    claimed = (await store.claim(worker_id="a", scope=SCOPE, limit=1, now=now))[0]
    assert claimed.claim_token is not None
    released = await store.release(
        claimed.id,
        scope=SCOPE,
        token=claimed.claim_token,
        fence=claimed.claim_fence,
    )
    assert released.status == "queued"
    claimed2 = (await store.claim(worker_id="b", scope=SCOPE, limit=1, now=now))[0]
    assert claimed2.claim_fence == claimed.claim_fence + 1
    with pytest.raises(ExecutionFenceError):
        await store.start(
            original.id,
            scope=SCOPE,
            token=claimed.claim_token,
            fence=claimed.claim_fence,
            run_id="jobrun_stale",
        )
    cancelled = await store.cancel(original.id, scope=SCOPE)
    child = await store.retry(cancelled.id, scope=SCOPE, created_at=now + timedelta(minutes=1))
    assert child.parent_request_id == original.id
    assert child.id != original.id
    assert (await store.get(original.id, scope=SCOPE)).status == "cancelled"
    assert len(await store.activities(original.id, scope=SCOPE)) >= 4


async def test_execution_queries_enforce_profile_scope(tmp_path: Path) -> None:
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    store = ExecutionStore(_settings(tmp_path))
    await store.initialize()
    submitted = await store.submit(_request(now), scope=SCOPE)
    work = ProfileScope.create("work")
    cross_profile = ProfileScope.create("work", access_profiles=["personal"])

    with pytest.raises(ExecutionNotFoundError):
        await store.get(submitted.id, scope=work)
    with pytest.raises(ExecutionNotFoundError):
        await store.cancel(submitted.id, scope=work)
    assert await store.list(scope=work) == []
    assert len(await store.list(scope=cross_profile)) == 1
