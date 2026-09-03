"""Cancellation integration at the runtime-owned httpx send boundary."""

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
from ricky.interfaces.messaging.telegram import TelegramTransport, split_telegram_text
from ricky.messaging.runtime import MessagingRuntime
from ricky.notifications import NotificationStore
from ricky.notifications.types import NotificationRequest
from ricky.profiles import ProfileLabel, ProfileScope


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


async def _enqueue(settings: RickySettings) -> None:
    store = NotificationStore(settings)
    await store.initialize()
    await store.enqueue(
        NotificationRequest(
            id=f"notification_{uuid4().hex}",
            route="owner",
            body="hello",
            source_kind="test",
            profile_label=ProfileLabel(required_profiles=("shared", "personal")),
            source_id="source",
            dedupe_key="send-cancellation",
            created_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
        ),
        scope=ProfileScope.create("personal"),
    )


async def test_cancellation_during_send_closes_httpx_client(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    await _enqueue(settings)
    started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = settings.messaging.telegram_accounts["personal/bot"]
    adapter = TelegramTransport("personal/bot", config, client=client)
    runtime = MessagingRuntime(
        settings,
        transport_factory=lambda account: adapter,
        text_splitter=split_telegram_text,
    )
    task = asyncio.create_task(runtime.deliver_once())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pending = await NotificationStore(settings).list(
        scope=ProfileScope.create("personal"),
        status="in_doubt",
    )
    assert len(pending) == 1
    assert client.is_closed
