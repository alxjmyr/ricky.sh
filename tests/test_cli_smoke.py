"""Smoke tests for the CLI entry point."""

from __future__ import annotations

from typer.testing import CliRunner

from ricky import __version__
from ricky.interfaces.cli.app import app

runner = CliRunner()


def _isolated_project(tmp_path, monkeypatch, *, api_key: str | None = None) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='probe'\n")
    (tmp_path / "user-data").mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("RICKY_OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("RICKY_ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
    monkeypatch.delenv("RICKY_SLACK_USER_TOKEN", raising=False)
    if api_key is not None:
        secrets = tmp_path / "user-data" / "profiles" / "personal" / ".secrets.toml"
        secrets.parent.mkdir(parents=True, exist_ok=True)
        secrets.write_text(f'openrouter_api_key = "{api_key}"\n', encoding="utf-8")


def _write_slack_token(tmp_path, token: str) -> None:
    secrets = tmp_path / "user-data" / "profiles" / "personal" / ".secrets.toml"
    secrets.parent.mkdir(parents=True, exist_ok=True)
    existing = secrets.read_text(encoding="utf-8") if secrets.exists() else ""
    secrets.write_text(existing + f'slack_user_token = "{token}"\n', encoding="utf-8")


def test_help_exits_cleanly() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "agentic assistant" in result.stdout


def test_no_args_starts_chat_and_can_quit(tmp_path, monkeypatch) -> None:
    _isolated_project(tmp_path, monkeypatch, api_key="test-key")

    result = runner.invoke(app, [], input="/quit\n")

    assert result.exit_code == 0
    assert "ricky chat" in result.stdout
    assert "Exiting" in result.stdout


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_config_command_runs(tmp_path, monkeypatch) -> None:
    _isolated_project(tmp_path, monkeypatch)
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "default_provider" in result.stdout
    assert "providers.openrouter.default_model" in result.stdout
    assert "anthropic_api_key" in result.stdout
    assert "not set" in result.stdout


def test_chat_command_quits(tmp_path, monkeypatch) -> None:
    _isolated_project(tmp_path, monkeypatch, api_key="test-key")

    result = runner.invoke(app, ["chat"], input="/quit\n")

    assert result.exit_code == 0
    assert "ricky chat" in result.stdout


def test_ask_without_api_key_exits_with_provider_error(tmp_path, monkeypatch) -> None:
    _isolated_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["ask", "hello"])

    assert result.exit_code == 1
    assert "Provider error" in result.stdout
    assert "API key" in result.stdout


def test_skill_command_lists_loaded_bundled_skills(tmp_path, monkeypatch, bundled_root) -> None:
    _isolated_project(tmp_path, monkeypatch, api_key="test-key")
    skill_dir = bundled_root / "skills" / "probe"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: probe
description: Probe the project
---
Probe instructions.
""",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["chat"], input="/skill\n/quit\n")

    assert result.exit_code == 0
    assert "probe" in result.stdout
    assert "Probe the project" in result.stdout


def test_skill_command_activates_prompt_skill(tmp_path, monkeypatch, bundled_root) -> None:
    _isolated_project(tmp_path, monkeypatch, api_key="test-key")
    skill_dir = bundled_root / "skills" / "probe"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: probe
description: Probe the project
---
Probe instructions.
""",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["chat"], input="/skill probe focused\n/quit\n")

    assert result.exit_code == 0
    assert "[skill] probe focused" in result.stdout


def test_config_slack_reports_missing_token(tmp_path, monkeypatch) -> None:
    _isolated_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["config", "slack"])

    assert result.exit_code == 1
    assert "slack_user_token is not set" in result.stdout


def test_config_slack_reports_identity_and_closes_toolset(tmp_path, monkeypatch) -> None:
    _isolated_project(tmp_path, monkeypatch)
    closed: list[bool] = []

    class FakeToolset:
        async def check_auth(self) -> tuple[str, str]:
            return "alex", "lyric"

        async def aclose(self) -> None:
            closed.append(True)

    monkeypatch.setattr("ricky.interfaces.cli.app.slack_toolset", lambda _s: FakeToolset())

    result = runner.invoke(app, ["config", "slack"])

    assert result.exit_code == 0
    assert "authenticated as alex in lyric" in result.stdout
    assert closed == [True]


