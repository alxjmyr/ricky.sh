"""Provider-free messaging runtime, delivery, ambiguity, and cancellation tests."""

import asyncio
import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr

from ricky.attachments import StoredAttachment
from ricky.config import (
    MessagingRouteSettings,
    MessagingSettings,
    MessagingTransportSettings,
    RickySettings,
    TelegramAccountSettings,
)
from ricky.interfaces.messaging.telegram import (
    TelegramAmbiguousDeliveryError,
    split_telegram_text,
)
from ricky.messaging import MessagingStore
from ricky.messaging.runtime import MessagingRuntime
from ricky.messaging.types import (
    DeliveryPart,
    DeliveryReceipt,
    InboundMessage,
    ReceiveBatch,
    ReceivedUpdate,
    TransportCursor,
    TransportMessage,
)
from ricky.notifications import NotificationStore
from ricky.notifications.store import NotificationLeaseError
from ricky.notifications.types import MessageTextFormat, NotificationRequest, OutboxEntry
from ricky.profiles import ProfileLabel, ProfileScope

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)
SCOPE = ProfileScope.create("personal")


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings(
        user_data_dir=str(tmp_path / "user"),
        messaging=MessagingSettings(
            body_char_limit=20_000,
            telegram_accounts={
                "personal/bot": TelegramAccountSettings(
                    bot_token=SecretStr("test-token"),
                    long_poll_timeout_seconds=0,
                    allowed_sender_ids=["100"],
                    allowed_destination_ids=["200"],
                    max_inbound_text_length=20_000,
                )
            },
            transports={
                "owner-telegram": MessagingTransportSettings(
                    type="telegram", account="personal/bot"
                )
            },
            routes={
                "owner": MessagingRouteSettings(
                    transport="owner-telegram",
                    destination="200",
                    owner_profile="personal",
                    accepted_profiles=["shared", "personal"],
                )
            },
        ),
    )


def _batch() -> ReceiveBatch:
    message = InboundMessage(
        id="inbound_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        transport="telegram",
        account="personal/bot",
        update_id="1",
        destination_id="200",
        sender_id="100",
        platform_message_id="300",
        text="hello",
        received_at=NOW,
        status="pending",
    )
    return ReceiveBatch(
        transport="telegram",
        account="personal/bot",
        updates=[ReceivedUpdate(update_id="1", message=message)],
        next_cursor=TransportCursor(transport="telegram", account="personal/bot", value="1"),
    )


class MockTransport:
    """Protocol test double proving the runtime has no network dependency."""

    def __init__(self, *, batch: ReceiveBatch | None = None, ambiguous: bool = False) -> None:
        self.batch = batch or ReceiveBatch(transport="telegram", account="personal/bot")
        self.ambiguous = ambiguous
        self.sent: list[TransportMessage] = []
        self.closed = False

    async def receive(self, cursor: TransportCursor | None) -> ReceiveBatch:
        del cursor
        return self.batch

    async def send(self, message: TransportMessage) -> DeliveryReceipt:
        self.sent.append(message)
        if self.ambiguous:
            raise TelegramAmbiguousDeliveryError("delivery status is ambiguous")
        return DeliveryReceipt(
            transport="telegram",
            account="personal/bot",
            transport_message_id=message.id,
            platform_message_id=str(400 + message.part_number),
            destination_id=message.destination_id,
            delivered_at=NOW,
        )

    async def aclose(self) -> None:
        self.closed = True


class BlockingTransport(MockTransport):
    def __init__(self, *, block_send: bool) -> None:
        super().__init__()
        self.block_send = block_send
        self.started = asyncio.Event()

    async def receive(self, cursor: TransportCursor | None) -> ReceiveBatch:
        del cursor
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(self, message: TransportMessage) -> DeliveryReceipt:
        if not self.block_send:
            return await super().send(message)
        self.sent.append(message)
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class DeliveryPhaseStore(MessagingStore):
    """Block one durable preparation/receipt phase until ownership is cancelled."""

    def __init__(self, settings: RickySettings, phase: str) -> None:
        super().__init__(settings)
        self.phase = phase
        self.entered = asyncio.Event()
        self.exited = asyncio.Event()

    async def _block(self) -> None:
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.exited.set()

    async def prepare_parts(
        self,
        entry: OutboxEntry,
        messages: Sequence[TransportMessage],
    ) -> list[DeliveryPart]:
        if self.phase == "prepare":
            await self._block()
        return await super().prepare_parts(entry, messages)

    async def record_receipt(
        self,
        entry: OutboxEntry,
        receipt: DeliveryReceipt,
    ) -> DeliveryPart:
        if self.phase == "receipt":
            await self._block()
        return await super().record_receipt(entry, receipt)


