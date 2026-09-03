"""Structured long-running owner for gateway polling, turns, dispatch, and delivery."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ricky.config import RickySettings
from ricky.executions.dispatcher import ExecutionDispatcher
from ricky.gateway.conversations import (
    ConversationCoordinator,
    GatewayNotificationService,
)
from ricky.gateway.health import GatewayHealth
from ricky.gateway.lock import GatewayLock, GatewayLockError
from ricky.gateway.recovery import GatewayRecovery
from ricky.messaging.runtime import MessagingRuntime
from ricky.messaging.types import InboundMessage
from ricky.notifications import NotificationService, gateway_lifecycle
from ricky.notifications.routes import RoutePolicy
from ricky.profiles import ProfileLabel
from ricky.protected_values import ResidentProtectedValueRegistry

ErrorSink = Callable[[str, BaseException], Awaitable[None] | None]
StartupHook = Callable[[], Awaitable[None]]

ServiceEventKind = Literal[
    "claim",
    "start",
    "success",
    "failure",
    "uncertainty",
    "recovery",
    "delivery",
    "shutdown",
]


class ServiceEvent(BaseModel):
    """One bounded typed service event.

    Events reference transcript and record ids. They never duplicate raw model
    context, tool results, or any credential.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ServiceEventKind
    loop: str = Field(min_length=1, max_length=100)
    at: datetime
    record_id: str | None = Field(default=None, max_length=500)
    summary: str = Field(min_length=1, max_length=1_000)


EventSink = Callable[[ServiceEvent], Awaitable[None] | None]


class ClosableRuntime(Protocol):
    async def aclose(self) -> None: ...