def test_config_slack_checks_each_configured_profile(tmp_path, monkeypatch) -> None:
    _isolated_project(tmp_path, monkeypatch)
    for profile in ("personal", "work"):
        profile_root = tmp_path / "user-data" / "profiles" / profile
        profile_root.mkdir(parents=True)
        (profile_root / "ricky.toml").write_text(
            f'[slack]\ndownload_dir = "downloads/{profile}-slack"\n',
            encoding="utf-8",
        )
        (profile_root / ".secrets.toml").write_text(
            f'slack_user_token = "{profile}-token"\n',
            encoding="utf-8",
        )
    observed: list[tuple[str, str]] = []

    class FakeToolset:
        def __init__(self, profile: str) -> None:
            self.profile = profile

        async def check_auth(self) -> tuple[str, str]:
            return self.profile, f"{self.profile}-team"

        async def aclose(self) -> None:
            observed.append((self.profile, "closed"))

    def fake_toolset(settings):
        token = settings.slack_user_token
        assert token is not None
        profile = token.get_secret_value().removesuffix("-token")
        assert settings.slack.download_dir == f"profiles/{profile}/downloads/{profile}-slack"
        return FakeToolset(profile)

    monkeypatch.setattr("ricky.interfaces.cli.app.slack_toolset", fake_toolset)

    result = runner.invoke(app, ["config", "slack"])

    assert result.exit_code == 0
    assert "Slack [personal] OK: authenticated as personal in personal-team." in result.stdout
    assert "Slack [work] OK: authenticated as work in work-team." in result.stdout
    assert observed == [("personal", "closed"), ("work", "closed")]


def test_config_slack_reports_auth_failure(tmp_path, monkeypatch) -> None:
    _isolated_project(tmp_path, monkeypatch)

    from ricky.tools.integrations.slack.client import SlackApiError

    class FailingToolset:
        async def check_auth(self) -> tuple[str, str]:
            raise SlackApiError("auth.test", "invalid_auth")

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr("ricky.interfaces.cli.app.slack_toolset", lambda _s: FailingToolset())

    result = runner.invoke(app, ["config", "slack"])

    assert result.exit_code == 1
    assert "invalid_auth" in result.stdout


def test_config_slack_reports_invalid_base_url_without_traceback(tmp_path, monkeypatch) -> None:
    _isolated_project(tmp_path, monkeypatch)
    _write_slack_token(tmp_path, "xoxp-test")
    (tmp_path / "user-data" / "ricky.toml").write_text(
        '[slack]\napi_base_url = "https://slack.com:bad/api"\n',
        encoding="utf-8",
    )

    result = runner.invoke(app, ["config", "slack"])

    assert result.exit_code == 1
    assert "slack.api_base_url" in result.stdout
    assert "Traceback" not in result.stdout


def test_chat_closes_provider_when_slack_toolset_construction_fails(tmp_path, monkeypatch) -> None:
    _isolated_project(tmp_path, monkeypatch, api_key="test-key")
    closed: list[bool] = []

    class FakeProvider:
        async def aclose(self) -> None:
            closed.append(True)

    monkeypatch.setattr(
        "ricky.interfaces.cli.app.create_provider",
        lambda _name, _settings: FakeProvider(),
    )

    def fail_toolset(_settings):
        raise ValueError("slack toolset construction failed")

    monkeypatch.setattr("ricky.interfaces.cli.app.slack_toolset", fail_toolset)

    result = runner.invoke(app, ["chat"])

    assert result.exit_code == 2
    assert "slack toolset construction failed" in result.stdout
    assert closed == [True]


def test_chat_registers_slack_tools_only_with_token(tmp_path, monkeypatch) -> None:
    _isolated_project(tmp_path, monkeypatch, api_key="test-key")
    from ricky.interfaces.cli import app as app_module

    captured: list[list[str]] = []
    original = app_module.ToolRegistry

    def capturing_registry(tools, **kwargs):
        captured.append([tool.name for tool in tools])
        return original(tools, **kwargs)

    monkeypatch.setattr(app_module, "ToolRegistry", capturing_registry)

    runner.invoke(app, ["chat"], input="/quit\n")
    assert not any("slack_send_message" in names for names in captured)

    _write_slack_token(tmp_path, "xoxp-test")
    runner.invoke(app, ["chat"], input="/quit\n")
    assert any("slack_send_message" in names for names in captured[-1:])
