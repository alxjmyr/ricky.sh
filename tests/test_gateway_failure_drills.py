"""Gateway failure drills and their documented terminal states.

Each test names one drill from Section 9, stops the process at that exact point,
and asserts the durable state a restart must find. The expected user message,
operator message, and retry rule for every drill are recorded in
the durable recovery invariants in `.designs/architecture.md`.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gateway_ops_support import (
    PROFILE_SCOPE,
    FakeRunner,
    claimed_execution,
    enqueue_notification,
    job_run,
    make_conversation,
    make_session,
    reserve_effect,
    settings,
    store_inbound,
    transport_message,
)
from gateway_ops_support import inbound as build_inbound
from ricky.executions.store import ExecutionStore
from ricky.gateway.lock import GatewayLock, GatewayLockError
from ricky.gateway.recovery import GatewayRecovery
from ricky.gateway.service import GatewayService, ServiceEvent
from ricky.gateway.service_unit import GatewayServiceUnit
from ricky.gateway.store import GatewayStore
from ricky.jobs.store import JobRunStore
from ricky.messaging.store import MessagingStore, MessagingStoreError
from ricky.notifications.store import NotificationStore
from ricky.sessions.store import SessionStore

pytestmark = pytest.mark.asyncio


def _later(seconds: float = 10.0) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


# Drill 1: process stop during long poll.
async def test_drill_1_stop_during_long_poll_leaves_no_durable_evidence(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    messaging = MessagingStore(config)
    await messaging.initialize()
    await messaging.acquire_poller("telegram", "personal", owner="dead", lease_seconds=1)

    plan = await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=_later())

    assert plan.by_subsystem("poller")[0].disposition == "released"
    assert await messaging.list_inbox(limit=10) == []
    # Retry rule: poll again from the unchanged cursor. Nothing was committed.
    assert await messaging.cursor("telegram", "personal") is None


# Drill 2: process stop before inbox commit.
async def test_drill_2_stop_before_inbox_commit_loses_no_accepted_message(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    messaging = MessagingStore(config)
    await messaging.initialize()
    message = await store_inbound(messaging, build_inbound())
    await messaging.claim_inbox(message.id, owner="dead", lease_seconds=1)

    await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=_later())

    # Terminal state: pending. Retry rule: reprocess normally, no user message.
    assert (await messaging.get_inbox(message.id)).status == "pending"
    assert [item.id for item in await messaging.list_pending_oldest(limit=10)] == [message.id]


# Drill 3: process stop during a foreground provider stream.
async def test_drill_3_stop_during_provider_stream_is_uncertain_not_retried(
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
    await messaging.claim_inbox(message.id, owner="dead", lease_seconds=1)
    conversation_id, session_id = await make_conversation(gateway, sessions)
    await gateway.begin_result(
        message_id=message.id,
        conversation_id=conversation_id,
        session_id=session_id,
        scope=PROFILE_SCOPE,
    )

    await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=_later())

    # Terminal state: uncertain everywhere. Retry rule: never automatic.
    assert (await messaging.get_inbox(message.id)).status == "uncertain"
    assert (await gateway.get(conversation_id, scope=PROFILE_SCOPE)).status == "uncertain"
    assert await messaging.list_pending_oldest(limit=10) == []


# Drill 4: process stop after local task creation, before response delivery.
async def test_drill_4_stop_before_response_delivery_keeps_the_queued_reply(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    notifications = NotificationStore(config)
    executions = ExecutionStore(config)
    await notifications.initialize()
    await executions.initialize()
    entry = await enqueue_notification(notifications)
    now = datetime.now(UTC)
    request = await claimed_execution(executions, started=False, now=now)

    await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=now + timedelta(seconds=3_600))

    # The queued work and the queued reply both survive and both retry.
    assert (await notifications.get_outbox(entry.id, scope=PROFILE_SCOPE)).status == "pending"
    assert (await executions.get(request.id, scope=PROFILE_SCOPE)).status == "queued"


# Drill 5: process stop before background run start.
async def test_drill_5_stop_before_run_start_requeues_the_request(tmp_path: Path) -> None:
    config = settings(tmp_path)
    executions = ExecutionStore(config)
    await executions.initialize()
    now = datetime.now(UTC)
    request = await claimed_execution(executions, started=False, now=now)

    await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=now + timedelta(seconds=3_600))

    current = await executions.get(request.id, scope=PROFILE_SCOPE)
    assert current.status == "queued"
    assert current.claim_token is None
    # Retry rule: safe, because no run and therefore no effect ever began.
    assert current.error is None


# Drill 6: process stop during a background effect call.
async def test_drill_6_stop_during_an_effect_call_is_in_doubt_and_never_retried(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    executions = ExecutionStore(config)
    jobs = JobRunStore(config)
    await executions.initialize()
    await jobs.initialize()
    run = job_run()
    await jobs.insert(run, scope=PROFILE_SCOPE)
    action_id = await reserve_effect(jobs, run)
    now = datetime.now(UTC)
    request = await claimed_execution(executions, started=True, now=now)

    await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=now + timedelta(seconds=3_600))

    assert (await jobs.get_action(action_id, scope=PROFILE_SCOPE)).status == "in_doubt"
    assert (await executions.get(request.id, scope=PROFILE_SCOPE)).status == "uncertain"
    # Retry rule: forbidden. A human reconciles the action before anything reruns.
    reconciled, resolution = await jobs.reconcile_action(
        action_id, "not_performed", scope=PROFILE_SCOPE
    )
    assert reconciled.status == "not_performed" and resolution.actor == "ricky_job_cli"


# Drill 7: process stop during an outbox send.
async def test_drill_7_stop_during_send_is_in_doubt_when_a_part_was_prepared(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    notifications = NotificationStore(config)
    messaging = MessagingStore(config)
    # Both stores share one SQLite file, so they must initialize in sequence.
    await notifications.initialize()
    await messaging.initialize()
    entry = await enqueue_notification(notifications)
    claimed = await notifications.claim(
        entry.id,
        scope=PROFILE_SCOPE,
        worker="dead",
        transport="telegram",
        destination_ref="200",
        lease_seconds=1,
    )
    await messaging.prepare_parts(claimed, [transport_message(claimed)])

    await GatewayRecovery(config, scope=PROFILE_SCOPE).apply(now=_later())

    current = await notifications.get_outbox(entry.id, scope=PROFILE_SCOPE)
    assert current.status == "in_doubt"
    assert current.error is not None
    # Retry rule: forbidden until an operator resolves the ambiguous send.
    resolved = await notifications.resolve(
        entry.id,
        scope=PROFILE_SCOPE,
        disposition="not_delivered",
        actor="operator",
        note="checked the chat",
    )
    assert resolved.status in {"pending", "failed", "cancelled"}


# Drill 8: SQLite busy and temporary disk-full errors.
async def test_drill_8_a_storage_error_surfaces_as_a_bounded_store_error(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    messaging = MessagingStore(config)
    await messaging.initialize()

    def explode() -> None:
        raise sqlite3.OperationalError("database or disk is full")

    with pytest.raises(MessagingStoreError, match="messaging store operation failed"):
        await messaging._run(explode)  # noqa: SLF001 - the drill targets this boundary

    # The store stays usable once the transient condition clears.
    assert await messaging.inbox_counts() == {}


async def test_drill_8b_one_broken_store_does_not_hide_other_recovery(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    messaging = MessagingStore(config)
    await messaging.initialize()
    message = await store_inbound(messaging, build_inbound())
    await messaging.claim_inbox(message.id, owner="dead", lease_seconds=1)
    recovery = GatewayRecovery(config, scope=PROFILE_SCOPE)

    async def busy(**kwargs: object) -> list:  # type: ignore[type-arg]
        del kwargs
        raise sqlite3.OperationalError("database is locked")

    recovery.notifications.stale_claims = busy  # type: ignore[method-assign]

    plan = await recovery.apply(now=_later())

    assert plan.by_subsystem("inbox")[0].to_state == "pending"
    assert any("database is locked" in failure for failure in plan.failures)


# Drill 9: expired bot credential.
async def test_drill_9_a_missing_credential_fails_doctor_without_leaking_it(
    tmp_path: Path,
) -> None:
    from pydantic import SecretStr

    from ricky.gateway.health import GatewayHealth

    config = settings(tmp_path)
    account = config.messaging.telegram_accounts["personal/bot"]
    stripped = config.model_copy(
        update={
            "messaging": config.messaging.model_copy(
                update={
                    "telegram_accounts": {
                        "personal/bot": account.model_copy(update={"bot_token": SecretStr("")})
                    }
                }
            )
        }
    )
    unit = GatewayServiceUnit(
        stripped, unit_dir=tmp_path / "units", runner=FakeRunner(), executable="/usr/bin/uv"
    )

    report = await GatewayHealth(stripped, unit=unit).doctor()

    credential = next(
        check for check in report.checks if check.name == "telegram credential personal/bot"
    )
    assert credential.status == "fail"
    assert "missing from the owning profile" in credential.detail
    assert "test-token" not in credential.detail


# Drill 10: provider failure.
async def test_drill_10_a_provider_failure_is_recorded_and_reported(tmp_path: Path) -> None:
    config = settings(tmp_path)
    executions = ExecutionStore(config)
    await executions.initialize()
    now = datetime.now(UTC)
    request = await claimed_execution(executions, started=True, now=now)
    assert request.claim_token is not None
    await executions.finish(
        request.id,
        scope=PROFILE_SCOPE,
        token=request.claim_token,
        fence=request.claim_fence,
        status="failed",
        error="provider returned 503",
    )

    from ricky.gateway.health import GatewayHealth

    unit = GatewayServiceUnit(
        config, unit_dir=tmp_path / "units", runner=FakeRunner(), executable="/usr/bin/uv"
    )
    status = await GatewayHealth(config, unit=unit).status()

    assert status.executions["failed"] == 1
    assert any("provider returned 503" in error for error in status.recent_errors)


# Drill 11: malformed platform update.
async def test_drill_11_a_malformed_update_never_stops_an_unrelated_loop(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    events: list[ServiceEvent] = []
    failures: list[str] = []
    stop = asyncio.Event()

    class BrokenInbox:
        def __init__(self) -> None:
            self.calls = 0

        async def list_pending_oldest(self, **kwargs: object) -> list[Any]:
            del kwargs
            self.calls += 1
            if self.calls == 1:
                raise ValueError("malformed platform update")
            stop.set()
            return []

    class Coordinator:
        def __init__(self) -> None:
            self.messaging = BrokenInbox()

        async def initialize(self) -> None:
            return None

        async def process(self, message_id: str) -> None:
            del message_id

    class QuietMessaging:
        async def poll_once(self, account: str) -> None:
            del account
            await asyncio.sleep(0.01)

        async def deliver_once(self) -> int:
            await asyncio.sleep(0.01)
            return 0

        async def aclose(self) -> None:
            return None

    class QuietDispatcher:
        def __init__(self) -> None:
            self.store = SimpleNamespace(initialize=self._noop, recover_expired=self._noop_list)

        async def _noop(self) -> None:
            return None

        async def _noop_list(self) -> list[Any]:
            return []

        async def worker_once(self, *, scope) -> list[Any]:  # type: ignore[no-untyped-def]
            del scope
            await asyncio.sleep(0.01)
            return []

        async def project_notifications(self, *, scope) -> int:  # type: ignore[no-untyped-def]
            del scope
            return 0

    service = GatewayService(
        config,
        messaging=cast(Any, QuietMessaging()),
        conversations=cast(Any, Coordinator()),
        dispatcher=cast(Any, QuietDispatcher()),
        error_sink=lambda name, exc: failures.append(f"{name}: {exc}"),  # type: ignore[arg-type,func-returns-value]
        event_sink=events.append,
        recovery=GatewayRecovery(config, scope=PROFILE_SCOPE),
    )
    task = asyncio.create_task(service.run(stop=stop))
    await asyncio.wait_for(stop.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert any("malformed platform update" in failure for failure in failures)
    assert any(event.kind == "failure" for event in events)
    # The inbox loop restarted after the bad update instead of stopping the service.
    assert any(event.kind == "start" for event in events)


# Drill 12: service restart loop.
async def test_drill_12_a_second_gateway_cannot_start_against_one_user_data_dir(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    first = GatewayLock(config)
    second = GatewayLock(config)

    owner = first.acquire()
    try:
        with pytest.raises(GatewayLockError, match="another gateway already owns"):
            second.acquire()
        assert first.is_active() is True
    finally:
        first.release()

    # After a clean stop the root is free again, so a supervisor restart succeeds.
    third = GatewayLock(config)
    restarted = third.acquire()
    third.release()
    assert restarted.pid == owner.pid


async def test_drill_12b_a_lock_file_from_a_dead_process_is_reacquirable(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    lock = GatewayLock(config)
    lock.acquire()
    lock.release()

    assert lock.path.exists()
    assert lock.is_active() is False
    reacquired = GatewayLock(config)
    reacquired.acquire()
    reacquired.release()


async def test_a_running_gateway_takes_the_lock_before_any_recovery_write(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    messaging = MessagingStore(config)
    await messaging.initialize()
    message = await store_inbound(messaging, build_inbound())
    await messaging.claim_inbox(message.id, owner="dead", lease_seconds=1)
    blocker = GatewayLock(config)
    blocker.acquire()

    class QuietMessaging:
        async def aclose(self) -> None:
            return None

    service = GatewayService(
        config,
        messaging=cast(Any, QuietMessaging()),
        conversations=cast(Any, SimpleNamespace()),
        dispatcher=cast(Any, SimpleNamespace()),
    )
    try:
        with pytest.raises(ValueError, match="another gateway already owns"):
            await service.run()
    finally:
        blocker.release()

    # Recovery never ran, so the interrupted claim is untouched.
    assert (await messaging.get_inbox(message.id)).status == "claimed"


async def test_startup_recovery_runs_before_any_loop_claims_work(tmp_path: Path) -> None:
    config = settings(tmp_path)
    messaging = MessagingStore(config)
    sessions = SessionStore(config)
    await messaging.initialize()
    await sessions.initialize()
    message = await store_inbound(messaging, build_inbound())
    await messaging.claim_inbox(message.id, owner="dead", lease_seconds=1)
    await make_session(sessions)
    events: list[ServiceEvent] = []
    stop = asyncio.Event()

    class ObservingCoordinator:
        def __init__(self) -> None:
            self.inbox_states: list[str] = []
            self.messaging = self

        async def initialize(self) -> None:
            return None

        async def list_pending_oldest(self, **kwargs: object) -> list[Any]:
            del kwargs
            current = await messaging.get_inbox(message.id)
            self.inbox_states.append(current.status)
            stop.set()
            return []

        async def process(self, message_id: str) -> None:
            del message_id

    class QuietMessaging:
        async def poll_once(self, account: str) -> None:
            del account
            await asyncio.sleep(0.01)

        async def deliver_once(self) -> int:
            await asyncio.sleep(0.01)
            return 0

        async def aclose(self) -> None:
            return None

    class QuietDispatcher:
        def __init__(self) -> None:
            self.store = SimpleNamespace(initialize=self._noop, recover_expired=self._noop_list)

        async def _noop(self) -> None:
            return None

        async def _noop_list(self) -> list[Any]:
            return []

        async def worker_once(self, *, scope) -> list[Any]:  # type: ignore[no-untyped-def]
            del scope
            await asyncio.sleep(0.01)
            return []

        async def project_notifications(self, *, scope) -> int:  # type: ignore[no-untyped-def]
            del scope
            return 0

    class ExpiredClockRecovery(GatewayRecovery):
        """Drive the real recovery pass from a moment after the lease expired."""

        async def apply(self, *, now: datetime | None = None) -> Any:
            return await super().apply(now=now or _later())

    coordinator = ObservingCoordinator()
    service = GatewayService(
        config,
        messaging=cast(Any, QuietMessaging()),
        conversations=cast(Any, coordinator),
        dispatcher=cast(Any, QuietDispatcher()),
        event_sink=events.append,
        recovery=ExpiredClockRecovery(config, scope=PROFILE_SCOPE),
    )
    task = asyncio.create_task(service.run(stop=stop))
    await asyncio.wait_for(stop.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # By the time any loop looked at the inbox, recovery had already resolved it.
    assert coordinator.inbox_states and set(coordinator.inbox_states) == {"pending"}
    assert any(event.kind == "recovery" for event in events)


async def test_startup_recovery_can_be_disabled(tmp_path: Path) -> None:
    config = settings(tmp_path, startup_recovery=False)
    messaging = MessagingStore(config)
    await messaging.initialize()
    message = await store_inbound(messaging, build_inbound())
    await messaging.claim_inbox(message.id, owner="dead", lease_seconds=1)

    class Failing:
        async def apply(self, **kwargs: object) -> None:
            raise AssertionError("recovery must not run when it is disabled")

    class QuietMessaging:
        async def aclose(self) -> None:
            return None

    service = GatewayService(
        config,
        messaging=cast(Any, QuietMessaging()),
        conversations=cast(Any, SimpleNamespace()),
        dispatcher=cast(Any, SimpleNamespace()),
        recovery=cast(Any, Failing()),
    )
    stop = asyncio.Event()
    stop.set()
    with pytest.raises((AttributeError, TypeError)):
        await service.run(stop=stop)

    assert (await messaging.get_inbox(message.id)).status == "claimed"