class RenewalFailureNotifications(NotificationStore):
    def __init__(self, settings: RickySettings) -> None:
        super().__init__(settings)
        self.renewals = 0

    async def renew(
        self,
        entry: OutboxEntry,
        *,
        scope: ProfileScope,
    ) -> OutboxEntry:
        del entry, scope
        self.renewals += 1
        raise NotificationLeaseError("injected delivery renewal failure")


class DeliveryPhaseTransport(MockTransport):
    def __init__(self, phase: str) -> None:
        super().__init__()
        self.phase = phase
        self.entered = asyncio.Event()
        self.exited = asyncio.Event()

    async def send(self, message: TransportMessage) -> DeliveryReceipt:
        if self.phase != "send":
            return await super().send(message)
        self.sent.append(message)
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.exited.set()
        raise AssertionError("unreachable")


async def _enqueue(
    settings: RickySettings,
    body: str,
    *,
    body_format: MessageTextFormat = "plain_text",
    title: str | None = None,
    created_at: datetime = NOW,
) -> str:
    store = NotificationStore(settings)
    await store.initialize()
    record = await store.enqueue(
        NotificationRequest(
            id=f"notification_{uuid4().hex}",
            route="owner",
            title=title,
            body=body,
            body_format=body_format,
            urgency="normal",
            source_kind="test",
            profile_label=ProfileLabel(required_profiles=("shared", "personal")),
            source_id="source",
            dedupe_key=uuid4().hex,
            correlations=[],
            created_at=created_at,
        ),
        scope=SCOPE,
    )
    return record.outbox.id


async def _enqueue_attachment(settings: RickySettings) -> str:
    content = b"lease-protected-attachment"
    relative = "notifications/attachments/lease/report.bin"
    path = Path(settings.user_data_dir) / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    store = NotificationStore(settings)
    await store.initialize()
    record = await store.enqueue(
        NotificationRequest(
            id=f"notification_{uuid4().hex}",
            route="owner",
            body="Attached.",
            urgency="normal",
            source_kind="test",
            profile_label=ProfileLabel(required_profiles=("shared", "personal")),
            source_id="source",
            dedupe_key=uuid4().hex,
            correlations=[],
            attachments=[
                StoredAttachment(
                    storage_path=relative,
                    filename="report.bin",
                    media_type="application/octet-stream",
                    size_bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                )
            ],
            created_at=NOW,
        ),
        scope=SCOPE,
    )
    return record.outbox.id


@pytest.mark.parametrize(
    ("phase", "expected_status"),
    [
        ("prepare", "pending"),
        ("send", "in_doubt"),
        ("receipt", "in_doubt"),
    ],
)
async def test_delivery_renewal_failure_cancels_and_joins_every_owned_phase(
    tmp_path: Path,
    phase: str,
    expected_status: str,
) -> None:
    base = _settings(tmp_path)
    settings = base.model_copy(
        update={"messaging": base.messaging.model_copy(update={"lease_seconds": 1})}
    )
    outbox_id = await _enqueue_attachment(settings)
    delivery_store = DeliveryPhaseStore(settings, phase)
    notifications = RenewalFailureNotifications(settings)
    transport = DeliveryPhaseTransport(phase)
    runtime = MessagingRuntime(
        settings,
        store=delivery_store,
        notifications=notifications,
        transport_factory=lambda _account: transport,
        # Make the attachment the only provider-send part in this drill.
        text_splitter=lambda text, *, text_format: [],
    )
    delivered = asyncio.create_task(runtime.deliver_once())
    entered = transport.entered if phase == "send" else delivery_store.entered
    exited = transport.exited if phase == "send" else delivery_store.exited
    await asyncio.wait_for(entered.wait(), timeout=2)

    with pytest.raises(NotificationLeaseError, match="renewal failure"):
        await asyncio.wait_for(delivered, timeout=2)

    assert notifications.renewals == 1
    assert exited.is_set()
    assert transport.closed
    assert (
        await NotificationStore(settings).get_outbox(outbox_id, scope=SCOPE)
    ).status == expected_status
    assert all(
        part.status != "delivered"
        for part in await MessagingStore(settings).delivery_parts(outbox_id)
    )


