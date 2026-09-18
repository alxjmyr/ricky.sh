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
        for column in (
            "handoff_title",
            "acknowledgement_outbox_id",
            "acknowledgement_delivered_at",
            "acknowledgement_expires_at",
        ):
            database.execute(f"ALTER TABLE execution_requests DROP COLUMN {column}")
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


@pytest.mark.parametrize("settlement", ["release", "cancel", "expire", "recover_expired"])
async def test_acknowledgement_admission_is_fenced_and_never_revives_terminal_work(
    tmp_path: Path,
    settlement: str,
) -> None:
    now = datetime.now(UTC)
    store = ExecutionStore(_settings(tmp_path))
    await store.initialize()
    held = _request(now).model_copy(
        update={
            "status": "awaiting_acknowledgement",
            "handoff_title": "Check balance",
            "source_conversation_id": "conversation-test",
            "source_message_id": "inbound-test",
            "acknowledgement_expires_at": now + timedelta(seconds=60),
        }
    )
    held = await store.submit(held, scope=SCOPE)
    assert await store.submit(held, scope=SCOPE) == held
    assert await store.claim(worker_id="early", scope=SCOPE, limit=1, now=now) == []
    assert await store.list_pending_acknowledgements(scope=SCOPE) == [held]
    assert await store.list_pending_acknowledgements(scope=ProfileScope.create("work")) == []
    with pytest.raises(ExecutionStoreError, match="not attached"):
        await store.release_acknowledged(held.id, "outbox-test", scope=SCOPE, now=now)
    await store.attach_acknowledgement(held.id, "outbox-test", scope=SCOPE)
    await store.attach_acknowledgement(held.id, "outbox-test", scope=SCOPE)
    with pytest.raises(ExecutionStoreError, match="different acknowledgement"):
        await store.attach_acknowledgement(held.id, "outbox-other", scope=SCOPE)
    with pytest.raises(ExecutionNotFoundError):
        await store.release_acknowledged(held.id, "outbox-test", scope=ProfileScope.create("work"))
    if settlement == "cancel":
        await store.cancel(held.id, scope=SCOPE)
    if settlement in {"expire", "recover_expired"}:
        now += timedelta(seconds=61)
    if settlement == "recover_expired":
        [expired] = await store.recover_expired(scope=SCOPE, now=now)
        assert expired.status == "blocked"
    released = await store.release_acknowledged(held.id, "outbox-test", scope=SCOPE, now=now)
    assert (
        released.status
        == {
            "release": "queued",
            "cancel": "cancelled",
            "expire": "blocked",
            "recover_expired": "blocked",
        }[settlement]
    )
    assert released.acknowledgement_delivered_at == now
    assert (
        await store.release_acknowledged(
            held.id, "outbox-test", scope=SCOPE, now=now + timedelta(seconds=1)
        )
        == released
    )
    assert await store.list_pending_acknowledgements(scope=SCOPE) == []
    claimed = await store.claim(worker_id="late", scope=SCOPE, limit=1, now=now)
    assert len(claimed) == (1 if settlement == "release" else 0)


