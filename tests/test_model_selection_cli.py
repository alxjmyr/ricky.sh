"""CLI tests for provider/model selection and the guided picker."""

from __future__ import annotations

import tomllib
from collections.abc import AsyncIterator
from pathlib import Path

from typer.testing import CliRunner

import ricky.interfaces.cli.app as cli_app
import ricky.interfaces.cli.select as cli_select
from ricky.interfaces.cli.app import app
from ricky.llm.types import (
    CompletionRequest,
    Message,
    MessageDone,
    ModelInfo,
    StreamEvent,
    TextDelta,
    TransportError,
)

runner = CliRunner()


def _isolated_project(
    tmp_path: Path,
    monkeypatch,
    *,
    openrouter_key: str | None = None,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='probe'\n")
    monkeypatch.chdir(tmp_path)
    for name in (
        "OPENROUTER_API_KEY",
        "RICKY_OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "RICKY_ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    if openrouter_key is not None:
        secrets = tmp_path / "user-data" / "profiles" / "personal" / ".secrets.toml"
        secrets.parent.mkdir(parents=True, exist_ok=True)
        secrets.write_text(f'openrouter_api_key = "{openrouter_key}"\n')


def test_chat_provider_and_model_flags_pin_session(tmp_path: Path, monkeypatch) -> None:
    _isolated_project(tmp_path, monkeypatch, openrouter_key="test-key")
    secrets = tmp_path / "user-data" / "profiles" / "personal" / ".secrets.toml"
    secrets.write_text(secrets.read_text() + 'anthropic_api_key = "test-key"\n')

    result = runner.invoke(
        app,
        ["chat", "-p", "anthropic", "-m", "custom/model"],
        input="/model attempted/change\n/clear\n/model\n/quit\n",
    )

    assert result.exit_code == 0
    assert "ricky chat (anthropic · custom/model)" in result.stdout
    assert result.stdout.count("Current model: anthropic · custom/model") == 2
    assert "Model selection is pinned for this session" in result.stdout
    assert "Started a fresh session" in result.stdout


def test_chat_profile_flags_issue_multi_profile_scope(tmp_path: Path, monkeypatch) -> None:
    _isolated_project(tmp_path, monkeypatch)

    result = runner.invoke(
        app,
        ["chat", "--profile", "work", "--access-profile", "personal"],
        input="/quit\n",
    )

    assert result.exit_code == 0
    assert "ricky chat (claude_code · sonnet)" in result.stdout
    assert "profile: work · access: shared, personal, work" in " ".join(result.stdout.split())


class _StreamingProvider:
    name = "claude_code"

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []
        self.closed = False

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        yield TextDelta(delta="profile reply")
        yield MessageDone(message=Message.text("assistant", "profile reply"))

    async def aclose(self) -> None:
        self.closed = True


def test_ask_profile_flags_preserve_streaming_and_model_policy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolated_project(tmp_path, monkeypatch)
    provider = _StreamingProvider()
    created: list[str] = []

    def fake_create_provider(name: str, _settings) -> _StreamingProvider:
        created.append(name)
        return provider

    monkeypatch.setattr(cli_app, "create_provider", fake_create_provider)

    result = runner.invoke(
        app,
        [
            "ask",
            "hello",
            "--profile",
            "work",
            "--access-profile",
            "personal",
            "--max-tokens",
            "12",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == "profile reply\n"
    assert created == ["claude_code"]
    assert provider.closed is True
    assert len(provider.requests) == 1
    assert provider.requests[0].model == "sonnet"
    assert provider.requests[0].max_tokens == 12
    assert provider.requests[0].messages == [Message.text("user", "hello")]


def test_unknown_provider_is_a_clear_cli_error(tmp_path: Path, monkeypatch) -> None:
    _isolated_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["chat", "-p", "unknown"])

    assert result.exit_code == 2
    assert "valid providers: openrouter, anthropic, claude_code" in result.stdout


def test_config_model_falls_back_to_manual_entry_without_auth(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolated_project(tmp_path, monkeypatch)

    result = runner.invoke(app, ["config", "model"], input="\nmanual/model\n")

    assert result.exit_code == 0
    assert "authentication missing" in result.stdout
    parsed = tomllib.loads(
        (tmp_path / "user-data" / "profiles" / "personal" / "ricky.toml").read_text()
    )
    assert parsed["profile"]["default_provider"] == "openrouter"
    assert parsed["profile"]["default_models"]["openrouter"] == "manual/model"


def test_config_model_uses_target_profile_default_and_filters_providers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolated_project(tmp_path, monkeypatch)
    work_config = tmp_path / "user-data" / "profiles" / "work" / "ricky.toml"
    work_config.parent.mkdir(parents=True)
    work_config.write_text(
        """[profile]
default_provider = "claude_code"
allowed_providers = ["claude_code"]

[profile.default_models]
claude_code = "work/default"
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_select, "auth_ready", lambda _name, _settings: False)

    result = runner.invoke(
        app,
        ["config", "model", "--profile", "work"],
        input="\nwork/selected\n",
    )

    assert result.exit_code == 0, result.output
    assert "claude_code" in result.stdout
    assert "(current default)" in result.stdout
    assert "openrouter" not in result.stdout
    assert "anthropic" not in result.stdout
    parsed = tomllib.loads(work_config.read_text())
    assert parsed["profile"]["default_provider"] == "claude_code"
    assert parsed["profile"]["default_models"]["claude_code"] == "work/selected"


class _CatalogProvider:
    def __init__(
        self,
        models: list[ModelInfo] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.models = models or []
        self.error = error
        self.closed = False

    async def list_models(self) -> list[ModelInfo]:
        if self.error is not None:
            raise self.error
        return self.models

    async def aclose(self) -> None:
        self.closed = True


def test_config_model_filters_catalog_and_accepts_numbered_selection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolated_project(tmp_path, monkeypatch)
    provider = _CatalogProvider(
        [
            ModelInfo(id="claude-opus", name="Claude Opus"),
            ModelInfo(id="claude-sonnet", name="Claude Sonnet"),
        ]
    )
    monkeypatch.setattr(cli_select, "auth_ready", lambda _name, _settings: True)
    monkeypatch.setattr(
        cli_select,
        "create_provider",
        lambda _name, _settings: provider,
    )

    result = runner.invoke(app, ["config", "model"], input="\nsonnet\n1\n")

    assert result.exit_code == 0
    assert "claude-sonnet" in result.stdout
    assert "claude-opus" not in result.stdout
    assert provider.closed is True
    parsed = tomllib.loads(
        (tmp_path / "user-data" / "profiles" / "personal" / "ricky.toml").read_text()
    )
    assert parsed["profile"]["default_models"]["openrouter"] == "claude-sonnet"


def test_config_model_accepts_typed_id_with_available_catalog(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolated_project(tmp_path, monkeypatch)
    provider = _CatalogProvider([ModelInfo(id="listed/model")])
    monkeypatch.setattr(cli_select, "auth_ready", lambda _name, _settings: True)
    monkeypatch.setattr(
        cli_select,
        "create_provider",
        lambda _name, _settings: provider,
    )

    result = runner.invoke(app, ["config", "model"], input="\n\ntyped/model\n")

    assert result.exit_code == 0
    parsed = tomllib.loads(
        (tmp_path / "user-data" / "profiles" / "personal" / "ricky.toml").read_text()
    )
    assert parsed["profile"]["default_models"]["openrouter"] == "typed/model"


def test_config_model_catalog_failure_falls_back_to_manual_entry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolated_project(tmp_path, monkeypatch)
    provider = _CatalogProvider(error=TransportError("network down"))
    monkeypatch.setattr(cli_select, "auth_ready", lambda _name, _settings: True)
    monkeypatch.setattr(
        cli_select,
        "create_provider",
        lambda _name, _settings: provider,
    )

    result = runner.invoke(app, ["config", "model"], input="\nfallback/model\n")

    assert result.exit_code == 0
    assert "Catalog unavailable: network down" in result.stdout
    assert provider.closed is True
    parsed = tomllib.loads(
        (tmp_path / "user-data" / "profiles" / "personal" / "ricky.toml").read_text()
    )
    assert parsed["profile"]["default_models"]["openrouter"] == "fallback/model"
