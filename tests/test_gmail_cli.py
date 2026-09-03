"""CLI wiring tests for Google OAuth and Gmail tools."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from ricky.interfaces.cli import app as app_module
from ricky.interfaces.cli.app import app
from ricky.tools.integrations.google import GoogleAuthError, GoogleAuthStatus

runner = CliRunner()
PERSONAL_ACCOUNT = "personal/personal"
WORK_ACCOUNT = "work/work"


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


def _google_profile(tmp_path: Path, profile: str, *, email: str) -> None:
    root = tmp_path / "user-data" / "profiles" / profile
    root.mkdir(parents=True, exist_ok=True)
    (root / "ricky.toml").write_text(f'[google.accounts.{profile}]\nemail = "{email}"\n')
    secrets = root / ".secrets.toml"
    existing = secrets.read_text() if secrets.exists() else ""
    secrets.write_text(
        existing + f"[google_oauth_clients.{profile}]\n"
        f'client_id = "{profile}-client"\n'
        f'client_secret = "{profile}-secret"\n'
    )


class FakeGoogleAuth:
    def __init__(
        self,
        statuses: list[GoogleAuthStatus],
        *,
        authorize_error: Exception | None = None,
    ) -> None:
        self._statuses = statuses
        self.account_names = tuple(status.account for status in statuses)
        self.authorize_error = authorize_error
        self.authorized: list[str] = []
        self.authorization_options: list[tuple[bool, int]] = []
        self.closed = False

    def statuses(self) -> list[GoogleAuthStatus]:
        return self._statuses

    async def authorize(
        self,
        account: str,
        *,
        on_authorization_url,
        open_browser: bool = True,
        callback_port: int = 0,
    ):
        self.authorized.append(account)
        self.authorization_options.append((open_browser, callback_port))
        on_authorization_url(
            f"https://auth.test/authorize?client_id={account}-client&state=redacted"
        )
        if self.authorize_error is not None:
            raise self.authorize_error
        return next(status for status in self._statuses if status.account == account)

    async def aclose(self) -> None:
        self.closed = True


def _status(
    account: str,
    *,
    client: bool = True,
    token: bool = True,
    missing_scopes: list[str] | None = None,
) -> GoogleAuthStatus:
    email = f"{account}@example.com"
    return GoogleAuthStatus(
        account=account,
        expected_email=email,
        client_configured=client,
        token_present=token,
        stored_email=email if token else None,
        granted_scopes=["email", "openid", "gmail.modify"] if token else [],
        missing_scopes=missing_scopes or [],
    )


def test_config_google_reports_no_accounts(tmp_path: Path, monkeypatch) -> None:
    _project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["config", "google"])

    assert result.exit_code == 1
    assert "No Google accounts are configured" in result.stdout


def test_config_google_reports_each_redacted_status_and_closes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _project(tmp_path, monkeypatch)
    fake = FakeGoogleAuth(
        [
            _status(PERSONAL_ACCOUNT),
            _status(WORK_ACCOUNT, client=False, token=False),
        ]
    )
    monkeypatch.setattr(app_module, "GoogleAuth", lambda *_a, **_k: fake)

    result = runner.invoke(app, ["config", "google"])

    assert result.exit_code == 1
    output = " ".join(result.stdout.split())
    assert f"Google [{PERSONAL_ACCOUNT}]" in output
    assert f"authorized as {PERSONAL_ACCOUNT}@example.com" in output
    assert f"Google [{WORK_ACCOUNT}]" in output
    assert "OAuth client missing" in result.stdout
    assert fake.closed


def test_config_google_success_for_both_accounts(tmp_path: Path, monkeypatch) -> None:
    _project(tmp_path, monkeypatch)
    fake = FakeGoogleAuth([_status(PERSONAL_ACCOUNT), _status(WORK_ACCOUNT)])
    monkeypatch.setattr(app_module, "GoogleAuth", lambda *_a, **_k: fake)

    result = runner.invoke(app, ["config", "google"])

    assert result.exit_code == 0
    assert result.stdout.count("authorized as") == 2
    assert fake.closed


def test_config_google_auth_runs_flow_prints_url_and_closes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _project(tmp_path, monkeypatch)
    fake = FakeGoogleAuth([_status(WORK_ACCOUNT)])
    monkeypatch.setattr(app_module, "GoogleAuth", lambda *_a, **_k: fake)

    result = runner.invoke(app, ["config", "google", "auth", WORK_ACCOUNT])

    assert result.exit_code == 0
    assert "https://auth.test/authorize" in result.stdout
    assert f"authorized as {WORK_ACCOUNT}@example.com" in result.stdout
    assert fake.authorized == [WORK_ACCOUNT]
    assert fake.authorization_options == [(True, 0)]
    assert fake.closed


def test_config_google_auth_headless_uses_fixed_callback_port(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _project(tmp_path, monkeypatch)
    fake = FakeGoogleAuth([_status(PERSONAL_ACCOUNT)])
    monkeypatch.setattr(app_module, "GoogleAuth", lambda *_a, **_k: fake)

    result = runner.invoke(
        app,
        [
            "config",
            "google",
            "auth",
            PERSONAL_ACCOUNT,
            "--no-browser",
            "--callback-port",
            "8765",
        ],
    )

    assert result.exit_code == 0
    assert "browser launch disabled" in result.stdout
    assert fake.authorization_options == [(False, 8765)]
    assert fake.closed


def test_config_google_auth_error_is_safe_and_closes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _project(tmp_path, monkeypatch)
    fake = FakeGoogleAuth(
        [_status(WORK_ACCOUNT)],
        authorize_error=GoogleAuthError("identity mismatch; no token was stored"),
    )
    monkeypatch.setattr(app_module, "GoogleAuth", lambda *_a, **_k: fake)

    result = runner.invoke(app, ["config", "google", "auth", WORK_ACCOUNT])

    assert result.exit_code == 1
    assert "identity mismatch" in result.stdout
    assert "Traceback" not in result.stdout
    assert fake.closed


def test_config_gmail_reports_missing_clients(tmp_path: Path, monkeypatch) -> None:
    _project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["config", "gmail"])

    assert result.exit_code == 1
    assert "matching OAuth client credentials" in result.stdout


def test_config_gmail_checks_both_accounts_and_closes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _project(tmp_path, monkeypatch)
    _google_profile(tmp_path, "personal", email="personal@example.com")
    _google_profile(tmp_path, "work", email="work@example.com")
    closed: list[bool] = []

    class FakeToolset:
        async def check_account(self, account: str) -> tuple[str, int]:
            return f"{account}@example.com", 42 if account == "personal/personal" else 84

        async def aclose(self) -> None:
            closed.append(True)

    monkeypatch.setattr(app_module, "gmail_toolset", lambda _settings, **_kwargs: FakeToolset())

    result = runner.invoke(app, ["config", "gmail"])

    assert result.exit_code == 0
    assert "Gmail [personal/personal] OK" in result.stdout
    assert "42 total messages" in " ".join(result.stdout.split())
    assert "Gmail [work/work] OK" in result.stdout
    assert "84 total messages" in result.stdout
    assert closed == [True]


def test_config_gmail_continues_after_one_account_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _project(tmp_path, monkeypatch)
    _google_profile(tmp_path, "personal", email="personal@example.com")
    _google_profile(tmp_path, "work", email="work@example.com")

    class FakeToolset:
        async def check_account(self, account: str) -> tuple[str, int]:
            if account == "personal/personal":
                raise GoogleAuthError("run ricky config google auth personal/personal")
            return "work@example.com", 84

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(app_module, "gmail_toolset", lambda _settings, **_kwargs: FakeToolset())

    result = runner.invoke(app, ["config", "gmail"])

    assert result.exit_code == 1
    assert "Gmail [personal/personal] check failed" in result.stdout
    assert "ricky config google auth personal/personal" in " ".join(result.stdout.split())
    assert "Gmail [work/work] OK" in result.stdout


def test_chat_registers_gmail_tools_with_matching_account_client(
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
    assert any("gmail_send_message" in names for names in captured)
    assert any("gmail_download_attachment" in names for names in captured)


def test_chat_closes_gmail_toolset_on_exit(tmp_path: Path, monkeypatch) -> None:
    _project(tmp_path, monkeypatch, with_provider_key=True)
    closed: list[bool] = []

    class FakeToolset:
        tools: list[object] = []

        async def aclose(self) -> None:
            closed.append(True)

    monkeypatch.setattr(app_module, "gmail_toolset", lambda _settings, **_kwargs: FakeToolset())

    result = runner.invoke(app, ["chat"], input="/quit\n")

    assert result.exit_code == 0
    assert closed == [True]


def test_config_google_reports_changed_oauth_client(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _project(tmp_path, monkeypatch)
    status = _status(PERSONAL_ACCOUNT)
    status.client_matches = False
    fake = FakeGoogleAuth([status])
    monkeypatch.setattr(app_module, "GoogleAuth", lambda *_a, **_k: fake)

    result = runner.invoke(app, ["config", "google"])

    assert result.exit_code == 1
    assert "OAuth client changed" in result.stdout
    assert f"ricky config google auth {PERSONAL_ACCOUNT}" in " ".join(result.stdout.split())
    assert fake.closed


def test_config_google_auth_rejects_invalid_callback_port(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _project(tmp_path, monkeypatch)
    fake = FakeGoogleAuth([_status(PERSONAL_ACCOUNT)])
    monkeypatch.setattr(app_module, "GoogleAuth", lambda *_a, **_k: fake)

    result = runner.invoke(
        app,
        [
            "config",
            "google",
            "auth",
            "personal",
            "--callback-port",
            "0",
        ],
    )

    assert result.exit_code == 2
    assert fake.authorization_options == []