async def test_v8_migration_preserves_legacy_queued_and_running_admission(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    store = ExecutionStore(_settings(tmp_path))
    await store.initialize()
    first = await store.submit(_request(now, key="first"), scope=SCOPE)
    [claimed] = await store.claim(worker_id="legacy", scope=SCOPE, limit=1, now=now)
    assert claimed.claim_token is not None
    running = await store.start(
        first.id,
        scope=SCOPE,
        token=claimed.claim_token,
        fence=claimed.claim_fence,
        run_id="legacy-run",
    )
    queued = await store.submit(_request(now, key="second"), scope=SCOPE)
    with sqlite3.connect(store.db_path) as database:
        for column in (
            "handoff_title",
            "acknowledgement_outbox_id",
            "acknowledgement_delivered_at",
            "acknowledgement_expires_at",
        ):
            database.execute(f"ALTER TABLE execution_requests DROP COLUMN {column}")
        database.execute("PRAGMA user_version = 8")
    with pytest.raises(ExecutionStoreError, match="requires migration"):
        await store.initialize()
    adapter = ExecutionsUpgradeAdapter((store.db_path,))
    [step] = adapter.plan_steps(source_data_generation=1, target_data_generation=1)
    assert (step.source_schema_version, step.target_schema_version) == (8, 9)
    adapter.apply(step)
    adapter.apply(step)
    await store.initialize()
    assert await store.get(first.id, scope=SCOPE) == running
    assert await store.get(queued.id, scope=SCOPE) == queued
    assert await store.list_pending_acknowledgements(scope=SCOPE) == []


async def test_expired_named_handoff_explicit_retry_gets_fresh_admission(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    store = ExecutionStore(_settings(tmp_path))
    await store.initialize()
    held = _request(now).model_copy(
        update={
            "status": "awaiting_acknowledgement",
            "handoff_title": "Prepare brief",
            "source_conversation_id": "conversation-test",
            "source_message_id": "inbound-test",
            "acknowledgement_expires_at": now + timedelta(seconds=1),
        }
    )
    await store.submit(held, scope=SCOPE)
    await store.recover_expired(scope=SCOPE, now=now + timedelta(seconds=2))
    retried = await store.retry(held.id, scope=SCOPE, created_at=now + timedelta(seconds=3))
    assert retried.status == "queued"
    assert retried.handoff_title is None
    assert retried.acknowledgement_outbox_id is None
    assert retried.acknowledgement_delivered_at is None
    assert retried.expires_at is None
    [claimed] = await store.claim(
        worker_id="retry", scope=SCOPE, limit=1, now=now + timedelta(seconds=4)
    )
    assert claimed.id == retried.id


@pytest.mark.parametrize("version", [7, 8])
async def test_partially_migrated_handoff_schema_is_rejected(tmp_path: Path, version: int) -> None:
    store = ExecutionStore(_settings(tmp_path))
    await store.initialize()
    with sqlite3.connect(store.db_path) as database:
        if version == 7:
            database.execute("DROP TABLE execution_browser_attestations")
            database.execute("DROP TABLE execution_browser_approvals")
        database.execute("ALTER TABLE execution_requests DROP COLUMN acknowledgement_delivered_at")
        database.execute(f"PRAGMA user_version = {version}")
    adapter = ExecutionsUpgradeAdapter((store.db_path,))
    [target] = adapter.discover(user_data_dir=store.user_root)
    assert adapter.inspect(target).state == "corrupt"
    with pytest.raises(ExecutionStoreError, match="partially migrated"):
        await store.initialize()


async def test_released_handoff_that_expires_before_claim_becomes_blocked(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    store = ExecutionStore(_settings(tmp_path))
    await store.initialize()
    held = _request(now).model_copy(
        update={
            "status": "awaiting_acknowledgement",
            "handoff_title": "Prepare brief",
            "source_conversation_id": "conversation-test",
            "source_message_id": "inbound-test",
            "acknowledgement_expires_at": now + timedelta(seconds=1),
            "expires_at": now + timedelta(seconds=1),
        }
    )
    await store.submit(held, scope=SCOPE)
    await store.attach_acknowledgement(held.id, "outbox-test", scope=SCOPE)
    await store.release_acknowledged(held.id, "outbox-test", scope=SCOPE, now=now)
    assert (
        await store.claim(worker_id="late", scope=SCOPE, limit=1, now=now + timedelta(seconds=2))
        == []
    )
    expired = await store.get(held.id, scope=SCOPE)
    assert expired.status == "blocked"
    assert expired.acknowledgement_delivered_at == now


async def test_acknowledgement_deadline_does_not_expire_released_named_work(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    store = ExecutionStore(_settings(tmp_path))
    await store.initialize()
    held = _request(now).model_copy(
        update={
            "status": "awaiting_acknowledgement",
            "handoff_title": "Prepare brief",
            "source_conversation_id": "conversation-test",
            "source_message_id": "inbound-test",
            "acknowledgement_expires_at": now + timedelta(seconds=1),
        }
    )
    await store.submit(held, scope=SCOPE)
    await store.attach_acknowledgement(held.id, "outbox-test", scope=SCOPE)
    await store.release_acknowledged(held.id, "outbox-test", scope=SCOPE, now=now)
    [claimed] = await store.claim(
        worker_id="late", scope=SCOPE, limit=1, now=now + timedelta(seconds=2)
    )
    assert claimed.id == held.id
    assert claimed.expires_at is None
