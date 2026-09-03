"""Deterministic startup-recovery rules for every interrupted subsystem."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gateway_ops_support import (
    PROFILE_SCOPE,
    claimed_execution,
    enqueue_notification,
    job_run,
    make_conversation,
    reserve_effect,
    settings,
    store_inbound,
    transport_message,
)
from gateway_ops_support import inbound as build_inbound
from ricky.executions.browser import BrowserExecutionBudget, BrowserExecutionScope
from ricky.executions.store import ExecutionStore
from ricky.gateway.recovery import GatewayRecovery
from ricky.gateway.store import GatewayStore
from ricky.jobs.browser_store import BrowserRunLedger
from ricky.jobs.lock import browser_worker_lease
from ricky.jobs.store import JobRunStore
from ricky.messaging.store import InboxLeaseError, MessagingStore
from ricky.notifications.store import NotificationLeaseError, NotificationStore
from ricky.sessions.store import SessionLeaseError, SessionStore

pytestmark = pytest.mark.asyncio


async def test_a_claimed_message_with_no_started_turn_is_safely_reclaimed(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    messaging = MessagingStore(config)
    await messaging.initialize()
    message = await store_inbound(messaging, build_inbound())
    await messaging.claim_inbox(message.id, owner="dead-worker", lease_seconds=1)
    later = datetime.now(UTC) + timedelta(seconds=10)

    plan = await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=later)

    actions = plan.by_subsystem("inbox")
    assert len(actions) == 1
    assert actions[0].to_state == "pending"
    assert actions[0].disposition == "reclaimed"
    assert (await messaging.get_inbox(message.id)).status == "pending"


async def test_a_claimed_message_whose_turn_started_becomes_uncertain(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    messaging = MessagingStore(config)
    gateway = GatewayStore(config)
    sessions = SessionStore(config)
    await messaging.initialize()
    await gateway.initialize()
    await sessions.initialize()
    message = await store_inbound(messaging, build_inbound())
    await messaging.claim_inbox(message.id, owner="dead-worker", lease_seconds=1)
    conversation_id, session_id = await make_conversation(gateway, sessions)
    await gateway.begin_result(
        message_id=message.id,
        conversation_id=conversation_id,
        session_id=session_id,
        scope=PROFILE_SCOPE,
    )
    later = datetime.now(UTC) + timedelta(seconds=10)

    plan = await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=later)

    assert plan.by_subsystem("inbox")[0].to_state == "uncertain"
    assert (await messaging.get_inbox(message.id)).status == "uncertain"
    result = await gateway.result_for_message(message.id, scope=PROFILE_SCOPE)
    assert result is not None and result.status == "uncertain"
    assert (await gateway.get(conversation_id, scope=PROFILE_SCOPE)).status == "uncertain"


@pytest.mark.parametrize(
    ("result_status", "inbox_status"),
    [
        ("committed", "processed"),
        ("failed", "processed"),
        ("uncertain", "uncertain"),
    ],
)
async def test_terminal_gateway_result_authoritatively_settles_expired_inbox_claim(
    tmp_path: Path,
    result_status: str,
    inbox_status: str,
) -> None:
    config = settings(tmp_path)
    messaging = MessagingStore(config)
    gateway = GatewayStore(config)
    sessions = SessionStore(config)
    await messaging.initialize()
    await gateway.initialize()
    await sessions.initialize()
    message = await store_inbound(messaging, build_inbound())
    await messaging.claim_inbox(message.id, owner="dead-worker", lease_seconds=1)
    conversation_id, session_id = await make_conversation(gateway, sessions)
    await gateway.begin_result(
        message_id=message.id,
        conversation_id=conversation_id,
        session_id=session_id,
        scope=PROFILE_SCOPE,
    )
    await gateway.finish_result(
        message_id=message.id,
        conversation_id=conversation_id,
        expected_conversation_revision=0,
        status=result_status,  # type: ignore[arg-type]
        session_revision=0,
        response_outbox_id=None,
        error="terminal test" if result_status in {"failed", "uncertain"} else None,
        scope=PROFILE_SCOPE,
    )
    later = datetime.now(UTC) + timedelta(seconds=10)

    first = await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=later)
    second = await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=later)

    assert first.by_subsystem("inbox")[0].to_state == inbox_status
    assert second.by_subsystem("inbox") == ()
    assert (await messaging.get_inbox(message.id)).status == inbox_status


async def test_a_stale_worker_cannot_commit_after_inbox_recovery(tmp_path: Path) -> None:
    config = settings(tmp_path)
    messaging = MessagingStore(config)
    await messaging.initialize()
    message = await store_inbound(messaging, build_inbound())
    claim = await messaging.claim_inbox(message.id, owner="dead-worker", lease_seconds=1)
    later = datetime.now(UTC) + timedelta(seconds=10)
    await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=later)

    with pytest.raises(InboxLeaseError):
        await messaging.finish_inbox(claim, status="processed")


async def test_an_expired_poller_lease_is_released(tmp_path: Path) -> None:
    config = settings(tmp_path)
    messaging = MessagingStore(config)
    await messaging.initialize()
    await messaging.acquire_poller("telegram", "personal", owner="dead", lease_seconds=1)
    later = datetime.now(UTC) + timedelta(seconds=10)

    plan = await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=later)

    assert plan.by_subsystem("poller")[0].disposition == "released"
    assert await messaging.stale_poller_leases(now=later) == []
    # The account is free, so a fresh poller can take the lease immediately.
    await messaging.acquire_poller("telegram", "personal", owner="fresh", lease_seconds=60)


async def test_a_running_foreground_turn_becomes_uncertain(tmp_path: Path) -> None:
    config = settings(tmp_path)
    gateway = GatewayStore(config)
    sessions = SessionStore(config)
    await gateway.initialize()
    await sessions.initialize()
    conversation_id, session_id = await make_conversation(gateway, sessions)
    await gateway.begin_result(
        message_id=f"inbound_{'a' * 32}",
        conversation_id=conversation_id,
        session_id=session_id,
        scope=PROFILE_SCOPE,
    )

    plan = await GatewayRecovery(config, scope=PROFILE_SCOPE).apply()

    turns = plan.by_subsystem("foreground_turn")
    assert len(turns) == 1 and turns[0].to_state == "uncertain"
    assert await gateway.running_results(scope=PROFILE_SCOPE) == []


async def test_an_expired_session_lease_with_no_running_turn_is_only_released(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    sessions = SessionStore(config)
    await sessions.initialize()
    from gateway_ops_support import make_session

    session_id = await make_session(sessions)
    await sessions.acquire(session_id, "dead-worker", scope=PROFILE_SCOPE, lease_seconds=1)
    later = datetime.now(UTC) + timedelta(seconds=10)

    plan = await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=later)

    actions = plan.by_subsystem("session")
    assert len(actions) == 1 and actions[0].disposition == "released"
    assert (await sessions.get(session_id, scope=PROFILE_SCOPE)).status == "active"


async def test_a_stale_session_lease_cannot_commit_after_recovery(tmp_path: Path) -> None:
    config = settings(tmp_path)
    sessions = SessionStore(config)
    await sessions.initialize()
    from gateway_ops_support import make_session

    session_id = await make_session(sessions)
    lease = await sessions.acquire(session_id, "dead-worker", scope=PROFILE_SCOPE, lease_seconds=1)
    later = datetime.now(UTC) + timedelta(seconds=10)
    await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=later)

    with pytest.raises(SessionLeaseError):
        await sessions.renew(lease)


async def test_an_execution_claim_with_no_run_start_is_requeued(tmp_path: Path) -> None:
    config = settings(tmp_path)
    executions = ExecutionStore(config)
    await executions.initialize()
    now = datetime.now(UTC)
    request = await claimed_execution(executions, started=False, now=now)
    later = now + timedelta(seconds=3_600)

    plan = await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=later)

    actions = plan.by_subsystem("execution")
    assert len(actions) == 1 and actions[0].to_state == "queued"
    assert (await executions.get(request.id, scope=PROFILE_SCOPE)).status == "queued"


async def test_an_execution_with_a_recorded_run_start_becomes_uncertain(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    executions = ExecutionStore(config)
    await executions.initialize()
    now = datetime.now(UTC)
    request = await claimed_execution(executions, started=True, now=now)
    later = now + timedelta(seconds=3_600)

    plan = await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=later)

    actions = plan.by_subsystem("execution")
    assert len(actions) == 1 and actions[0].to_state == "uncertain"
    assert (await executions.get(request.id, scope=PROFILE_SCOPE)).status == "uncertain"


async def test_restart_invalidates_disowned_browser_attempt_without_replay(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    executions = ExecutionStore(config)
    jobs = JobRunStore(config)
    ledger = BrowserRunLedger(config)
    await executions.initialize()
    await jobs.initialize()
    now = datetime.now(UTC)
    request = await claimed_execution(executions, started=True, now=now)
    assert request.run_id is not None
    run = job_run(run_id=request.run_id).model_copy(
        update={
            "job_name": None,
            "trigger": "execution",
            "trigger_id": request.id,
        }
    )
    await jobs.insert(run, scope=PROFILE_SCOPE)
    scope = BrowserExecutionScope(
        mode="read_only",
        allow_ephemeral=True,
        allow_public_https_research=True,
        allowed_tools=("browser_session_open", "browser_snapshot"),
        allowed_operations=(
            "session_starts",
            "controlled_pages",
            "semantic_observations",
        ),
        budget=BrowserExecutionBudget(
            session_starts=1,
            navigations=0,
            scrolls=0,
            created_pages=0,
            controlled_pages=1,
            semantic_observations=2,
            visual_observations=0,
            interactions=0,
            protected_materializations=0,
            uploads=0,
            upload_bytes=0,
            downloads=0,
            download_bytes=0,
            transaction_commits=0,
            parked_browsers=0,
            approval_ttl_seconds=120,
        ),
    )
    lease = await ledger.start_attempt(
        run_id=run.id,
        scope=PROFILE_SCOPE,
        browser_scope=scope,
        claim_fence=request.claim_fence,
        worker_id="lost-worker",
        execution_request_id=request.id,
        resource=None,
        resource_kind="ephemeral",
        resource_configuration_digest=None,
        now=now,
    )
    await ledger.transition(
        lease.attempt.id,
        scope=PROFILE_SCOPE,
        owner_token=lease.owner_token,
        claim_fence=request.claim_fence,
        status="running",
        now=now,
    )
    await reserve_effect(jobs, run)
    later = now + timedelta(seconds=3_600)

    plan = await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=later)

    [action] = plan.by_subsystem("browser_attempt")
    assert action.to_state == "in_doubt"
    recovered = await ledger.get_attempt(lease.attempt.id, scope=PROFILE_SCOPE)
    assert recovered.status == "in_doubt"
    assert recovered.cleanup == "failed"
    assert (await executions.get(request.id, scope=PROFILE_SCOPE)).status == "uncertain"


async def test_restart_recovers_named_job_browser_after_worker_process_exits(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    jobs = JobRunStore(config)
    ledger = BrowserRunLedger(config)
    await jobs.initialize()
    now = datetime.now(UTC)
    run = job_run().model_copy(
        update={
            "job_name": "personal/research",
            "trigger": "manual",
            "trigger_id": None,
        }
    )
    await jobs.insert(run, scope=PROFILE_SCOPE)
    scope = BrowserExecutionScope(
        mode="read_only",
        allow_ephemeral=True,
        allow_public_https_research=True,
        allowed_tools=("browser_session_open", "browser_snapshot"),
        allowed_operations=(
            "session_starts",
            "controlled_pages",
            "semantic_observations",
        ),
        budget=BrowserExecutionBudget(
            session_starts=1,
            navigations=0,
            scrolls=0,
            created_pages=0,
            controlled_pages=1,
            semantic_observations=2,
            visual_observations=0,
            interactions=0,
            protected_materializations=0,
            uploads=0,
            upload_bytes=0,
            downloads=0,
            download_bytes=0,
            transaction_commits=0,
            parked_browsers=0,
            approval_ttl_seconds=120,
        ),
    )
    worker = browser_worker_lease(jobs.root)
    lease = await ledger.start_attempt(
        run_id=run.id,
        scope=PROFILE_SCOPE,
        browser_scope=scope,
        claim_fence=1,
        worker_id=worker.identity,
        execution_request_id=None,
        resource=None,
        resource_kind="ephemeral",
        resource_configuration_digest=None,
        now=now,
    )
    await ledger.transition(
        lease.attempt.id,
        scope=PROFILE_SCOPE,
        owner_token=lease.owner_token,
        claim_fence=1,
        status="running",
        now=now,
    )
    worker.release()

    inspected = await GatewayRecovery(config, scope=PROFILE_SCOPE).inspect(now=now)
    [planned] = inspected.by_subsystem("browser_attempt")
    assert planned.to_state == "failed"
    assert (await ledger.get_attempt(lease.attempt.id, scope=PROFILE_SCOPE)).status == "running"

    applied = await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=now)
    [action] = applied.by_subsystem("browser_attempt")
    assert action.to_state == "failed"
    recovered = await ledger.get_attempt(lease.attempt.id, scope=PROFILE_SCOPE)
    assert recovered.status == "failed"
    assert recovered.cleanup == "failed"


async def test_an_outbox_claim_with_no_prepared_part_is_reclaimed(tmp_path: Path) -> None:
    config = settings(tmp_path)
    notifications = NotificationStore(config)
    await notifications.initialize()
    entry = await enqueue_notification(notifications)
    await notifications.claim(
        entry.id,
        worker="dead",
        transport="telegram",
        destination_ref="200",
        lease_seconds=1,
        scope=PROFILE_SCOPE,
    )
    later = datetime.now(UTC) + timedelta(seconds=10)

    plan = await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=later)

    actions = plan.by_subsystem("outbox")
    assert len(actions) == 1 and actions[0].to_state == "pending"
    assert (await notifications.get_outbox(entry.id, scope=PROFILE_SCOPE)).status == "pending"


async def test_an_outbox_claim_with_a_prepared_part_becomes_in_doubt(tmp_path: Path) -> None:
    config = settings(tmp_path)
    notifications = NotificationStore(config)
    messaging = MessagingStore(config)
    await notifications.initialize()
    await messaging.initialize()
    entry = await enqueue_notification(notifications)
    claimed = await notifications.claim(
        entry.id,
        worker="dead",
        transport="telegram",
        destination_ref="200",
        lease_seconds=1,
        scope=PROFILE_SCOPE,
    )
    await messaging.prepare_parts(claimed, [transport_message(claimed)])
    later = datetime.now(UTC) + timedelta(seconds=10)

    plan = await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=later)

    actions = plan.by_subsystem("outbox")
    assert len(actions) == 1 and actions[0].to_state == "in_doubt"
    assert (await notifications.get_outbox(entry.id, scope=PROFILE_SCOPE)).status == "in_doubt"


async def test_a_stale_delivery_worker_cannot_commit_after_recovery(tmp_path: Path) -> None:
    config = settings(tmp_path)
    notifications = NotificationStore(config)
    await notifications.initialize()
    entry = await enqueue_notification(notifications)
    claimed = await notifications.claim(
        entry.id,
        worker="dead",
        transport="telegram",
        destination_ref="200",
        lease_seconds=1,
        scope=PROFILE_SCOPE,
    )
    later = datetime.now(UTC) + timedelta(seconds=10)
    await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=later)

    with pytest.raises(NotificationLeaseError):
        await notifications.mark_delivered(
            claimed, scope=PROFILE_SCOPE, platform_message_id="platform-1"
        )


async def test_an_unresolved_effect_reservation_becomes_in_doubt(tmp_path: Path) -> None:
    config = settings(tmp_path)
    jobs = JobRunStore(config)
    await jobs.initialize()
    run = job_run()
    await jobs.insert(run, scope=PROFILE_SCOPE)
    action_id = await reserve_effect(jobs, run)

    plan = await GatewayRecovery(config, scope=PROFILE_SCOPE).apply()

    actions = plan.by_subsystem("effect")
    assert len(actions) == 1 and actions[0].to_state == "in_doubt"
    assert (await jobs.get_action(action_id, scope=PROFILE_SCOPE)).status == "in_doubt"
    # Recovery never guesses an external outcome; a human still reconciles it.
    assert actions[0].disposition == "in_doubt"


async def test_dry_run_recovery_changes_no_state(tmp_path: Path) -> None:
    config = settings(tmp_path)
    messaging = MessagingStore(config)
    notifications = NotificationStore(config)
    jobs = JobRunStore(config)
    await messaging.initialize()
    await notifications.initialize()
    await jobs.initialize()
    message = await store_inbound(messaging, build_inbound())
    await messaging.claim_inbox(message.id, owner="dead", lease_seconds=1)
    entry = await enqueue_notification(notifications)
    await notifications.claim(
        entry.id,
        worker="dead",
        transport="telegram",
        destination_ref="200",
        lease_seconds=1,
        scope=PROFILE_SCOPE,
    )
    run = job_run()
    await jobs.insert(run, scope=PROFILE_SCOPE)
    action_id = await reserve_effect(jobs, run)
    later = datetime.now(UTC) + timedelta(seconds=10)

    plan = await GatewayRecovery(config, scope=PROFILE_SCOPE).inspect(now=later)

    assert plan.applied is False
    assert len(plan.actions) >= 3
    assert all(action.applied is False for action in plan.actions)
    assert (await messaging.get_inbox(message.id)).status == "claimed"
    assert (await notifications.get_outbox(entry.id, scope=PROFILE_SCOPE)).status == "claimed"
    assert (await jobs.get_action(action_id, scope=PROFILE_SCOPE)).status == "reserved"


async def test_recovery_is_idempotent(tmp_path: Path) -> None:
    config = settings(tmp_path)
    messaging = MessagingStore(config)
    await messaging.initialize()
    message = await store_inbound(messaging, build_inbound())
    await messaging.claim_inbox(message.id, owner="dead", lease_seconds=1)
    later = datetime.now(UTC) + timedelta(seconds=10)

    first = await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=later)
    second = await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=later)

    assert len(first.actions) == 1
    assert second.actions == ()
    assert second.failures == ()
    assert (await messaging.get_inbox(message.id)).status == "pending"


async def test_one_broken_subsystem_does_not_hide_the_others(tmp_path: Path) -> None:
    config = settings(tmp_path)
    messaging = MessagingStore(config)
    await messaging.initialize()
    message = await store_inbound(messaging, build_inbound())
    await messaging.claim_inbox(message.id, owner="dead", lease_seconds=1)
    recovery = GatewayRecovery(config, scope=PROFILE_SCOPE)

    async def broken(*, scope) -> list:  # type: ignore[type-arg,no-untyped-def]
        del scope
        raise RuntimeError("effect ledger is unreadable")

    recovery.jobs.reserved_actions = broken  # type: ignore[method-assign]
    later = datetime.now(UTC) + timedelta(seconds=10)

    plan = await recovery.apply(now=later)

    assert plan.by_subsystem("inbox")[0].to_state == "pending"
    assert any("effect ledger is unreadable" in failure for failure in plan.failures)
