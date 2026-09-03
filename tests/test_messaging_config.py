"""Telegram public/secret configuration shape tests."""

from pathlib import Path

from ricky.config import load_settings_at


def test_telegram_account_merges_public_config_and_secret_token(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'config-test'\n")
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    (user_data / "ricky.toml").write_text(
        """
[messaging.transports.telegram-personal]
type = "telegram"
account = "personal/bot"

[messaging.routes.owner]
transport = "telegram-personal"
destination = "200"
owner_profile = "personal"
accepted_profiles = ["shared", "personal"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    profile = user_data / "profiles" / "personal"
    profile.mkdir(parents=True)
    (profile / "ricky.toml").write_text(
        """
[messaging.telegram_accounts.bot]
api_base_url = "https://telegram.invalid"
long_poll_timeout_seconds = 12
allowed_sender_ids = ["100"]
allowed_destination_ids = ["200"]
max_inbound_text_length = 1234
enabled = true
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (profile / ".secrets.toml").write_text(
        """
[messaging.telegram_accounts.bot]
bot_token = "test-bot-token"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    root_settings = load_settings_at(user_data)
    settings = root_settings.resolve_profile_runtime_settings(root_settings.resolve_profile_scope())
    account = settings.messaging.telegram_accounts["personal/bot"]
    assert account.api_base_url == "https://telegram.invalid"
    assert account.long_poll_timeout_seconds == 12
    assert account.allowed_sender_ids == ["100"]
    assert account.allowed_destination_ids == ["200"]
    assert account.max_inbound_text_length == 1234
    assert account.bot_token.get_secret_value() == "test-bot-token"
