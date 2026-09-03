"""Explicit LLM provider registry and construction."""

from __future__ import annotations

import shutil

from pydantic import BaseModel

from ricky.config import RickySettings
from ricky.llm.anthropic import AnthropicProvider
from ricky.llm.claude_code import ClaudeCodeProvider
from ricky.llm.openrouter import OpenRouterProvider
from ricky.llm.provider import Provider


class ProviderEntry(BaseModel):
    """Display and authentication metadata for a registered provider."""

    name: str
    title: str
    auth_hint: str


_ENTRIES = (
    ProviderEntry(
        name="openrouter",
        title="OpenRouter",
        auth_hint="set openrouter_api_key in the owning profile's .secrets.toml",
    ),
    ProviderEntry(
        name="anthropic",
        title="Anthropic",
        auth_hint="set anthropic_api_key in the owning profile's .secrets.toml",
    ),
    ProviderEntry(
        name="claude_code",
        title="Claude Code (local subscription)",
        auth_hint=(
            "claude CLI on PATH (or providers.claude_code.cli_path) "
            "and logged in via `claude` login"
        ),
    ),
)


def provider_names() -> list[str]:
    """Return registered provider names in display order."""
    return [entry.name for entry in _ENTRIES]


def provider_entries() -> list[ProviderEntry]:
    """Return registered provider metadata in display order."""
    return [entry.model_copy() for entry in _ENTRIES]


def auth_ready(name: str, settings: RickySettings) -> bool:
    """Return whether the provider's authentication mechanism is ready."""
    _require_known(name)
    if name == "openrouter":
        return settings.openrouter_api_key is not None
    if name == "anthropic":
        return settings.anthropic_api_key is not None
    return shutil.which(settings.providers.claude_code.cli_path) is not None


def create_provider(name: str, settings: RickySettings) -> Provider:
    """Create a provider by registered name."""
    _require_known(name)
    if name == "openrouter":
        return OpenRouterProvider(settings)
    if name == "anthropic":
        return AnthropicProvider(settings)
    return ClaudeCodeProvider(settings)


def create_default_provider(settings: RickySettings) -> Provider:
    """Create the configured default LLM provider."""
    return create_provider(settings.default_provider, settings)


def _require_known(name: str) -> None:
    if name not in provider_names():
        valid = ", ".join(provider_names())
        raise ValueError(f"Unknown provider {name!r}; valid providers: {valid}")
