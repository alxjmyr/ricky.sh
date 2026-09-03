"""Gateway CLI shape, secret redaction, and provider-free inspection tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from pydantic import SecretStr
from typer.testing import CliRunner

from ricky.config import (
    MessagingSettings,
    ProfileConfigSettings,
    ProfileMessagingSettings,
    RickySettings,
    TelegramAccountSettings,
)
from ricky.interfaces.cli import gateway
from ricky.interfaces.cli.app import app
from ricky.interfaces.messaging.telegram import TelegramBotIdentity
from ricky.messaging import MessagingStore
from ricky.messaging.types import InboundMessage, ReceiveBatch, ReceivedUpdate, TransportCursor

TOKEN = "123456:never-render-this-token"


def _settings(tmp_path: Path) -> RickySettings:
    account = TelegramAccountSettings(
        bot_token=SecretStr(TOKEN),
        allowed_sender_ids=["100"],
        allowed_destination_ids=["200"],
    )
    return RickySettings(
        user_data_dir=str(tmp_path / "user"),
        messaging=MessagingSettings(telegram_accounts={"personal/bot": account}),
        profile_configs={
            "personal": ProfileConfigSettings(
                messaging=ProfileMessagingSettings(telegram_accounts={"bot": account})
            )
        },
    )


def test_gateway_command_tree_is_registered() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["gateway", "transport", "poll", "telegram", "--help"])
    assert result.exit_code == 0
    assert "--once" in result.output
    assert "ACCOUNT" in result.output


def test_operator_view_unions_accounts_without_intersecting_route_policy(
    tmp_path: Path,
) -> None:
    account = TelegramAccountSettings(bot_token=SecretStr(TOKEN))
    settings = RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "profile_configs": {
                "personal": {
                    "messaging": {"telegram_accounts": {"bot": account}},
                },
                "work": {
                    "messaging": {"telegram_accounts": {"bot": account}},
                    "agents": {
                        "gateway_foreground": {"exclude_capabilities": ["builtin.project.read"]}
                    },
                },
            },
        }
    )

    operator = gateway._operator_runtime_settings(settings)
    personal = operator.resolve_profile_runtime_settings(operator.resolve_profile_scope("personal"))

    assert set(operator.messaging.telegram_accounts) == {"personal/bot", "work/bot"}
    assert operator.agents == settings.agents
    assert personal.agents.gateway_foreground.exclude_capabilities == []


def test_doctor_cli_never_renders_bot_token(tmp_path: Path, monkeypatch: object) -> None:
    settings = _settings(tmp_path)

    class FakeTelegramTransport:
        def __init__(self, account: str, config: TelegramAccountSettings) -> None:
            assert account == "personal/bot"
            assert config.bot_token.get_secret_value() == TOKEN

        async def doctor(self) -> TelegramBotIdentity:
            return TelegramBotIdentity(id="42", username="ricky_test_bot", display_name="Ricky")

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(gateway, "load_settings", lambda: settings)  # type: ignore[attr-defined]
    monkeypatch.setattr(gateway, "TelegramTransport", FakeTelegramTransport)  # type: ignore[attr-defined]
    result = CliRunner().invoke(app, ["gateway", "transport", "doctor", "telegram", "personal/bot"])
    assert result.exit_code == 0
    assert "@ricky_test_bot" in result.output
    assert TOKEN not in result.output


def test_inbox_inspection_uses_only_durable_store(tmp_path: Path, monkeypatch: object) -> None:
    settings = _settings(tmp_path)
    store = MessagingStore(settings)
    asyncio.run(store.initialize())
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    message = InboundMessage(
        id="inbound_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        transport="telegram",
        account="personal/bot",
        update_id="1",
        destination_id="200",
        sender_id="100",
        platform_message_id="300",
        text="inspect me",
        received_at=now,
        status="pending",
    )
    asyncio.run(
        store.ingest(
            ReceiveBatch(
                transport="telegram",
                account="personal/bot",
                updates=[ReceivedUpdate(update_id="1", message=message)],
                next_cursor=TransportCursor(
                    transport="telegram", account="personal/bot", value="1"
                ),
            )
        )
    )
    monkeypatch.setattr(gateway, "load_settings", lambda: settings)  # type: ignore[attr-defined]

    result = CliRunner().invoke(app, ["gateway", "inbox", "show", message.id])
    assert result.exit_code == 0
    assert "inspect me" in result.output
    assert TOKEN not in result.output


def test_inbox_dismiss_acknowledges_pending_message_offline(
    tmp_path: Path, monkeypatch: object
) -> None:
    settings = _settings(tmp_path)
    store = MessagingStore(settings)
    asyncio.run(store.initialize())
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    message = InboundMessage(
        id="inbound_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        transport="telegram",
        account="personal/bot",
        update_id="2",
        destination_id="200",
        sender_id="100",
        platform_message_id="301",
        text="dismiss me",
        received_at=now,
        status="pending",
    )
    asyncio.run(
        store.ingest(
            ReceiveBatch(
                transport="telegram",
                account="personal/bot",
                updates=[ReceivedUpdate(update_id="2", message=message)],
                next_cursor=TransportCursor(
                    transport="telegram", account="personal/bot", value="2"
                ),
            )
        )
    )
    monkeypatch.setattr(gateway, "load_settings", lambda: settings)  # type: ignore[attr-defined]

    result = CliRunner().invoke(app, ["gateway", "inbox", "dismiss", message.id])

    assert result.exit_code == 0
    assert f"Dismissed inbox message: {message.id}" in result.output
    assert asyncio.run(store.get_inbox(message.id)).status == "processed"
    assert TOKEN not in result.output
