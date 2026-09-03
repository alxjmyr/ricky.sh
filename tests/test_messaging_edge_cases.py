"""No-replay and cancellation edge cases across the messaging boundary."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from ricky.config import (
    MessagingRouteSettings,
    MessagingSettings,
    MessagingTransportSettings,
    RickySettings,
    TelegramAccountSettings,
)
from ricky.interfaces.messaging.telegram import (
    TelegramAmbiguousDeliveryError,
    TelegramDeliveryError,
    TelegramTransport,
    split_telegram_text,
)
from ricky.messaging.runtime import MessagingRuntime
from ricky.messaging.types import DeliveryReceipt, ReceiveBatch, TransportCursor, TransportMessage
from ricky.notifications import NotificationStore
from ricky.notifications.types import NotificationRequest
from ricky.profiles import ProfileLabel, ProfileScope

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings(
        user_data_dir=str(tmp_path / "user"),
        messaging=MessagingSettings(
            telegram_accounts={
                "personal/bot": TelegramAccountSettings(
                    bot_token=SecretStr("test-token"),
                    long_poll_timeout_seconds=0,
                    allowed_sender_ids=["100"],
                    allowed_destination_ids=["200"],
                )
            },
            transports={
                "main": MessagingTransportSettings(type="telegram", account="personal/bot")
            },
            routes={
                "owner": MessagingRouteSettings(
                    transport="main",
                    destination="200",
                    owner_profile="personal",
                    accepted_profiles=["shared", "personal"],
                )
            },
        ),
    )


async def _enqueue(settings: RickySettings, body: str) -> str:
    store = NotificationStore(settings)
    await store.initialize()
    record = await store.enqueue(
        NotificationRequest(
            id=f"notification_{uuid4().hex}",
            route="owner",
            body=body,
            source_kind="test",
            profile_label=ProfileLabel(required_profiles=("shared", "personal")),
            source_id="source",
            dedupe_key=uuid4().hex,
            created_at=NOW,
        ),
        scope=ProfileScope.create("personal"),
    )
    return record.outbox.id


class PartialDeliveryTransport:
    def __init__(self) -> None:
        self.sent: list[TransportMessage] = []

    async def receive(self, cursor: TransportCursor | None) -> ReceiveBatch:
        del cursor
        return ReceiveBatch(transport="telegram", account="personal/bot")

    async def send(self, message: TransportMessage) -> DeliveryReceipt:
        self.sent.append(message)
        if message.part_number == 2:
            raise TelegramDeliveryError("Telegram rejected the outbound message")
        return DeliveryReceipt(
            transport="telegram",
            account="personal/bot",
            transport_message_id=message.id,
            platform_message_id="401",
            destination_id=message.destination_id,
            delivered_at=NOW,
        )

    async def aclose(self) -> None:
        return None


async def test_partial_multipart_delivery_is_in_doubt_and_never_replayed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    outbox_id = await _enqueue(settings, "x" * 4_001)
    transport = PartialDeliveryTransport()
    runtime = MessagingRuntime(
        settings,
        transport_factory=lambda account: transport,
        text_splitter=split_telegram_text,
    )
    assert await runtime.deliver_once() == 0
    assert len(transport.sent) == 2
    assert (
        await NotificationStore(settings).get_outbox(
            outbox_id,
            scope=ProfileScope.create("personal"),
        )
    ).status == "in_doubt"

    another = PartialDeliveryTransport()
    second = MessagingRuntime(
        settings,
        transport_factory=lambda account: another,
        text_splitter=split_telegram_text,
    )
    assert await second.deliver_once() == 0
    assert another.sent == []


async def test_runtime_cancellation_during_long_poll_closes_httpx_client(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = TelegramTransport(
        "personal/bot", settings.messaging.telegram_accounts["personal/bot"], client=client
    )
    runtime = MessagingRuntime(
        settings,
        transport_factory=lambda account: adapter,
        text_splitter=split_telegram_text,
    )
    task = asyncio.create_task(runtime.poll_once("personal/bot"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.is_closed


async def test_server_error_after_send_is_ambiguous() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, json={"ok": False, "description": "internal"})

    config = TelegramAccountSettings(
        bot_token=SecretStr("test-token"),
        api_base_url="https://telegram.invalid",
        allowed_destination_ids=["200"],
    )
    adapter = TelegramTransport(
        "personal/bot", config, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    message = TransportMessage(
        id="transport_message_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        transport="telegram",
        account="personal/bot",
        destination_id="200",
        text="hello",
        outbox_id="outbox_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        part_number=1,
        part_count=1,
    )
    with pytest.raises(TelegramAmbiguousDeliveryError, match="ambiguous"):
        await adapter.send(message)
    await adapter.aclose()
