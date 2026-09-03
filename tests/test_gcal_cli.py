"""Configuration and CLI wiring tests for Google Calendar tools."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from ricky.config import RickySettings
from ricky.interfaces.cli import app as app_module
from ricky.interfaces.cli.app import app
from ricky.tools.integrations.gcal import GCAL_SCOPES
from ricky.tools.integrations.gcal.client import GcalError
from ricky.tools.integrations.gmail import GMAIL_SCOPES

runner = CliRunner()


def _project(tmp_path: Path, monkeypatch, *, with_provider_key: bool = False) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='probe'\n")
    (tmp_path / "user-data").mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path)
    for name in (
        "OPENROUTER_API_KEY",
        "RICKY_OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "RICKY_ANTHROPIC_API_KEY",
        "SLACK_USER_TOKEN",
        "RICKY_SLACK_USER_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    if with_provider_key:
        secrets = tmp_path / "user-data" / "profiles" / "personal" / ".secrets.toml"
        secrets.parent.mkdir(parents=True, exist_ok=True)
        secrets.write_text('openrouter_api_key = "test-key"\n')


def _google_profile(
    tmp_path: Path,
    profile: str,
    *,
    email: str,
    with_client: bool = True,
) -> None:
    root = tmp_path / "user-data" / "profiles" / profile
    root.mkdir(parents=True, exist_ok=True)
    (root / "ricky.toml").write_text(f'[google.accounts.{profile}]\nemail = "{email}"\n')
    if with_client:
        secrets = root / ".secrets.toml"
        existing = secrets.read_text() if secrets.exists() else ""
        secrets.write_text(
            existing + f"[google_oauth_clients.{profile}]\n"
            f'client_id = "{profile}-client"\n'
            f'client_secret = "{profile}-secret"\n'
        )


def test_gcal_defaults_and_config_table(tmp_path: Path, monkeypatch) -> None:
    _project(tmp_path, monkeypatch)
    (tmp_path / "user-data" / "ricky.toml").write_text(
        """[gcal]
api_base_url = "https://calendar.test/v3"
default_list_limit = 17
default_window_days = 14
description_char_limit = 9000
"""
    )

    settings = RickySettings()

    assert settings.gcal.api_base_url == "https://calendar.test/v3"
    assert settings.gcal.default_list_limit == 17
    assert settings.gcal.default_window_days == 14
    assert settings.gcal.description_char_limit == 9000

    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "gcal.api_base_url" in result.stdout
    assert "gcal.default_window_days" in result.stdout


def test_config_gcal_reports_missing_clients(tmp_path: Path, monkeypatch) -> None:
    _project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["config", "gcal"])

    assert result.exit_code == 1
    assert "matching OAuth client credentials" in result.stdout


def test_config_gcal_checks_both_accounts_and_closes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _project(tmp_path, monkeypatch)
    _google_profile(tmp_path, "personal", email="personal@example.com")
    _google_profile(tmp_path, "work", email="work@example.com")
    closed: list[bool] = []

    class FakeToolset:
        async def check_account(self, account: str) -> tuple[str, str]:
            return f"{account} primary", "America/Chicago"

        async def aclose(self) -> None:
            closed.append(True)

    monkeypatch.setattr(app_module, "gcal_toolset", lambda _settings, **_kwargs: FakeToolset())

    result = runner.invoke(app, ["config", "gcal"])

    assert result.exit_code == 0
    assert "Calendar [personal/personal] OK" in result.stdout
    assert "personal/personal primary" in result.stdout
    assert "Calendar [work/work] OK" in result.stdout
    assert "timezone America/Chicago" in result.stdout
    assert closed == [True]


def test_config_gcal_skips_accounts_without_credentials(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _project(tmp_path, monkeypatch)
    _google_profile(
        tmp_path,
        "personal",
        email="personal@example.com",
        with_client=False,
    )
    _google_profile(tmp_path, "work", email="work@example.com")

    class FakeToolset:
        async def check_account(self, account: str) -> tuple[str, str]:
            assert account == "work/work"
            return "work primary", "America/Chicago"

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(app_module, "gcal_toolset", lambda _settings, **_kwargs: FakeToolset())

    result = runner.invoke(app, ["config", "gcal"])

    assert result.exit_code == 0
    assert "Calendar [personal/personal] skipped: no OAuth client configured." in result.stdout
    assert "Calendar [work/work] OK" in result.stdout


def test_config_gcal_continues_after_one_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _project(tmp_path, monkeypatch)
    _google_profile(tmp_path, "personal", email="personal@example.com")
    _google_profile(tmp_path, "work", email="work@example.com")

    class FakeToolset:
        async def check_account(self, account: str) -> tuple[str, str]:
            if account == "personal/personal":
                raise GcalError("run ricky config google auth personal/personal")
            return "work primary", "America/Chicago"

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(app_module, "gcal_toolset", lambda _settings, **_kwargs: FakeToolset())

    result = runner.invoke(app, ["config", "gcal"])

    assert result.exit_code == 1
    assert "Calendar [personal/personal] check failed" in result.stdout
    assert "ricky config google auth personal/personal" in " ".join(result.stdout.split())
    assert "Calendar [work/work] OK" in result.stdout


def test_google_consent_and_status_use_gmail_calendar_scope_union(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _project(tmp_path, monkeypatch)
    captured: list[frozenset[str]] = []

    class FakeAuth:
        account_names: tuple[str, ...] = ()

        def statuses(self):
            return []

        async def aclose(self) -> None:
            pass

    def factory(_settings, *, scopes):
        captured.append(frozenset(scopes))
        return FakeAuth()

    monkeypatch.setattr(app_module, "GoogleAuth", factory)

    result = runner.invoke(app, ["config", "google"])

    assert result.exit_code == 1
    assert captured == [GMAIL_SCOPES | GCAL_SCOPES]


def test_chat_registers_all_calendar_tools_with_account_client(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _project(tmp_path, monkeypatch, with_provider_key=True)
    _google_profile(tmp_path, "personal", email="personal@example.com")
    captured: list[list[str]] = []
    original = app_module.ToolRegistry

    def capturing_registry(tools, **kwargs):
        captured.append([tool.name for tool in tools])
        return original(tools, **kwargs)

    monkeypatch.setattr(app_module, "ToolRegistry", capturing_registry)

    result = runner.invoke(app, ["chat"], input="/quit\n")

    assert result.exit_code == 0
    names = next(names for names in captured if "gcal_list_calendars" in names)
    assert "gcal_create_event" in names
    assert "gcal_respond_to_event" in names
    assert "gcal_delete_event" in names


def test_chat_closes_calendar_toolset_on_exit(tmp_path: Path, monkeypatch) -> None:
    _project(tmp_path, monkeypatch, with_provider_key=True)
    closed: list[bool] = []

    class FakeToolset:
        tools: list[object] = []

        async def aclose(self) -> None:
            closed.append(True)

    monkeypatch.setattr(app_module, "gcal_toolset", lambda _settings, **_kwargs: FakeToolset())

    result = runner.invoke(app, ["chat"], input="/quit\n")

    assert result.exit_code == 0
    assert closed == [True]


def test_gcal_toolset_is_absent_without_matching_client(tmp_path: Path, monkeypatch) -> None:
    from ricky.tools.integrations.gcal import gcal_toolset

    _project(tmp_path, monkeypatch)
    assert gcal_toolset(RickySettings()) is None
