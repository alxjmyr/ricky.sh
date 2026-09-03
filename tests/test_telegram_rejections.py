"""Exhaustive pre-processing rejection tests for Telegram inbound trust rules."""

from datetime import UTC, datetime

import httpx
from pydantic import SecretStr

from ricky.config import TelegramAccountSettings
from ricky.interfaces.messaging.telegram import TelegramTransport


async def test_bad_destination_content_length_and_ids_are_all_rejected() -> None:
    updates = [
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "from": {"id": 100},
                "chat": {"id": 999},
                "text": "wrong destination",
            },
        },
        {
            "update_id": 2,
            "message": {
                "message_id": 20,
                "from": {"id": 100},
                "chat": {"id": 200},
                "photo": [],
            },
        },
        {
            "update_id": 3,
            "message": {
                "message_id": 30,
                "from": {"id": 100},
                "chat": {"id": 200},
                "text": "   ",
            },
        },
        {
            "update_id": 4,
            "message": {
                "message_id": 40,
                "from": {"id": 100},
                "chat": {"id": 200},
                "text": "too long",
            },
        },
        {
            "update_id": 5,
            "message": {
                "message_id": "malformed",
                "from": {"id": 100},
                "chat": {"id": 200},
                "text": "valid",
            },
        },
        {
            "update_id": 6,
            "message": {
                "message_id": 60,
                "message_thread_id": "malformed",
                "from": {"id": 100},
                "chat": {"id": 200},
                "text": "valid",
            },
        },
        {
            "update_id": 7,
            "message": {
                "message_id": 70,
                "from": {"id": 100},
                "chat": {"id": 200},
                "text": "valid",
                "reply_to_message": {"message_id": "malformed"},
            },
        },
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"ok": True, "result": updates})

    settings = TelegramAccountSettings(
        bot_token=SecretStr("test-token"),
        api_base_url="https://telegram.invalid",
        long_poll_timeout_seconds=0,
        allowed_sender_ids=["100"],
        allowed_destination_ids=["200"],
        max_inbound_text_length=5,
    )
    transport = TelegramTransport(
        "personal",
        settings,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=lambda: datetime(2026, 8, 11, 12, tzinfo=UTC),
    )
    batch = await transport.receive(None)
    await transport.aclose()

    assert len(batch.updates) == len(updates)
    assert all(update.message.status == "rejected" for update in batch.updates)
    assert all(update.message.text == "[rejected update]" for update in batch.updates)
    assert batch.next_cursor is not None and batch.next_cursor.value == "7"
