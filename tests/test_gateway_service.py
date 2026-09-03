"""Structured gateway service concurrency and cancellation tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from ricky.config import (
    GatewaySettings,
    MessagingRouteSettings,
    MessagingSettings,
    MessagingTransportSettings,
    RickySettings,
    TelegramAccountSettings,
)
from ricky.gateway.health import DoctorCheck, GatewayHealth
from ricky.gateway.service import GatewayService
from ricky.profiles import ProfileScope

_SCOPE = ProfileScope.create("personal")


class BlockingMessaging:
    def __init__(self) -> None:
        self.poll_started = asyncio.Event()
        self.delivery_started = asyncio.Event()
        self.cancelled: set[str] = set()
        self.closed = False

    async def poll_once(self, account: str) -> None:
        del account
        self.poll_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.add("poll")
            raise

    async def deliver_once(self) -> int:
        self.delivery_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.add("delivery")
            raise
        return 0

    async def aclose(self) -> None:
        self.closed = True


class BlockingInbox:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def list_pending_oldest(self, **kwargs: object) -> list[Any]:
        del kwargs
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return []


class BlockingConversations:
    def __init__(self) -> None:
        self.messaging = BlockingInbox()
        self.initialized = False

    async def initialize(self) -> None:
        self.initialized = True

    async def process(self, message_id: str) -> None:
        del message_id


class BlockingExecutionStore:
    def __init__(self) -> None:
        self.maintenance_started = asyncio.Event()
        self.cancelled = False

    async def initialize(self) -> None:
        return None

    async def recover_expired(self, *, scope: ProfileScope) -> None:
        del scope
        self.maintenance_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class BlockingDispatcher:
    def __init__(self) -> None:
        self.worker_started = asyncio.Event()
        self.worker_cancelled = False
        self.store = BlockingExecutionStore()

    async def worker_once(self, *, scope: ProfileScope) -> list[Any]:
        del scope
        self.worker_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.worker_cancelled = True
            raise
        return []

    async def project_notifications(self, *, scope: ProfileScope) -> int:
        del scope
        return 0


class CleanupProbe:
    """Inject independent shutdown failures or a cancellable cleanup phase."""

    def __init__(self, selected: set[str], mode: str) -> None:
        self.selected = selected
        self.mode = mode
        self.attempts: list[str] = []
        self.entered = asyncio.Event()

    async def run(self, phase: str) -> None:
        self.attempts.append(phase)
        if phase not in self.selected:
            return
        if self.mode == "failure":
            raise RuntimeError(f"{phase} cleanup failed")
        self.entered.set()
        await asyncio.Event().wait()


class CleanupMessaging:
    def __init__(self, probe: CleanupProbe) -> None:
        self.probe = probe

    async def aclose(self) -> None:
        await self.probe.run("runtime")


class ReacquirableLock:
    def __init__(self) -> None:
        self.held = False
        self.release_calls = 0

    def acquire(self) -> Any:
        if self.held:
            raise RuntimeError("lock is already held")
        self.held = True
        return SimpleNamespace(pid=123)

    def release(self) -> None:
        self.release_calls += 1
        self.held = False


class CleanupDrillService(GatewayService):
    def __init__(
        self,
        settings: RickySettings,
        *,
        probe: CleanupProbe,
        lock: ReacquirableLock,
        primary_failure: bool = False,
    ) -> None:
        super().__init__(
            settings,
            messaging=cast_any(CleanupMessaging(probe)),
            conversations=cast_any(BlockingConversations()),
            dispatcher=cast_any(BlockingDispatcher()),
            lock=cast_any(lock),
        )
        self.probe = probe
        self.primary_failure = primary_failure

    async def _startup_recovery(self) -> None:
        return None

    async def _prepare_loops(self) -> list[str]:
        return []

    async def _run_loops(
        self,
        stop_event: asyncio.Event,
        accounts: list[str],
    ) -> None:
        del stop_event, accounts
        if self.primary_failure:
            raise ValueError("primary service failure")

    async def _publish_lifecycle(self, run_id: str, state: str) -> None:  # type: ignore[override]
        del run_id
        if state == "stopping":
            await self.probe.run("lifecycle")

    async def _emit(
        self,
        kind: str,
        loop: str,
        summary: str,
        *,
        record_id: str | None = None,
    ) -> None:  # type: ignore[override]
        del loop, summary, record_id
        if kind == "shutdown":
            await self.probe.run("event")


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings(
        user_data_dir=str(tmp_path / "user"),
        messaging=MessagingSettings(
            telegram_accounts={
                "personal/bot": TelegramAccountSettings(
                    bot_token=SecretStr("token"),
                    allowed_sender_ids=["100"],
                    allowed_destination_ids=["200"],
                )
            }
        ),
        gateway=GatewaySettings(
            enabled=True,
            concurrency=1,
            inbox_poll_seconds=0.01,
            maintenance_seconds=0.01,
        ),
    )


async def test_service_cancellation_awaits_every_owned_loop_and_closes_transport(
    tmp_path: Path,
) -> None:
    messaging = BlockingMessaging()
    conversations = BlockingConversations()
    dispatcher = BlockingDispatcher()
    service = GatewayService(
        _settings(tmp_path),
        messaging=cast_any(messaging),
        conversations=cast_any(conversations),
        dispatcher=cast_any(dispatcher),
    )
    task = asyncio.create_task(service.run())
    await asyncio.wait_for(
        asyncio.gather(
            messaging.poll_started.wait(),
            messaging.delivery_started.wait(),
            conversations.messaging.started.wait(),
            dispatcher.worker_started.wait(),
            dispatcher.store.maintenance_started.wait(),
        ),
        timeout=2,
    )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert conversations.initialized
    assert messaging.cancelled == {"poll", "delivery"}
    assert conversations.messaging.cancelled
    assert dispatcher.worker_cancelled
    assert dispatcher.store.cancelled
    assert messaging.closed


@pytest.mark.parametrize(
    "failed_phases",
    [
        {"lifecycle"},
        {"event"},
        {"runtime"},
        {"lifecycle", "event"},
        {"lifecycle", "runtime"},
        {"event", "runtime"},
        {"lifecycle", "event", "runtime"},
    ],
    ids=[
        "lifecycle",
        "event",
        "runtime",
        "lifecycle-event",
        "lifecycle-runtime",
        "event-runtime",
        "all",
    ],
)
async def test_shutdown_failure_matrix_attempts_every_cleanup_and_releases_lock(
    tmp_path: Path,
    failed_phases: set[str],
) -> None:
    probe = CleanupProbe(failed_phases, "failure")
    lock = ReacquirableLock()
    service = CleanupDrillService(_settings(tmp_path), probe=probe, lock=lock)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await service.run()

    assert probe.attempts == ["lifecycle", "event", "runtime"]
    assert lock.release_calls == 1
    assert not lock.held
    lock.acquire()
    lock.release()


@pytest.mark.parametrize("cancelled_phase", ["lifecycle", "event", "runtime"])
async def test_shutdown_cancellation_attempts_later_cleanup_and_releases_lock(
    tmp_path: Path,
    cancelled_phase: str,
) -> None:
    probe = CleanupProbe({cancelled_phase}, "cancellation")
    lock = ReacquirableLock()
    service = CleanupDrillService(_settings(tmp_path), probe=probe, lock=lock)
    running = asyncio.create_task(service.run())
    await asyncio.wait_for(probe.entered.wait(), timeout=2)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert probe.attempts == ["lifecycle", "event", "runtime"]
    assert lock.release_calls == 1
    assert not lock.held
    lock.acquire()
    lock.release()


async def test_primary_failure_survives_all_cleanup_failures_after_lock_release(
    tmp_path: Path,
) -> None:
    probe = CleanupProbe({"lifecycle", "event", "runtime"}, "failure")
    lock = ReacquirableLock()
    service = CleanupDrillService(
        _settings(tmp_path),
        probe=probe,
        lock=lock,
        primary_failure=True,
    )

    with pytest.raises(ValueError, match="primary service failure") as excinfo:
        await service.run()

    assert len(excinfo.value.__notes__) == 3
    assert probe.attempts == ["lifecycle", "event", "runtime"]
    assert not lock.held


async def test_startup_refuses_capability_health_failure_before_loops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversations = BlockingConversations()

    async def fail_capabilities(self: GatewayHealth) -> tuple[DoctorCheck, ...]:
        del self
        return (
            DoctorCheck(
                name="capability inventory personal",
                status="fail",
                detail="tool broken_effect is missing metadata: effect_kind",
            ),
        )

    monkeypatch.setattr(GatewayHealth, "capability_checks", fail_capabilities)
    service = GatewayService(
        _settings(tmp_path),
        messaging=cast_any(BlockingMessaging()),
        conversations=cast_any(conversations),
        dispatcher=cast_any(BlockingDispatcher()),
    )

    with pytest.raises(ValueError, match="broken_effect.*effect_kind"):
        await service._prepare_loops()

    assert conversations.initialized


class BatchInbox:
    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages

    async def list_pending_oldest(self, **kwargs: object) -> list[Any]:
        del kwargs
        return self.messages


class RecordingConversations:
    def __init__(self, messages: list[Any]) -> None:
        self.messaging = BatchInbox(messages)
        self.processed: list[str] = []

    async def initialize(self) -> None:
        return None

    async def process(self, message_id: str) -> None:
        self.processed.append(message_id)


def _inbound_stub(message_id: str, *, destination: str = "200") -> Any:
    return SimpleNamespace(
        id=message_id,
        transport="telegram",
        account="personal/bot",
        destination_id=destination,
        thread_id=None,
    )


async def test_process_once_starts_oldest_first_bounded_batch(tmp_path: Path) -> None:
    messages = [
        _inbound_stub("oldest"),
        _inbound_stub("newest"),
    ]
    conversations = RecordingConversations(messages)
    service = GatewayService(
        _settings(tmp_path),
        messaging=cast_any(BlockingMessaging()),
        conversations=cast_any(conversations),
        dispatcher=cast_any(BlockingDispatcher()),
    )
    assert await service.process_once() == 2
    assert conversations.processed == ["oldest", "newest"]


async def test_gateway_notification_service_adds_conversation_correlation(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    from ricky.gateway.service import GatewayNotificationService
    from ricky.gateway.store import GatewayStore
    from ricky.gateway.types import ConversationKey
    from ricky.notifications.routes import RoutePolicy
    from ricky.notifications.types import NotificationRequest

    settings = _settings(tmp_path)
    gateway = GatewayStore(settings)
    await gateway.initialize()
    conversation = await gateway.create(
        key=ConversationKey(
            transport="telegram",
            account="personal/bot",
            destination_id="200",
        ),
        session_id="session_" + "a" * 32,
        route_name="owner",
        provider="openrouter",
        model="model",
        profile_scope=_SCOPE,
        project_root=None,
    )
    routes = RoutePolicy(settings, conversation_resolver=gateway)
    service = GatewayNotificationService(settings, routes=routes)
    record = await service.enqueue(
        NotificationRequest(
            id="notification_" + "b" * 32,
            route=f"conversation:{conversation.id}",
            body="Execution completed",
            source_kind="execution",
            profile_label=_SCOPE.label(),
            source_id="execution_" + "c" * 32,
            dedupe_key="result",
            created_at=datetime.now(UTC),
        ),
        scope=_SCOPE,
    )
    assert [(item.kind, item.id) for item in record.request.correlations] == [
        ("conversation", conversation.id)
    ]


def cast_any(value: object) -> Any:
    return value


class LifecycleMessaging:
    """Offline runtime double used to verify lifecycle notification delivery."""

    def __init__(self) -> None:
        self.delivery_attempts = 0
        self.closed = False

    async def poll_once(self, account: str) -> None:
        del account

    async def deliver_once(self) -> int:
        self.delivery_attempts += 1
        return 0

    async def aclose(self) -> None:
        self.closed = True


class LifecycleNotifications:
    def __init__(self, stop: asyncio.Event) -> None:
        self.stop = stop
        self.requests: list[Any] = []

    async def enqueue(self, request: Any, *, scope: ProfileScope) -> None:
        del scope
        self.requests.append(request)
        if request.dedupe_key == "started":
            self.stop.set()


class LifecycleConversations:
    async def initialize(self) -> None:
        return None


class LifecycleDispatcher:
    async def worker_once(self, *, scope: ProfileScope) -> list[Any]:
        del scope
        return []

    async def project_notifications(self, *, scope: ProfileScope) -> int:
        del scope
        return 0

    store = SimpleNamespace(
        initialize=lambda: _async_none(),
        recover_expired=lambda **_kwargs: _async_list(),
    )


async def _async_none() -> None:
    return None


async def _async_list() -> list[Any]:
    return []


def _lifecycle_settings(tmp_path: Path) -> RickySettings:
    base = _settings(tmp_path)
    return base.model_copy(
        update={
            "messaging": base.messaging.model_copy(
                update={
                    "transports": {
                        "owner-telegram": MessagingTransportSettings(
                            type="telegram", account="personal/bot"
                        )
                    },
                    "routes": {
                        "owner": MessagingRouteSettings(
                            transport="owner-telegram",
                            destination="200",
                            owner_profile="personal",
                            accepted_profiles=["shared", "personal"],
                        )
                    },
                }
            ),
            "gateway": base.gateway.model_copy(
                update={"operator_route": "owner", "startup_recovery": False}
            ),
        }
    )


async def test_gateway_run_enqueues_and_attempts_delivery_for_each_lifecycle(
    tmp_path: Path,
) -> None:
    """Manual runs and systemd restarts share this exact service entry point."""

    config = _lifecycle_settings(tmp_path)
    notifications: list[Any] = []
    deliveries = 0
    for _ in range(2):
        stop = asyncio.Event()
        messaging = LifecycleMessaging()
        lifecycle = LifecycleNotifications(stop)
        service = GatewayService(
            config,
            messaging=cast_any(messaging),
            conversations=cast_any(LifecycleConversations()),
            dispatcher=cast_any(LifecycleDispatcher()),
            lifecycle_notifications=cast_any(lifecycle),
        )

        await service.run(stop=stop)

        assert messaging.closed
        assert messaging.delivery_attempts == 2
        notifications.extend(lifecycle.requests)
        deliveries += messaging.delivery_attempts

    assert deliveries == 4
    assert [request.dedupe_key for request in notifications] == [
        "started",
        "stopping",
        "started",
        "stopping",
    ]
    assert {request.route for request in notifications} == {"owner"}
    assert {request.source_kind for request in notifications} == {"gateway_lifecycle"}
    assert [request.title for request in notifications] == [
        "Ricky gateway started",
        "Ricky gateway stopping",
        "Ricky gateway started",
        "Ricky gateway stopping",
    ]
    run_ids = [request.source_id for request in notifications]
    assert run_ids[0] == run_ids[1]
    assert run_ids[2] == run_ids[3]
    assert run_ids[0] != run_ids[2]
