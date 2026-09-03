"""Tests for provider registry construction and session pinning."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from ricky.agent import AgentSession
from ricky.config import ProvidersSettings, RickySettings
from ricky.llm.anthropic import AnthropicProvider
from ricky.llm.factory import (
    auth_ready,
    create_default_provider,
    create_provider,
    provider_entries,
    provider_names,
)
from ricky.llm.openrouter import OpenRouterProvider


def _settings() -> RickySettings:
    return RickySettings(
        default_provider="anthropic",
        openrouter_api_key=SecretStr("openrouter-key"),
        anthropic_api_key=SecretStr("anthropic-key"),
    )


def test_registry_metadata_and_auth_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ricky.llm.factory.shutil.which", lambda _path: None)
    settings = RickySettings(openrouter_api_key=None, anthropic_api_key=None)

    assert provider_names() == ["openrouter", "anthropic", "claude_code"]
    assert [entry.title for entry in provider_entries()] == [
        "OpenRouter",
        "Anthropic",
        "Claude Code (local subscription)",
    ]
    assert auth_ready("openrouter", settings) is False
    assert auth_ready("anthropic", settings) is False
    assert auth_ready("claude_code", settings) is False


@pytest.mark.asyncio
async def test_registry_constructs_named_and_default_providers() -> None:
    settings = _settings()

    openrouter = create_provider("openrouter", settings)
    default = create_default_provider(settings)

    assert isinstance(openrouter, OpenRouterProvider)
    assert isinstance(default, AnthropicProvider)

    await openrouter.aclose()
    await default.aclose()


def test_registry_names_match_typed_provider_settings() -> None:
    """The factory registry and ProvidersSettings fields must never drift apart."""
    assert provider_names() == list(ProvidersSettings.model_fields)

    settings = _settings()
    for name in provider_names():
        selection = settings.resolve_selection(name)
        assert selection.provider == name
        assert selection.model


@pytest.mark.parametrize("operation", ["auth", "create"])
def test_registry_rejects_unknown_provider(operation: str) -> None:
    settings = _settings()

    with pytest.raises(ValueError, match="openrouter, anthropic, claude_code"):
        if operation == "auth":
            auth_ready("unknown", settings)
        else:
            create_provider("unknown", settings)


def test_session_is_pinned_to_resolved_provider_and_model() -> None:
    settings = _settings()

    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="custom/model",
    )
    restored = AgentSession.model_validate_json(session.model_dump_json())

    assert restored == session
    assert session.provider == "openrouter"
    assert session.model == "custom/model"
    assert session.settings_snapshot["default_provider"] == "anthropic"
    assert session.settings_snapshot["resolved_selection"] == {
        "provider": "openrouter",
        "model": "custom/model",
    }