async def test_mock_transport_polls_without_network_and_persists_before_cursor(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    transport = MockTransport(batch=_batch())
    runtime = MessagingRuntime(
        settings, transport_factory=lambda account: transport, text_splitter=split_telegram_text
    )
    messages = await runtime.poll_once("personal/bot")

    assert [message.id for message in messages] == ["inbound_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
    store = MessagingStore(settings)
    assert (await store.cursor("telegram", "personal/bot")).value == "1"  # type: ignore[union-attr]
    assert len(await store.list_inbox(status="pending")) == 1
    assert transport.closed


async def test_confirmed_multipart_delivery_persists_every_receipt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    outbox_id = await _enqueue(settings, "x" * 8_005)
    transport = MockTransport()
    runtime = MessagingRuntime(
        settings, transport_factory=lambda account: transport, text_splitter=split_telegram_text
    )

    assert await runtime.deliver_once() == 1
    assert [len(item.text) for item in transport.sent] == [4_000, 4_000, 5]
    parts = await MessagingStore(settings).delivery_parts(outbox_id)
    assert [part.status for part in parts] == ["delivered", "delivered", "delivered"]
    assert [part.platform_message_id for part in parts] == ["401", "402", "403"]
    assert (
        await NotificationStore(settings).get_outbox(outbox_id, scope=SCOPE)
    ).status == "delivered"


async def test_portable_markdown_format_survives_durable_part_preparation(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    outbox_id = await _enqueue(
        settings,
        "**Done.**\n\n- First\n- Second",
        body_format="portable_markdown_v1",
        title="Daily *summary*",
    )
    transport = MockTransport()
    runtime = MessagingRuntime(
        settings,
        transport_factory=lambda account: transport,
        text_splitter=split_telegram_text,
    )

    assert await runtime.deliver_once() == 1
    [sent] = transport.sent
    assert sent.text_format == "portable_markdown_v1"
    assert sent.text == "## Daily \\*summary\\*\n\n**Done.**\n\n- First\n- Second"
    [part] = await MessagingStore(settings).delivery_parts(outbox_id)
    assert part.message == sent


async def test_rich_preparation_failure_is_quarantined_before_claiming(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    bad_outbox_id = await _enqueue(
        settings,
        "```" + "x" * 3_995 + "\n```",
        body_format="portable_markdown_v1",
    )
    good_outbox_id = await _enqueue(
        settings,
        "good",
        created_at=NOW + timedelta(seconds=1),
    )
    transport = MockTransport()
    runtime = MessagingRuntime(
        settings,
        transport_factory=lambda account: transport,
        text_splitter=split_telegram_text,
    )

    assert await runtime.deliver_once() == 1
    notifications = NotificationStore(settings)
    failed = await notifications.get_outbox(bad_outbox_id, scope=SCOPE)
    assert failed.status == "failed"
    assert failed.attempt_count == 0
    assert failed.error == (
        "pre-send message preparation failure: "
        "message split limit is too small for a Markdown code fence"
    )
    assert await notifications.attempts(bad_outbox_id, scope=SCOPE) == []
    assert (await notifications.get_outbox(good_outbox_id, scope=SCOPE)).status == "delivered"
    assert [message.text for message in transport.sent] == ["good"]


async def test_poll_and_delivery_own_independent_transports(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    await _enqueue(settings, "hello")
    polling = BlockingTransport(block_send=False)
    delivery = MockTransport()
    created: list[MockTransport] = []

    def factory(account: str) -> MockTransport:
        del account
        transport = polling if not created else delivery
        created.append(transport)
        return transport

    runtime = MessagingRuntime(
        settings,
        transport_factory=factory,
        text_splitter=split_telegram_text,
    )
    poll_task = asyncio.create_task(runtime.poll_once("personal/bot"))
    await polling.started.wait()

    assert await runtime.deliver_once() == 1
    assert delivery.closed
    assert not polling.closed

    poll_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await poll_task
    assert polling.closed


async def test_empty_delivery_pass_constructs_no_transport(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    constructed = 0

    def factory(account: str) -> MockTransport:
        nonlocal constructed
        del account
        constructed += 1
        return MockTransport()

    runtime = MessagingRuntime(
        settings,
        transport_factory=factory,
        text_splitter=split_telegram_text,
    )

    assert await runtime.deliver_once() == 0
    assert constructed == 0


async def test_delivery_adds_durable_attachment_parts_after_text(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    content = b"report"
    relative = "notifications/attachments/n/report.txt"
    path = Path(settings.user_data_dir) / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    store = NotificationStore(settings)
    await store.initialize()
    record = await store.enqueue(
        NotificationRequest(
            id=f"notification_{uuid4().hex}",
            route="owner",
            body="Here it is.",
            urgency="normal",
            source_kind="test",
            profile_label=ProfileLabel(required_profiles=("shared", "personal")),
            source_id="source",
            dedupe_key=uuid4().hex,
            correlations=[],
            attachments=[
                StoredAttachment(
                    storage_path=relative,
                    filename="report.txt",
                    media_type="text/plain",
                    size_bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                )
            ],
            created_at=NOW,
        ),
        scope=SCOPE,
    )
    transport = MockTransport()
    runtime = MessagingRuntime(
        settings,
        transport_factory=lambda account: transport,
        text_splitter=split_telegram_text,
    )

    assert await runtime.deliver_once() == 1
    assert [message.part_number for message in transport.sent] == [1, 2]
    assert transport.sent[0].text == "Here it is."
    assert transport.sent[1].attachment is not None
    assert transport.sent[1].attachment.filename == "report.txt"
    assert (await store.get_by_outbox(record.outbox.id, scope=SCOPE)).outbox.status == "delivered"


async def test_ambiguous_send_becomes_in_doubt_and_is_not_retried(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    outbox_id = await _enqueue(settings, "hello")
    transport = MockTransport(ambiguous=True)
    runtime = MessagingRuntime(
        settings, transport_factory=lambda account: transport, text_splitter=split_telegram_text
    )

    assert await runtime.deliver_once() == 0
    assert (
        await NotificationStore(settings).get_outbox(outbox_id, scope=SCOPE)
    ).status == "in_doubt"
    assert (await MessagingStore(settings).delivery_parts(outbox_id))[0].status == "in_doubt"

    second = MockTransport()
    assert (
        await MessagingRuntime(
            settings, transport_factory=lambda account: second, text_splitter=split_telegram_text
        ).deliver_once()
        == 0
    )
    assert second.sent == []


async def test_bad_route_is_quarantined_without_blocking_later_delivery(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = NotificationStore(settings)
    await store.initialize()
    bad = await store.enqueue(
        NotificationRequest(
            id=f"notification_{uuid4().hex}",
            route="removed-route",
            body="bad",
            urgency="normal",
            source_kind="test",
            profile_label=ProfileLabel(required_profiles=("shared", "personal")),
            source_id="bad",
            dedupe_key=uuid4().hex,
            correlations=[],
            created_at=NOW,
        ),
        scope=SCOPE,
    )
    good = await store.enqueue(
        NotificationRequest(
            id=f"notification_{uuid4().hex}",
            route="owner",
            body="good",
            urgency="normal",
            source_kind="test",
            profile_label=ProfileLabel(required_profiles=("shared", "personal")),
            source_id="good",
            dedupe_key=uuid4().hex,
            correlations=[],
            created_at=NOW + timedelta(seconds=1),
        ),
        scope=SCOPE,
    )
    transport = MockTransport()
    runtime = MessagingRuntime(
        settings,
        transport_factory=lambda account: transport,
        text_splitter=split_telegram_text,
    )

    assert await runtime.deliver_once() == 1
    assert (await store.get_outbox(bad.outbox.id, scope=SCOPE)).status == "failed"
    assert (await store.get_outbox(good.outbox.id, scope=SCOPE)).status == "delivered"
    assert [message.text for message in transport.sent] == ["good"]


@pytest.mark.parametrize("phase", ["poll", "send"])
async def test_cancellation_closes_owned_transport(tmp_path: Path, phase: str) -> None:
    settings = _settings(tmp_path)
    transport = BlockingTransport(block_send=phase == "send")
    runtime = MessagingRuntime(
        settings, transport_factory=lambda account: transport, text_splitter=split_telegram_text
    )
    if phase == "poll":
        task = asyncio.create_task(runtime.poll_once("personal/bot"))
    else:
        await _enqueue(settings, "hello")
        task = asyncio.create_task(runtime.deliver_once())
    await transport.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.closed


async def test_reply_route_comes_only_from_trusted_inbox_record(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = MessagingStore(settings)
    await store.initialize()
    await store.ingest(_batch())
    transport = MockTransport()
    runtime = MessagingRuntime(
        settings, transport_factory=lambda account: transport, text_splitter=split_telegram_text
    )
    outbox_id = await runtime.enqueue_reply("inbound_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "reply")

    assert await runtime.deliver_once() == 1
    assert transport.sent[0].destination_id == "200"
    assert transport.sent[0].reply_to_platform_message_id == "300"
    assert (
        await NotificationStore(settings).get_outbox(outbox_id, scope=SCOPE)
    ).status == "delivered"
