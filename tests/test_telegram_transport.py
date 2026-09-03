"""Telegram wire adapter tests using only httpx.MockTransport."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from ricky.attachments import StoredAttachment
from ricky.config import TelegramAccountSettings
from ricky.interfaces.messaging.telegram import (
    TelegramAmbiguousDeliveryError,
    TelegramConflictError,
    TelegramTransport,
    split_telegram_text,
)
from ricky.messaging.types import TransportCursor, TransportMessage

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)
TOKEN = "123456:super-secret-test-token"


def _settings() -> TelegramAccountSettings:
    return TelegramAccountSettings(
        bot_token=SecretStr(TOKEN),
        api_base_url="https://telegram.invalid",
        long_poll_timeout_seconds=0,
        allowed_sender_ids=["100"],
        allowed_destination_ids=["200"],
        max_inbound_text_length=20,
    )


def _client(handler: httpx.AsyncBaseTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


async def test_receive_normalizes_authorized_text_and_advances_offset() -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 11,
                        "message": {
                            "message_id": 300,
                            "from": {"id": 100},
                            "chat": {"id": 200},
                            "text": " hello ",
                            "reply_to_message": {"message_id": 299},
                        },
                    }
                ],
            },
        )

    transport = TelegramTransport(
        "personal",
        _settings(),
        client=_client(httpx.MockTransport(handler)),
        clock=lambda: NOW,
    )
    batch = await transport.receive(
        TransportCursor(transport="telegram", account="personal", value="10")
    )
    await transport.aclose()

    assert requests[0]["offset"] == 11
    assert batch.next_cursor is not None and batch.next_cursor.value == "11"
    assert batch.updates[0].message.text == "hello"
    assert batch.updates[0].message.reply_to_platform_message_id == "299"
    assert batch.updates[0].message.status == "pending"


async def test_unauthorized_and_unsupported_updates_are_bounded_and_later_updates_continue() -> (
    None
):
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {"update_id": 1, "edited_message": {"text": "ignored"}},
                    {
                        "update_id": 2,
                        "message": {
                            "message_id": 20,
                            "from": {"id": 999},
                            "chat": {"id": 200},
                            "text": "private content",
                        },
                    },
                    {
                        "update_id": 3,
                        "message": {
                            "message_id": 30,
                            "from": {"id": 100},
                            "chat": {"id": 200},
                            "text": "accepted",
                        },
                    },
                ],
            },
        )

    transport = TelegramTransport(
        "personal",
        _settings(),
        client=_client(httpx.MockTransport(handler)),
        clock=lambda: NOW,
    )
    batch = await transport.receive(None)
    await transport.aclose()

    assert [item.message.status for item in batch.updates] == ["rejected", "rejected", "pending"]
    assert [item.message.text for item in batch.updates[:2]] == [
        "[rejected update]",
        "[rejected update]",
    ]
    assert "private content" not in batch.model_dump_json()
    assert batch.next_cursor is not None and batch.next_cursor.value == "3"


def test_splitting_is_deterministic_and_below_telegram_limit() -> None:
    text = "a" * 8_005
    first = split_telegram_text(text)
    second = split_telegram_text(text)
    assert first == second
    assert "".join(first) == text
    assert [len(part) for part in first] == [4_000, 4_000, 5]
    assert all(len(part) < 4_096 for part in first)


def test_splitting_preserves_positional_limit_argument() -> None:
    parts = split_telegram_text("x" * 250, 100)

    assert [len(part) for part in parts] == [100, 100, 50]


async def test_send_message_is_plain_text_and_returns_receipt() -> None:
    bodies: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 444}})

    transport = TelegramTransport(
        "personal",
        _settings(),
        client=_client(httpx.MockTransport(handler)),
        clock=lambda: NOW,
    )
    message = TransportMessage(
        id="transport_message_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        transport="telegram",
        account="personal",
        destination_id="200",
        text="literal *text*",
        outbox_id="outbox_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        part_number=1,
        part_count=1,
    )
    receipt = await transport.send(message)
    await transport.aclose()

    assert bodies == [{"chat_id": "200", "text": "literal *text*"}]
    assert "parse_mode" not in bodies[0]
    assert receipt.platform_message_id == "444"


async def test_send_portable_markdown_uses_telegram_rich_message() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 445}})

    transport = TelegramTransport(
        "personal",
        _settings(),
        client=_client(httpx.MockTransport(handler)),
        clock=lambda: NOW,
    )
    message = TransportMessage(
        id="transport_message_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        transport="telegram",
        account="personal",
        destination_id="200",
        text="## Result\n\n**Done.**",
        text_format="portable_markdown_v1",
        outbox_id="outbox_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        part_number=1,
        part_count=1,
    )

    receipt = await transport.send(message)
    await transport.aclose()

    assert requests == [
        (
            f"/bot{TOKEN}/sendRichMessage",
            {
                "chat_id": "200",
                "rich_message": {
                    "markdown": "## Result\n\n**Done.**",
                    "skip_entity_detection": True,
                },
            },
        )
    ]
    assert receipt.platform_message_id == "445"


@pytest.mark.parametrize(
    ("status_code", "description"),
    [
        (400, "Bad Request: can't parse rich markdown"),
        (404, "Not Found"),
    ],
)
async def test_definitive_rich_rejection_falls_back_once_to_readable_plain_text(
    status_code: int,
    description: str,
) -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path.endswith("/sendRichMessage"):
            return httpx.Response(
                status_code,
                json={
                    "ok": False,
                    "error_code": status_code,
                    "description": description,
                },
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 446}})

    transport = TelegramTransport(
        "personal",
        _settings(),
        client=_client(httpx.MockTransport(handler)),
        clock=lambda: NOW,
    )
    message = TransportMessage(
        id="transport_message_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        transport="telegram",
        account="personal",
        destination_id="200",
        text="## Result\n\n**Done** with [docs](https://example.com).",
        text_format="portable_markdown_v1",
        outbox_id="outbox_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        part_number=1,
        part_count=1,
    )

    receipt = await transport.send(message)
    await transport.aclose()

    assert [path.rsplit("/", 1)[-1] for path, _body in requests] == [
        "sendRichMessage",
        "sendMessage",
    ]
    assert requests[1][1] == {
        "chat_id": "200",
        "text": "Result\n\nDone with docs (https://example.com).",
        "link_preview_options": {"is_disabled": True},
    }
    assert receipt.platform_message_id == "446"


async def test_ambiguous_rich_send_is_never_retried_as_plain_text() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("ambiguous", request=request)

    transport = TelegramTransport(
        "personal",
        _settings(),
        client=_client(httpx.MockTransport(handler)),
        clock=lambda: NOW,
    )
    message = TransportMessage(
        id="transport_message_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        transport="telegram",
        account="personal",
        destination_id="200",
        text="**Possibly sent.**",
        text_format="portable_markdown_v1",
        outbox_id="outbox_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        part_number=1,
        part_count=1,
    )

    with pytest.raises(TelegramAmbiguousDeliveryError, match="ambiguous"):
        await transport.send(message)
    await transport.aclose()

    assert calls == 1


async def test_send_document_uses_durable_attachment_and_returns_receipt(
    tmp_path: Path,
) -> None:
    content = b"attachment bytes"
    relative = Path("notifications/attachments/n/file.txt")
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 402}})

    transport = TelegramTransport(
        "personal",
        _settings(),
        client=_client(httpx.MockTransport(handler)),
        clock=lambda: NOW,
        user_data_root=tmp_path,
    )
    message = TransportMessage(
        id="transport_message_cccccccccccccccccccccccccccccccc",
        transport="telegram",
        account="personal",
        destination_id="200",
        attachment=StoredAttachment(
            storage_path=str(relative),
            filename="file.txt",
            media_type="text/plain",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        ),
        outbox_id="outbox-1",
        part_number=1,
        part_count=1,
    )

    receipt = await transport.send(message)
    await transport.aclose()

    assert requests[0].url.path.endswith("/sendDocument")
    assert b"attachment bytes" in requests[0].content
    assert b'filename="file.txt"' in requests[0].content
    assert receipt.platform_message_id == "402"


async def test_read_timeout_is_ambiguous_and_conflict_is_operator_clear() -> None:
    async def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret transport detail", request=request)

    timeout_transport = TelegramTransport(
        "personal",
        _settings(),
        client=_client(httpx.MockTransport(timeout_handler)),
        clock=lambda: NOW,
    )
    message = TransportMessage(
        id="transport_message_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        transport="telegram",
        account="personal",
        destination_id="200",
        text="hello",
        outbox_id="outbox_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        part_number=1,
        part_count=1,
    )
    with pytest.raises(TelegramAmbiguousDeliveryError, match="ambiguous"):
        await timeout_transport.send(message)
    await timeout_transport.aclose()

    async def conflict_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(409, json={"ok": False, "error_code": 409})

    conflict_transport = TelegramTransport(
        "personal",
        _settings(),
        client=_client(httpx.MockTransport(conflict_handler)),
        clock=lambda: NOW,
    )
    with pytest.raises(TelegramConflictError, match="another poller"):
        await conflict_transport.receive(None)
    await conflict_transport.aclose()


async def test_cancellation_during_long_poll_closes_httpx_client() -> None:
    started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    client = _client(httpx.MockTransport(handler))
    transport = TelegramTransport("personal", _settings(), client=client, clock=lambda: NOW)
    task = asyncio.create_task(transport.receive(None))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await transport.aclose()
    assert client.is_closed


def test_bot_token_is_redacted_from_models_and_validation_errors() -> None:
    settings = _settings()
    rendered = f"{settings!r}\n{settings.model_dump()}\n{settings.model_dump_json()}"
    assert TOKEN not in rendered
    with pytest.raises(ValidationError) as caught:
        TelegramAccountSettings(
            bot_token=SecretStr(TOKEN),
            api_base_url="invalid",
        )
    assert TOKEN not in str(caught.value)