class GatewayService:
    """Own every long-running gateway loop with structured cancellation."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        messaging: MessagingRuntime,
        conversations: ConversationCoordinator | None = None,
        dispatcher: ExecutionDispatcher | None = None,
        error_sink: ErrorSink | None = None,
        event_sink: EventSink | None = None,
        lock: GatewayLock | None = None,
        recovery: GatewayRecovery | None = None,
        lifecycle_notifications: NotificationService | None = None,
        startup_hook: StartupHook | None = None,
        protected_values: ResidentProtectedValueRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.profile_scope = settings.resolve_profile_scope(
            settings.profiles.default,
            access_profiles=settings.profiles.enabled,
        )
        self.messaging = messaging
        self.conversations = conversations or ConversationCoordinator(settings)
        if dispatcher is None:
            routes = RoutePolicy(settings, conversation_resolver=self.conversations.gateway)
            dispatcher = ExecutionDispatcher(
                settings,
                routes=routes,
                notifications=GatewayNotificationService(settings, routes=routes),
                protected_value_registry=protected_values,
            )
        self.dispatcher = dispatcher
        bind_dispatcher = getattr(self.conversations, "bind_dispatcher", None)
        if bind_dispatcher is not None:
            bind_dispatcher(dispatcher)
        self.error_sink = error_sink
        self.event_sink = event_sink
        self.lock = lock or GatewayLock(settings)
        self.recovery = recovery or GatewayRecovery(settings, scope=self.profile_scope)
        self.lifecycle_notifications = lifecycle_notifications or NotificationService(settings)
        self.startup_hook = startup_hook
        self.protected_values = protected_values

    async def run(self, *, stop: asyncio.Event | None = None) -> None:
        """Run until stopped or cancelled, then close every owned transport.

        Startup order matters. The single-instance lock is taken before any
        recovery write, so two gateways can never repair the same records at
        once, and recovery completes before any loop can claim fresh work.
        """

        if not self.settings.gateway.enabled:
            raise ValueError("gateway.enabled must be true to run the gateway service")
        stop_event = stop or asyncio.Event()
        try:
            owner = self.lock.acquire()
        except GatewayLockError as exc:
            if self.protected_values is not None:
                await self.protected_values.aclose()
            raise ValueError(str(exc)) from exc
        run_id = f"gateway_run_{uuid4().hex}"
        primary_error: BaseException | None = None
        try:
            if self.startup_hook is not None:
                await self.startup_hook()
            await self._emit("start", "service", f"gateway locked by pid {owner.pid}")
            await self._startup_recovery()
            accounts = await self._prepare_loops()
            await self._publish_lifecycle(run_id, "started")
            await self._run_loops(stop_event, accounts)
        except BaseException as exc:
            primary_error = exc
        finally:
            cleanup_errors: list[BaseException] = []
            try:
                for cleanup in (
                    self._publish_lifecycle(run_id, "stopping"),
                    self._emit("shutdown", "service", "gateway stopped and released its lock"),
                    self.messaging.aclose(),
                    *(
                        (self.protected_values.aclose(),)
                        if self.protected_values is not None
                        else ()
                    ),
                ):
                    try:
                        await cleanup
                    except BaseException as exc:
                        cleanup_errors.append(exc)
            finally:
                try:
                    self.lock.release()
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if primary_error is not None:
                for cleanup_error in cleanup_errors:
                    primary_error.add_note(
                        f"gateway cleanup also failed: {type(cleanup_error).__name__}: "
                        f"{cleanup_error}"
                    )
                raise primary_error
            if cleanup_errors:
                first, *later = cleanup_errors
                for cleanup_error in later:
                    first.add_note(
                        f"later gateway cleanup failed: {type(cleanup_error).__name__}: "
                        f"{cleanup_error}"
                    )
                raise first

    async def _startup_recovery(self) -> None:
        if not self.settings.gateway.startup_recovery:
            return
        plan = await self.recovery.apply()
        for action in plan.actions:
            await self._emit(
                "recovery",
                action.subsystem,
                f"{action.from_state} -> {action.to_state}: {action.reason}",
                record_id=action.record_id,
            )
        for failure in plan.failures:
            await self._emit("failure", "recovery", failure[:1_000])

    async def _prepare_loops(self) -> list[str]:
        """Validate gateway runtime prerequisites before announcing readiness."""

        await self.conversations.initialize()
        capability_failures = [
            check
            for check in await GatewayHealth(self.settings).capability_checks()
            if check.status == "fail"
        ]
        if capability_failures:
            details = "; ".join(check.detail for check in capability_failures)
            raise ValueError(f"gateway capability validation failed: {details}")
        accounts = [
            name
            for name, config in self.settings.messaging.telegram_accounts.items()
            if config.enabled
        ]
        if not accounts:
            raise ValueError("gateway service requires at least one enabled messaging account")
        return accounts

    async def _run_loops(self, stop_event: asyncio.Event, accounts: list[str]) -> None:
        async with asyncio.TaskGroup() as group:
            for account in accounts:
                group.create_task(
                    self._supervise(
                        f"poll:{account}",
                        lambda account=account: self._poll_loop(account, stop_event),
                        stop_event,
                    )
                )
            group.create_task(
                self._supervise("inbox", lambda: self._inbox_loop(stop_event), stop_event)
            )
            group.create_task(
                self._supervise("executions", lambda: self._execution_loop(stop_event), stop_event)
            )
            group.create_task(
                self._supervise("outbox", lambda: self._outbox_loop(stop_event), stop_event)
            )
            group.create_task(
                self._supervise(
                    "maintenance",
                    lambda: self._maintenance_loop(stop_event),
                    stop_event,
                )
            )

    async def _publish_lifecycle(
        self,
        run_id: str,
        state: Literal["started", "stopping"],
    ) -> None:
        """Persist and promptly attempt a provider-neutral lifecycle update.

        The configured operator route is logical, so this behavior reaches
        Telegram today and any supported future transport without gateway code
        knowing a destination or credential. Delivery errors are observable but
        never prevent clean startup, shutdown, or lock release.
        """

        route = self.settings.gateway.operator_route
        if route is None:
            return
        configured = self.settings.messaging.routes.get(route)
        if configured is None:
            await self._emit(
                "failure",
                "lifecycle",
                "gateway.operator_route must name a configured static messaging route",
            )
            return
        try:
            await self.lifecycle_notifications.enqueue(
                gateway_lifecycle(
                    route=route,
                    run_id=run_id,
                    state=state,
                    profile_label=ProfileLabel.owned_by("shared"),
                ),
                scope=self.profile_scope,
            )
            await self.messaging.deliver_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit("failure", "lifecycle", f"{type(exc).__name__}: {exc}")

    async def process_once(self) -> int:
        """Process one oldest-first bounded pending-inbox batch."""

        await self.conversations.initialize()
        messages = await self.conversations.messaging.list_pending_oldest(
            limit=self.settings.gateway.concurrency,
        )
        if not messages:
            return 0
        await self._process_messages(messages)
        return len(messages)

    async def _process_messages(self, messages: list[InboundMessage]) -> None:
        """Run each conversation oldest-first while distinct keys run concurrently."""

        grouped: dict[tuple[str, str, str, str | None], list[InboundMessage]] = {}
        for message in messages:
            key = (
                message.transport,
                message.account,
                message.destination_id,
                message.thread_id,
            )
            grouped.setdefault(key, []).append(message)
        await asyncio.gather(*(self._process_conversation(group) for group in grouped.values()))

    async def _emit(
        self,
        kind: ServiceEventKind,
        loop: str,
        summary: str,
        *,
        record_id: str | None = None,
    ) -> None:
        """Publish one bounded typed event. A sink failure never stops a loop."""

        if self.event_sink is None:
            return
        event = ServiceEvent(
            kind=kind,
            loop=loop,
            at=datetime.now(UTC),
            record_id=record_id,
            summary=summary[:1_000],
        )
        with suppress(Exception):
            result = self.event_sink(event)
            if result is not None:
                await result

    async def _process_conversation(self, messages: list[InboundMessage]) -> None:
        for message in messages:
            await self._emit("claim", "inbox", "claiming inbound message", record_id=message.id)
            result = await self.conversations.process(message.id)
            # A coordinator double may return nothing; the event stream is
            # observability, so an absent result must never fail the turn.
            status = getattr(result, "status", None) or "unknown"
            kind: ServiceEventKind = "uncertainty" if status == "uncertain" else "success"
            await self._emit(
                kind,
                "inbox",
                f"foreground turn finished as {status}",
                record_id=message.id,
            )

    async def _poll_loop(self, account: str, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.messaging.poll_once(account)

    async def _inbox_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            messages = await self.conversations.messaging.list_pending_oldest(
                limit=self.settings.gateway.concurrency,
            )
            if messages:
                await self._process_messages(messages)
                progressed = False
                for message in messages:
                    current = await self.conversations.messaging.get_inbox(message.id)
                    if current.status != "pending":
                        progressed = True
                        break
                if progressed:
                    continue
            await _wait(stop, self.settings.gateway.inbox_poll_seconds)

    async def _execution_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            completed = await self.dispatcher.worker_once(scope=self.profile_scope)
            if not completed:
                await _wait(stop, self.settings.executions.poll_seconds)

    async def _outbox_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            delivered = await self.messaging.deliver_once()
            if delivered:
                await self._emit("delivery", "outbox", f"delivered {delivered} notification(s)")
            else:
                await _wait(stop, self.settings.gateway.inbox_poll_seconds)

    async def _maintenance_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.dispatcher.store.initialize()
            expire_browser_approvals = getattr(
                self.dispatcher.store, "expire_browser_approvals", None
            )
            if expire_browser_approvals is not None:
                await expire_browser_approvals(scope=self.profile_scope)
            await self.dispatcher.store.recover_expired(scope=self.profile_scope)
            recover_browser_attempts = getattr(self.recovery, "recover_browser_attempts", None)
            if recover_browser_attempts is not None:
                await recover_browser_attempts()
            await self.dispatcher.project_notifications(scope=self.profile_scope)
            await _wait(stop, self.settings.gateway.maintenance_seconds)

    async def _supervise(
        self,
        name: str,
        operation: Callable[[], Awaitable[None]],
        stop: asyncio.Event,
    ) -> None:
        while not stop.is_set():
            try:
                await operation()
                return
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                await self._emit("failure", name, f"{type(exc).__name__}: {exc}")
                if self.error_sink is not None:
                    result = self.error_sink(name, exc)
                    if result is not None:
                        await result
                await _wait(stop, min(5.0, self.settings.gateway.inbox_poll_seconds))


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    with suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
