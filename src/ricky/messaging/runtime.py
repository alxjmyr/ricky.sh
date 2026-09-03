"""Platform-neutral inbox polling and durable outbox delivery orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import Protocol
from uuid import uuid4

from ricky.config import MessagingRouteSettings, RickySettings, TelegramAccountSettings
from ricky.messaging.errors import AmbiguousDeliveryError, DeliveryNotPerformedError
from ricky.messaging.markdown import compose_notification_text
from ricky.messaging.store import MessagingStore
from ricky.messaging.types import InboundMessage, MessageTransport, TransportMessage
from ricky.notifications.routes import ResolvedRoute, RouteError, RoutePolicy
from ricky.notifications.store import (
    NotificationLeaseError,
    NotificationStateError,
    NotificationStore,
)
from ricky.notifications.types import MessageTextFormat, NotificationRequest, OutboxEntry
from ricky.owned_operation import run_with_lease_heartbeat
from ricky.profiles import ProfileLabel

TransportFactory = Callable[[str], MessageTransport]


class PartSplitter(Protocol):
    """Split text while selecting its portable presentation format by name."""

    def __call__(
        self,
        text: str,
        *,
        text_format: MessageTextFormat,
    ) -> list[str]: ...


class MessagingRuntimeError(RuntimeError):
    """A messaging runtime configuration or delivery operation failed."""


class MessagingRuntime:
    """Own short transport resources while stores retain all durable state."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        store: MessagingStore | None = None,
        notifications: NotificationStore | None = None,
        routes: RoutePolicy | None = None,
        transport_factory: TransportFactory,
        text_splitter: PartSplitter,
    ) -> None:
        self.settings = settings
        self.store = store or MessagingStore(settings)
        self.notifications = notifications or NotificationStore(settings)
        self.routes = routes or RoutePolicy(settings)
        self.profile_scope = settings.resolve_profile_scope(
            settings.profiles.default,
            access_profiles=settings.profiles.enabled,
        )
        self._factory = transport_factory
        self._text_splitter = text_splitter

    async def poll_once(self, account: str) -> list[InboundMessage]:
        config = self._telegram_account(account)
        await self.store.initialize()
        lease = await self.store.acquire_poller(
            "telegram",
            account,
            owner=f"gateway_{uuid4().hex}",
            lease_seconds=max(10, config.long_poll_timeout_seconds + 10),
        )
        transport: MessageTransport | None = None
        try:
            cursor = await self.store.cursor("telegram", account)
            transport = self._factory(account)
            batch = await transport.receive(cursor)
            return await self.store.ingest(batch)
        finally:
            try:
                if transport is not None:
                    await transport.aclose()
            finally:
                await self.store.release_poller(lease)

    async def enqueue_reply(self, message_id: str, text: str) -> str:
        """Durably enqueue a reply using only the trusted stored inbound route."""

        await self.store.initialize()
        inbound = await self.store.get_inbox(message_id)
        if inbound.status == "rejected":
            raise MessagingRuntimeError("cannot reply to a rejected inbound update")
        body = text.strip()
        if not body:
            raise ValueError("reply text cannot be empty")
        if len(body) > self.settings.messaging.body_char_limit:
            raise ValueError(
                f"reply text exceeds {self.settings.messaging.body_char_limit} characters"
            )
        configured_route = self._route_for_inbound(inbound)
        request = NotificationRequest(
            id=f"notification_{uuid4().hex}",
            route=f"inbox:{inbound.id}",
            body=body,
            urgency="normal",
            source_kind="inbox_reply",
            profile_label=ProfileLabel(required_profiles=tuple(configured_route.accepted_profiles)),
            source_id=inbound.id,
            dedupe_key=uuid4().hex,
            correlations=[],
            created_at=inbound.received_at,
        )
        await self.notifications.initialize()
        record = await self.notifications.enqueue(request, scope=self.profile_scope)
        return record.outbox.id

    async def deliver_once(self, *, limit: int = 50) -> int:
        await self.store.initialize()
        await self.notifications.initialize()
        records = await self.notifications.list_pending_oldest(
            scope=self.profile_scope,
            limit=limit,
        )
        delivered = 0
        transports: dict[str, MessageTransport] = {}
        try:
            for record in records:
                try:
                    route, reply_id = await self._resolve_route(
                        record.request.route, record.request.profile_label
                    )
                    if route.transport != "telegram":
                        raise MessagingRuntimeError(
                            f"unsupported messaging transport: {route.transport}"
                        )
                    account = self._telegram_account(route.account)
                    if route.destination_ref not in account.allowed_destination_ids:
                        raise MessagingRuntimeError(
                            "resolved Telegram destination is outside the account allowlist"
                        )
                except (MessagingRuntimeError, RouteError, KeyError, ValueError) as exc:
                    await self.notifications.fail_pending(
                        record.outbox.id,
                        scope=self.profile_scope,
                        error=f"pre-send route/configuration failure: {exc}",
                    )
                    continue
                try:
                    text = compose_notification_text(
                        title=record.request.title,
                        body=record.request.body,
                        text_format=record.request.body_format,
                    )
                    parts = self._text_splitter(
                        text,
                        text_format=record.request.body_format,
                    )
                    part_count = len(parts) + len(record.request.attachments)
                    messages = [
                        TransportMessage(
                            id=f"transport_message_{uuid4().hex}",
                            transport="telegram",
                            account=route.account,
                            destination_id=route.destination_ref,
                            text=part,
                            text_format=record.request.body_format,
                            outbox_id=record.outbox.id,
                            part_number=index,
                            part_count=part_count,
                            reply_to_platform_message_id=reply_id,
                        )
                        for index, part in enumerate(parts, start=1)
                    ]
                    messages.extend(
                        TransportMessage(
                            id=f"transport_message_{uuid4().hex}",
                            transport="telegram",
                            account=route.account,
                            destination_id=route.destination_ref,
                            attachment=attachment,
                            outbox_id=record.outbox.id,
                            part_number=len(parts) + index,
                            part_count=part_count,
                            reply_to_platform_message_id=reply_id,
                        )
                        for index, attachment in enumerate(
                            record.request.attachments,
                            start=1,
                        )
                    )
                except ValueError as exc:
                    await self.notifications.fail_pending(
                        record.outbox.id,
                        scope=self.profile_scope,
                        error=f"pre-send message preparation failure: {exc}",
                    )
                    continue
                try:
                    claimed = await self.notifications.claim(
                        record.outbox.id,
                        scope=self.profile_scope,
                        worker=f"gateway_{uuid4().hex}",
                        transport=route.transport,
                        destination_ref=route.destination_ref,
                    )
                except NotificationLeaseError:
                    continue
                except NotificationStateError as exc:
                    current = await self.notifications.get_outbox(
                        record.outbox.id,
                        scope=self.profile_scope,
                    )
                    if current.status == "pending":
                        await self.notifications.fail_pending(
                            record.outbox.id,
                            scope=self.profile_scope,
                            error=f"pre-send claim failure: {exc}",
                        )
                    continue
                transport = transports.get(route.account)
                if transport is None:
                    try:
                        transport = self._factory(route.account)
                    except Exception as exc:  # noqa: BLE001 - no send was attempted.
                        await self.notifications.mark_failed(
                            claimed,
                            scope=self.profile_scope,
                            error=str(exc),
                        )
                        continue
                    transports[route.account] = transport
                if await run_with_lease_heartbeat(
                    self._deliver_claimed(claimed, messages, transport),
                    lease=claimed,
                    renew=lambda entry: self.notifications.renew(
                        entry,
                        scope=self.profile_scope,
                    ),
                    interval_seconds=max(0.05, self.settings.messaging.lease_seconds / 3),
                ):
                    delivered += 1
        finally:
            if transports:
                await asyncio.gather(*(transport.aclose() for transport in transports.values()))
        return delivered

    async def run_poll_loop(
        self,
        account: str,
        *,
        stop: asyncio.Event,
        idle_seconds: float = 0.1,
    ) -> None:
        while not stop.is_set():
            await self.poll_once(account)
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=idle_seconds)

    async def run_delivery_loop(
        self,
        *,
        stop: asyncio.Event,
        idle_seconds: float = 1.0,
    ) -> None:
        while not stop.is_set():
            await self.deliver_once()
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=idle_seconds)

    async def aclose(self) -> None:
        """Compatibility no-op; each bounded operation owns its transport."""

    async def _deliver_claimed(
        self,
        claimed: OutboxEntry,
        messages: list[TransportMessage],
        transport: MessageTransport,
    ) -> bool:
        """Prepare, send, and settle one claimed notification under one owner."""

        try:
            await self.store.prepare_parts(claimed, messages)
        except BaseException:
            await self.notifications.release(claimed, scope=self.profile_scope)
            raise
        last_receipt = None
        for message in messages:
            try:
                receipt = await transport.send(message)
            except asyncio.CancelledError:
                error = "delivery was cancelled after send began; status is ambiguous"
                await self.store.mark_part_in_doubt(claimed, message.id, error=error)
                await self.notifications.mark_in_doubt(
                    claimed,
                    scope=self.profile_scope,
                    error=error,
                )
                raise
            except AmbiguousDeliveryError as exc:
                error = str(exc)
                await self.store.mark_part_in_doubt(claimed, message.id, error=error)
                await self.notifications.mark_in_doubt(
                    claimed,
                    scope=self.profile_scope,
                    error=error,
                )
                return False
            except DeliveryNotPerformedError as exc:
                if last_receipt is None:
                    await self.notifications.mark_failed(
                        claimed,
                        scope=self.profile_scope,
                        error=str(exc),
                    )
                else:
                    error = (
                        "multipart delivery partially completed; remaining delivery "
                        "must be reconciled before retry"
                    )
                    await self.store.mark_part_in_doubt(claimed, message.id, error=error)
                    await self.notifications.mark_in_doubt(
                        claimed,
                        scope=self.profile_scope,
                        error=error,
                    )
                return False
            try:
                await self.store.record_receipt(claimed, receipt)
            except asyncio.CancelledError:
                # Provider success is already observable. If ownership is lost
                # while its receipt is being committed, never leave recovery
                # free to classify the part as replay-safe merely because the
                # receipt transaction had not returned to this task yet.
                error = (
                    "delivery was cancelled while recording a provider receipt; status is ambiguous"
                )
                with suppress(Exception):
                    parts = await self.store.delivery_parts(claimed.id)
                    current = next(
                        (part for part in parts if part.message.id == message.id),
                        None,
                    )
                    if current is not None and current.status == "pending":
                        await self.store.mark_part_in_doubt(
                            claimed,
                            message.id,
                            error=error,
                        )
                with suppress(Exception):
                    await self.notifications.mark_in_doubt(
                        claimed,
                        scope=self.profile_scope,
                        error=error,
                    )
                raise
            last_receipt = receipt
        assert last_receipt is not None
        await self.notifications.mark_delivered(
            claimed,
            scope=self.profile_scope,
            platform_message_id=last_receipt.platform_message_id,
        )
        return True

    async def _resolve_route(
        self,
        route_name: str,
        profile_label: ProfileLabel,
    ) -> tuple[ResolvedRoute, str | None]:
        if route_name.startswith("inbox:"):
            message_id = route_name.removeprefix("inbox:")
            inbound = await self.store.get_inbox(message_id)
            if inbound.status == "rejected":
                raise MessagingRuntimeError("rejected inbound updates are not trusted routes")
            configured_route = self._route_for_inbound(inbound)
            rejected = sorted(
                set(profile_label.required_profiles) - set(configured_route.accepted_profiles)
            )
            if rejected:
                raise MessagingRuntimeError(
                    "inbox route rejects required notification profile(s): " + ", ".join(rejected)
                )
            return (
                ResolvedRoute(
                    route=route_name,
                    transport=inbound.transport,
                    account=inbound.account,
                    destination_ref=inbound.destination_id,
                    owner_profile=configured_route.owner_profile,
                    accepted_profiles=list(configured_route.accepted_profiles),
                ),
                inbound.platform_message_id,
            )
        return await self.routes.resolve(route_name, profile_label), None

    def _route_for_inbound(self, inbound: InboundMessage) -> MessagingRouteSettings:
        matches: list[MessagingRouteSettings] = []
        for route in self.settings.messaging.routes.values():
            transport = self.settings.messaging.transports[route.transport]
            if (
                transport.type == inbound.transport
                and transport.account == inbound.account
                and route.destination == inbound.destination_id
            ):
                matches.append(route)
        if len(matches) != 1:
            raise MessagingRuntimeError(
                "trusted inbox route must match exactly one configured messaging route"
            )
        return matches[0]

    def _telegram_account(self, account: str) -> TelegramAccountSettings:
        config = self.settings.messaging.telegram_accounts.get(account)
        if config is None:
            raise MessagingRuntimeError(f"unknown Telegram account: {account}")
        if not config.enabled:
            raise MessagingRuntimeError(f"Telegram account {account!r} is disabled")
        return config
