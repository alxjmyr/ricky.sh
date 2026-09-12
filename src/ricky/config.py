"""Runtime configuration for ricky.

Configuration is file-backed. Settings resolve from, in order of
precedence (highest wins):

1. Explicit values passed to ``RickySettings(...)`` (used in tests).
2. Enabled profiles' ``ricky.toml`` and ``.secrets.toml`` files below
   ``user_data_dir/profiles/<name>``.
3. Installation-wide ``ricky.toml`` below ``user_data_dir``.
4. Field defaults.

The machine-local XDG bootstrap pointer selects ``user_data_dir`` after
``ricky init``. Before initialization, ``RICKY_USER_DATA_DIR`` may select that
root. Secrets live only in an owning profile's ``.secrets.toml``. See the
configuration and profile boundaries in ``.designs/architecture.md`` and
``.designs/profiles.md``.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import tomllib
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from ricky.installation import (
    fsync_directory,
    installation_operation_lock,
    require_compatible_installation,
    resolve_bootstrap_user_data_dir,
    write_private_file,
)
from ricky.profiles import (
    SHARED_PROFILE,
    ProfileName,
    ProfileResourceRef,
    ProfileScope,
    validate_profile_name,
)

CONFIG_FILENAME = "ricky.toml"
SECRETS_FILENAME = ".secrets.toml"


def validate_timezone_name(value: str) -> str:
    """Validate and return one IANA timezone name."""
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA timezone {value!r}") from exc
    return value


def _user_data_relative_path(value: str, *, setting: str) -> str:
    """Validate one configured path that must stay below ``user_data_dir``."""

    path = Path(value)
    if path.is_absolute() or value in {"", ".", ".."} or ".." in path.parts:
        raise ValueError(f"{setting} must stay below user_data_dir")
    return value


class ProviderSettings(BaseModel):
    """Shared shape of one provider's non-secret configuration table."""

    default_model: str


class OpenRouterSettings(ProviderSettings):
    """Non-secret OpenRouter configuration."""

    default_model: str = "anthropic/claude-sonnet-4"


class AnthropicSettings(ProviderSettings):
    """Non-secret Anthropic configuration."""

    default_model: str = "claude-sonnet-5"
    default_max_tokens: int = 8192


class ClaudeCodeSettings(ProviderSettings):
    """Non-secret Claude Code CLI configuration."""

    default_model: str = "sonnet"
    cli_path: str = "claude"
    resume_sessions: bool = True


class ProvidersSettings(BaseModel):
    """Typed settings for every registered provider (fields define valid names)."""

    openrouter: OpenRouterSettings = Field(default_factory=OpenRouterSettings)
    anthropic: AnthropicSettings = Field(default_factory=AnthropicSettings)
    claude_code: ClaudeCodeSettings = Field(default_factory=ClaudeCodeSettings)


class ProfileDefinitionSettings(BaseModel):
    """Typed routing, persona, and model policy for one named profile."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(default="", max_length=1_000)
    routing_hints: list[str] = Field(default_factory=list, max_length=100)
    default_provider: str | None = Field(default=None, min_length=1)
    default_models: dict[str, str] = Field(default_factory=dict)
    allowed_providers: list[str] = Field(default_factory=list)

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        return value.strip()

    @field_validator("routing_hints")
    @classmethod
    def _routing_hints(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("profile routing hints cannot be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError("profile routing hints must be unique")
        return normalized

    @field_validator("allowed_providers")
    @classmethod
    def _allowed_providers(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("allowed profile providers cannot be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError("allowed profile providers must be unique")
        return normalized


def _default_profile_definitions() -> dict[str, ProfileDefinitionSettings]:
    return {
        SHARED_PROFILE: ProfileDefinitionSettings(
            description="Context and resources intentionally available in every Ricky session.",
            routing_hints=["Cross-context preferences and universally applicable user facts."],
        ),
        "personal": ProfileDefinitionSettings(
            description="The user's personal life, accounts, responsibilities, and preferences.",
            routing_hints=["Family, home, personal finance, health, and non-work commitments."],
        ),
        "work": ProfileDefinitionSettings(
            description="The user's employer and professional work context.",
            routing_hints=["Employer data, coworkers, work accounts, and professional tasks."],
            default_provider="claude_code",
            allowed_providers=["claude_code"],
        ),
    }


class ProfilesSettings(BaseModel):
    """Installation profile registry and typed per-profile policy."""

    model_config = ConfigDict(extra="forbid")

    default: str = "personal"
    enabled: list[str] = Field(default_factory=lambda: ["shared", "personal", "work"])
    definitions: dict[str, ProfileDefinitionSettings] = Field(
        default_factory=_default_profile_definitions
    )

    @model_validator(mode="before")
    @classmethod
    def _merge_builtin_definitions(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        merged = dict(value)
        raw_enabled = merged.get("enabled", ["shared", "personal", "work"])
        if not isinstance(raw_enabled, list):
            return value
        enabled = [str(item) for item in raw_enabled]
        if SHARED_PROFILE not in enabled:
            enabled.append(SHARED_PROFILE)
        defaults = _default_profile_definitions()
        definitions: dict[str, object] = {
            name: definition.model_dump(mode="python")
            for name, definition in defaults.items()
            if name in enabled
        }
        configured = merged.get("definitions", {})
        if isinstance(configured, dict):
            for name, definition in configured.items():
                observed = definitions.get(name, {})
                if isinstance(observed, dict) and isinstance(definition, dict):
                    definitions[name] = _merge_profile_documents(observed, definition)
                else:
                    definitions[name] = definition
        merged["definitions"] = definitions
        return merged

    @field_validator("default")
    @classmethod
    def _default_profile(cls, value: str) -> str:
        return validate_profile_name(value)

    @field_validator("enabled")
    @classmethod
    def _enabled_profiles(cls, values: list[str]) -> list[str]:
        normalized = [validate_profile_name(value) for value in values]
        if SHARED_PROFILE not in normalized:
            normalized.append(SHARED_PROFILE)
        if len(normalized) != len(set(normalized)):
            raise ValueError("enabled profiles must be unique")
        return sorted(normalized, key=lambda item: (item != SHARED_PROFILE, item))

    @model_validator(mode="after")
    def _complete_registry(self) -> ProfilesSettings:
        if self.default not in self.enabled:
            raise ValueError("default profile must be enabled")
        missing = sorted(set(self.enabled) - set(self.definitions))
        if missing:
            raise ValueError("enabled profiles require definitions: " + ", ".join(missing))
        unknown = sorted(set(self.definitions) - set(self.enabled))
        if unknown:
            raise ValueError("profile definitions must be enabled: " + ", ".join(unknown))
        return self


class ModelContextProfile(BaseModel):
    """Exact configured capacity for one provider/model pair."""

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    context_window_tokens: int = Field(ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    image_token_estimate: int | None = Field(default=None, ge=1)


class ContextMediaSettings(BaseModel):
    """Session media storage and provider-request projection policy."""

    model_config = ConfigDict(extra="forbid")

    session_byte_limit: int = Field(default=25_000_000, ge=1, le=1_000_000_000)
    request_image_limit: int = Field(default=2, ge=1, le=20)
    request_image_byte_limit: int = Field(default=10_000_000, ge=1, le=100_000_000)
    request_image_pixel_limit: int = Field(default=8_000_000, ge=1, le=100_000_000)
    default_image_token_estimate: int = Field(default=8_192, ge=1, le=1_000_000)


class ToolResultContextSettings(BaseModel):
    """Lossless large tool-result storage and projection policy."""

    enabled: bool = True
    offload_threshold_chars: int = Field(default=12_000, ge=1)
    inline_excerpt_chars: int = Field(default=8_000, ge=1)
    head_fraction: float = Field(default=0.7, ge=0.0, le=1.0)
    artifact_max_chars: int = Field(default=2_000_000, ge=1)
    session_artifact_max_chars: int = Field(default=20_000_000, ge=1)
    read_chunk_chars: int = Field(default=12_000, ge=1)

    @model_validator(mode="after")
    def _validate_limits(self) -> ToolResultContextSettings:
        if self.inline_excerpt_chars > self.offload_threshold_chars:
            raise ValueError(
                "context.tool_results.inline_excerpt_chars must not exceed offload_threshold_chars"
            )
        if self.artifact_max_chars < self.offload_threshold_chars:
            raise ValueError(
                "context.tool_results.artifact_max_chars must be at least offload_threshold_chars"
            )
        if self.session_artifact_max_chars < self.artifact_max_chars:
            raise ValueError(
                "context.tool_results.session_artifact_max_chars must be at least "
                "artifact_max_chars"
            )
        if self.read_chunk_chars > self.artifact_max_chars:
            raise ValueError(
                "context.tool_results.read_chunk_chars must not exceed artifact_max_chars"
            )
        return self


class ContextCompactionSettings(BaseModel):
    """Policy for explicit, tool-free semantic context compaction."""

    enabled: bool = True
    keep_recent_tokens: int = Field(default=16_000, ge=1)
    max_summary_tokens: int = Field(default=4_096, ge=1)
    max_focus_chars: int = Field(default=2_000, ge=1)
    max_summary_chars: int = Field(default=24_000, ge=1)


class ContextSettings(BaseModel):
    """Deterministic context estimation and capacity policy."""

    chars_per_token: float = Field(default=4.0, gt=0)
    response_reserve_tokens: int = Field(default=8_192, ge=0)
    safety_margin_tokens: int = Field(default=1_024, ge=0)
    models: list[ModelContextProfile] = Field(default_factory=list, max_length=100)
    media: ContextMediaSettings = Field(default_factory=ContextMediaSettings)
    tool_results: ToolResultContextSettings = Field(default_factory=ToolResultContextSettings)
    compaction: ContextCompactionSettings = Field(default_factory=ContextCompactionSettings)

    @model_validator(mode="after")
    def _unique_models(self) -> ContextSettings:
        keys = [(profile.provider, profile.model) for profile in self.models]
        if len(keys) != len(set(keys)):
            raise ValueError("context.models provider/model pairs must be unique")
        return self


class GoogleAccountSettings(BaseModel):
    """Expected identity for one named Google account."""

    email: str


class GoogleSettings(BaseModel):
    """Shared non-secret OAuth settings for Google integrations."""

    auth_base_url: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url: str = "https://oauth2.googleapis.com/token"
    userinfo_url: str = "https://openidconnect.googleapis.com/v1/userinfo"
    token_store_path: str = "google/tokens.json"
    auth_callback_timeout_seconds: float = 120.0
    accounts: dict[str, GoogleAccountSettings] = Field(default_factory=dict)

    @field_validator("token_store_path")
    @classmethod
    def _token_store_below_user_data(cls, value: str) -> str:
        return _user_data_relative_path(value, setting="google.token_store_path")


class GoogleOAuthClientSettings(BaseModel):
    """Secret Desktop OAuth credentials for one named Google account."""

    client_id: str
    client_secret: SecretStr


class GmailSettings(BaseModel):
    """Non-secret Gmail integration configuration."""

    download_dir: str = "downloads/gmail"
    default_list_limit: int = Field(default=25, ge=1, le=50)
    body_char_limit: int = Field(default=20_000, ge=1_000)
    attachment_count_limit: int = Field(default=10, ge=1, le=50)
    attachment_file_byte_limit: int = Field(default=20_000_000, ge=1, le=25_000_000)
    attachment_total_byte_limit: int = Field(default=20_000_000, ge=1, le=25_000_000)
    api_base_url: str = "https://gmail.googleapis.com"

    @field_validator("download_dir")
    @classmethod
    def _downloads_below_user_data(cls, value: str) -> str:
        return _user_data_relative_path(value, setting="gmail.download_dir")

    @model_validator(mode="after")
    def _validate_attachment_limits(self) -> GmailSettings:
        if self.attachment_total_byte_limit < self.attachment_file_byte_limit:
            raise ValueError(
                "gmail.attachment_total_byte_limit must be at least attachment_file_byte_limit"
            )
        return self


class GcalSettings(BaseModel):
    """Non-secret Google Calendar integration configuration."""

    api_base_url: str = "https://www.googleapis.com/calendar/v3"
    default_list_limit: int = Field(default=25, ge=1, le=50)
    default_window_days: int = Field(default=7, ge=1, le=365)
    description_char_limit: int = Field(default=20_000, ge=1_000)


class SlackSettings(BaseModel):
    """Non-secret Slack integration configuration."""

    download_dir: str = "downloads/slack"
    default_history_limit: int = 30
    api_base_url: str = "https://slack.com/api"
    # Slack returns unread counts on conversations.info for DMs only. Every
    # other kind is counted from conversations.history, so the count saturates
    # at this many messages per conversation.
    unread_probe_limit: int = Field(default=50, ge=1, le=200)
    # The unread probe costs one or two API calls per conversation. Cap the
    # fan-out and probe the most recently active conversations first.
    unread_max_conversations: int = Field(default=200, ge=1, le=1_000)
    attachment_count_limit: int = Field(default=10, ge=1, le=20)
    attachment_file_byte_limit: int = Field(default=20_000_000, ge=1, le=100_000_000)
    attachment_total_byte_limit: int = Field(default=50_000_000, ge=1, le=200_000_000)

    @field_validator("download_dir")
    @classmethod
    def _downloads_below_user_data(cls, value: str) -> str:
        return _user_data_relative_path(value, setting="slack.download_dir")

    @model_validator(mode="after")
    def _validate_attachment_limits(self) -> SlackSettings:
        if self.attachment_total_byte_limit < self.attachment_file_byte_limit:
            raise ValueError(
                "slack.attachment_total_byte_limit must be at least attachment_file_byte_limit"
            )
        return self


class WebSearchBudgetSettings(BaseModel):
    """Validated retrieval and rendering ceilings for one search effort."""

    candidate_count: int = Field(ge=1, le=50)
    source_limit: int = Field(ge=1, le=50)
    context_token_limit: int = Field(ge=1_024, le=32_768)
    snippet_limit: int = Field(ge=1, le=100)
    tokens_per_source: int = Field(ge=512, le=8_192)
    snippets_per_source: int = Field(ge=1, le=100)
    result_char_limit: int = Field(ge=1_000, le=11_500)
    relevance_mode: Literal["strict", "balanced"]


def _quick_web_search_budget() -> WebSearchBudgetSettings:
    return WebSearchBudgetSettings(
        candidate_count=5,
        source_limit=3,
        context_token_limit=2_048,
        snippet_limit=8,
        tokens_per_source=1_024,
        snippets_per_source=3,
        result_char_limit=6_000,
        relevance_mode="strict",
    )


def _standard_web_search_budget() -> WebSearchBudgetSettings:
    return WebSearchBudgetSettings(
        candidate_count=15,
        source_limit=6,
        context_token_limit=4_096,
        snippet_limit=18,
        tokens_per_source=1_536,
        snippets_per_source=5,
        result_char_limit=9_500,
        relevance_mode="balanced",
    )


def _deep_web_search_budget() -> WebSearchBudgetSettings:
    return WebSearchBudgetSettings(
        candidate_count=30,
        source_limit=10,
        context_token_limit=8_192,
        snippet_limit=36,
        tokens_per_source=2_048,
        snippets_per_source=6,
        result_char_limit=11_500,
        relevance_mode="balanced",
    )


class WebSearchEffortSettings(BaseModel):
    """Explicit profiles available to the model-facing search tool."""

    quick: WebSearchBudgetSettings = Field(default_factory=_quick_web_search_budget)
    standard: WebSearchBudgetSettings = Field(default_factory=_standard_web_search_budget)
    deep: WebSearchBudgetSettings = Field(default_factory=_deep_web_search_budget)


class BraveWebSearchSettings(BaseModel):
    """Non-secret settings for the Brave LLM Context adapter."""

    api_base_url: str = "https://api.search.brave.com"


class WebSearchProviderSettings(BaseModel):
    """Typed settings for every registered Web search provider."""

    brave: BraveWebSearchSettings = Field(default_factory=BraveWebSearchSettings)


class WebSearchSettings(BaseModel):
    """Provider selection, locale, retries, and fixed effort profiles."""

    provider: str = "brave"
    country: str = "US"
    search_language: str = "en"
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    read_retry_limit: int = Field(default=1, ge=0)
    max_retry_delay_seconds: float = Field(default=2.0, ge=0)
    download_dir: str = "downloads/web"
    efforts: WebSearchEffortSettings = Field(default_factory=WebSearchEffortSettings)
    providers: WebSearchProviderSettings = Field(default_factory=WebSearchProviderSettings)

    @field_validator("download_dir")
    @classmethod
    def _downloads_below_user_data(cls, value: str) -> str:
        return _user_data_relative_path(value, setting="web_search.download_dir")


class WorkflowSettings(BaseModel):
    """Workflow runner defaults and load-time limits."""

    enabled: bool = True
    instruction_char_limit: int = Field(default=8_000, ge=500)
    max_parallel_steps: int = Field(default=4, ge=1)
    max_parallel_items: int = Field(default=4, ge=1)
    max_binding_chars: int = Field(default=120_000, ge=1_000)
    max_result_chars: int = Field(default=120_000, ge=1_000)
    max_schema_depth: int = Field(default=12, ge=1, le=64)
    max_foreach_items: int = Field(default=100, ge=1)
    model_attempts: int = Field(default=2, ge=1)
    agent_iterations: int = Field(default=8, ge=1)
    run_dir: str = Field(default="workflow-runs", min_length=1)

    @model_validator(mode="after")
    def _validate_run_dir(self) -> WorkflowSettings:
        path = Path(self.run_dir)
        if path.is_absolute() or self.run_dir in {".", ".."} or ".." in path.parts:
            raise ValueError("workflow.run_dir must stay below its configured data root")
        return self


class MemorySettings(BaseModel):
    """Persistent memory routing and context limits."""

    enabled: bool = True
    index_char_limit: int = Field(default=8_000, ge=1_000)
    recall_char_limit: int = Field(default=12_000, ge=1_000)
    recall_note_limit: int = Field(default=10, ge=1, le=50)
    note_body_char_limit: int = Field(default=8_000, ge=500)


class DurableTaskSettings(BaseModel):
    """User-global durable-task storage and operation limits."""

    dir: str = Field(default="tasks", min_length=1)
    lease_seconds: int = Field(default=900, ge=1, le=86_400)
    search_limit: int = Field(default=50, ge=1, le=500)
    activity_limit: int = Field(default=50, ge=1, le=500)
    artifact_read_char_limit: int = Field(default=50_000, ge=1_000, le=2_000_000)
    artifact_write_char_limit: int = Field(default=200_000, ge=1_000, le=5_000_000)
    artifact_file_byte_limit: int = Field(default=20_000_000, ge=4_000, le=100_000_000)
    artifact_list_byte_limit: int = Field(default=50_000_000, ge=4_000, le=500_000_000)
    artifact_list_limit: int = Field(default=500, ge=1, le=5_000)
    sqlite_busy_timeout_ms: int = Field(default=5_000, ge=1, le=60_000)

    @model_validator(mode="after")
    def _validate_dir(self) -> DurableTaskSettings:
        path = Path(self.dir)
        if path.is_absolute() or self.dir in {".", ".."} or ".." in path.parts:
            raise ValueError("durable_tasks.dir must stay below user_data_dir")
        if self.artifact_file_byte_limit < self.artifact_write_char_limit * 4:
            raise ValueError(
                "durable_tasks.artifact_file_byte_limit must allow the maximum "
                "UTF-8 size of artifact_write_char_limit"
            )
        if self.artifact_list_byte_limit < self.artifact_file_byte_limit:
            raise ValueError(
                "durable_tasks.artifact_list_byte_limit must be at least artifact_file_byte_limit"
            )
        return self


class SessionSettings(BaseModel):
    """User-global persistent conversation storage and turn limits."""

    store_path: str = Field(default="sessions/sessions.sqlite3", min_length=1)
    sqlite_busy_timeout_ms: int = Field(default=5_000, ge=1, le=60_000)
    lease_seconds: int = Field(default=300, ge=1, le=3_600)
    turn_wall_seconds: float = Field(default=300.0, gt=0, le=3_600)
    turn_retention: int = Field(default=1_000, ge=1, le=100_000)

    @model_validator(mode="after")
    def _validate_store_path(self) -> SessionSettings:
        path = Path(self.store_path)
        if (
            path.is_absolute()
            or self.store_path in {".", ".."}
            or ".." in path.parts
            or path.name != "sessions.sqlite3"
        ):
            raise ValueError(
                "sessions.store_path must be a confined relative path ending in sessions.sqlite3"
            )
        if self.turn_wall_seconds + 30 > 3_600:
            raise ValueError(
                "sessions.turn_wall_seconds must leave 30 seconds of inbox-claim headroom"
            )
        return self


class MessagingTransportSettings(BaseModel):
    """One configured transport account available to logical routes."""

    type: Literal["telegram", "discord"]
    account: str = Field(min_length=1, max_length=100)

    @field_validator("account")
    @classmethod
    def _trim_account(cls, value: str) -> str:
        return ProfileResourceRef.from_qualified(value).qualified

    @property
    def account_ref(self) -> ProfileResourceRef:
        """Return the credential owner and local account name."""

        return ProfileResourceRef.from_qualified(self.account)


class TelegramAccountSettings(BaseModel):
    """One authenticated Telegram bot account and its inbound trust boundary."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    bot_token: SecretStr
    api_base_url: str = Field(default="https://api.telegram.org", min_length=1, max_length=500)
    long_poll_timeout_seconds: int = Field(default=30, ge=0, le=50)
    allowed_sender_ids: list[str] = Field(default_factory=list)
    allowed_destination_ids: list[str] = Field(default_factory=list)
    max_inbound_text_length: int = Field(default=8_000, ge=1, le=20_000)
    enabled: bool = True

    @field_validator("api_base_url")
    @classmethod
    def _validate_api_base_url(cls, value: str) -> str:
        value = value.rstrip("/")
        if not value.startswith(("https://", "http://")):
            raise ValueError("Telegram api_base_url must be an HTTP(S) URL")
        return value

    @field_validator("allowed_sender_ids", "allowed_destination_ids")
    @classmethod
    def _validate_telegram_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value or not value.removeprefix("-").isdigit() for value in normalized):
            raise ValueError("Telegram allowlists must contain decimal platform ids")
        if len(normalized) != len(set(normalized)):
            raise ValueError("Telegram allowlists must not contain duplicates")
        return normalized


class MessagingRouteSettings(BaseModel):
    """One static logical route to a configured transport destination."""

    transport: str = Field(min_length=1, max_length=100)
    destination: str = Field(min_length=1, max_length=500)
    owner_profile: ProfileName
    accepted_profiles: list[ProfileName] = Field(min_length=1)

    @field_validator("transport", "destination")
    @classmethod
    def _trim_route_value(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("messaging route values cannot be empty")
        return value

    @field_validator("owner_profile")
    @classmethod
    def _owner_profile(cls, value: str) -> str:
        return validate_profile_name(value)

    @field_validator("accepted_profiles")
    @classmethod
    def _accepted_profiles(cls, values: list[str]) -> list[str]:
        normalized = [validate_profile_name(value) for value in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError("messaging route accepted_profiles must be unique")
        return sorted(normalized, key=lambda item: (item != SHARED_PROFILE, item))

    @model_validator(mode="after")
    def _owner_has_clearance(self) -> MessagingRouteSettings:
        if self.owner_profile not in self.accepted_profiles:
            raise ValueError("messaging route owner_profile must be accepted by the route")
        return self


class MessagingSettings(BaseModel):
    """Platform-neutral notification routes, storage, and delivery limits."""

    store_path: str = Field(default="notifications/notifications.sqlite3", min_length=1)
    sqlite_busy_timeout_ms: int = Field(default=5_000, ge=1, le=60_000)
    lease_seconds: int = Field(default=60, ge=1, le=3_600)
    delivery_attempt_limit: int = Field(default=3, ge=1, le=20)
    title_char_limit: int = Field(default=200, ge=1, le=500)
    body_char_limit: int = Field(default=8_000, ge=100, le=20_000)
    attachment_dir: str = Field(default="notifications/attachments", min_length=1)
    attachment_count_limit: int = Field(default=10, ge=1, le=20)
    attachment_file_byte_limit: int = Field(default=20_000_000, ge=1, le=50_000_000)
    attachment_total_byte_limit: int = Field(default=50_000_000, ge=1, le=100_000_000)
    transports: dict[str, MessagingTransportSettings] = Field(default_factory=dict)
    telegram_accounts: dict[str, TelegramAccountSettings] = Field(default_factory=dict)
    routes: dict[str, MessagingRouteSettings] = Field(default_factory=dict)
    agent_routes: list[str] = Field(default_factory=list)
    job_route: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _validate_messaging(self) -> MessagingSettings:
        path = Path(self.store_path)
        if (
            path.is_absolute()
            or self.store_path in {".", ".."}
            or ".." in path.parts
            or path.name != "notifications.sqlite3"
        ):
            raise ValueError(
                "messaging.store_path must be a confined relative path ending in "
                "notifications.sqlite3"
            )
        attachment_dir = Path(self.attachment_dir)
        if (
            attachment_dir.is_absolute()
            or self.attachment_dir in {".", ".."}
            or ".." in attachment_dir.parts
        ):
            raise ValueError("messaging.attachment_dir must stay below user_data_dir")
        if self.attachment_total_byte_limit < self.attachment_file_byte_limit:
            raise ValueError(
                "messaging.attachment_total_byte_limit must be at least attachment_file_byte_limit"
            )
        for name, transport in self.transports.items():
            if not name.strip() or len(name) > 100:
                raise ValueError("messaging transport names must contain 1 to 100 characters")
            if transport.type not in {"telegram", "discord"}:
                raise ValueError(f"messaging transport {name!r} has an unregistered type")
        for name in self.telegram_accounts:
            if ProfileResourceRef.from_qualified(name).qualified != name:
                raise ValueError("Telegram account keys must use canonical profile/name form")
        for name, route in self.routes.items():
            if not name.strip() or len(name) > 200 or name.startswith("conversation:"):
                raise ValueError(
                    "static messaging route names must contain 1 to 200 characters and "
                    "must not use the reserved conversation: prefix"
                )
            if route.transport not in self.transports:
                raise ValueError(
                    f"messaging route {name!r} names unknown transport {route.transport!r}"
                )
        if len(self.agent_routes) != len(set(self.agent_routes)):
            raise ValueError("messaging.agent_routes must be unique")
        unknown_agent_routes = sorted(set(self.agent_routes) - set(self.routes))
        if unknown_agent_routes:
            raise ValueError(
                "messaging.agent_routes contains unknown static route(s): "
                + ", ".join(unknown_agent_routes)
            )
        if self.job_route is not None and self.job_route not in self.routes:
            raise ValueError("messaging.job_route must name a configured static route")
        return self


class ProfileMessagingSettings(BaseModel):
    """Profile-owned messaging credentials available to an issued scope."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    telegram_accounts: dict[str, TelegramAccountSettings] = Field(default_factory=dict)

    @field_validator("telegram_accounts")
    @classmethod
    def _account_names(
        cls,
        accounts: dict[str, TelegramAccountSettings],
    ) -> dict[str, TelegramAccountSettings]:
        for name in accounts:
            if not name.strip() or len(name) > 100:
                raise ValueError("Telegram account names must contain 1 to 100 characters")
        return accounts


_CAPABILITY_ID_PATTERN = r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+$"


class AgentCapabilityPolicySettings(BaseModel):
    """Standing eligibility and live-review policy for one agent class.

    These lists contain no task-specific authority or guardrail values. They
    only determine whether an installed capability may be proposed and which
    authenticated live-review gates must complete before it is selected.
    """

    model_config = ConfigDict(extra="forbid")

    exclude_capabilities: list[str] = Field(default_factory=list)
    confirmation_required_capabilities: list[str] = Field(default_factory=list)
    guardrail_required_capabilities: list[str] = Field(default_factory=list)

    @field_validator(
        "exclude_capabilities",
        "confirmation_required_capabilities",
        "guardrail_required_capabilities",
    )
    @classmethod
    def _capability_ids(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("capability policy lists must not contain duplicates")
        for value in cleaned:
            if re.fullmatch(_CAPABILITY_ID_PATTERN, value) is None:
                raise ValueError(f"invalid capability id: {value}")
        return cleaned

    def digest(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AdHocExecutionPolicySettings(BaseModel):
    """Hard default runtime ceilings for newly compiled ad hoc contracts."""

    model_config = ConfigDict(extra="forbid")

    wall_clock_seconds: float = Field(default=600.0, gt=0, le=86_400)
    iterations: int = Field(default=10, ge=1, le=100)
    max_completion_tokens_per_request: int = Field(default=4_096, ge=1, le=1_000_000)
    effect_calls: int = Field(default=0, ge=0, le=1_000)


class AdHocBackgroundSettings(AgentCapabilityPolicySettings):
    execution: AdHocExecutionPolicySettings = Field(default_factory=AdHocExecutionPolicySettings)


class AgentClassSettings(BaseModel):
    """Independently configurable foreground and ad hoc-background policy."""

    model_config = ConfigDict(extra="forbid")

    gateway_foreground: AgentCapabilityPolicySettings = Field(
        default_factory=AgentCapabilityPolicySettings
    )
    ad_hoc_background: AdHocBackgroundSettings = Field(default_factory=AdHocBackgroundSettings)


class GatewayRouteSettings(BaseModel):
    """One pinned foreground-agent route for a trusted messaging route."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=500)
    primary_profile: ProfileName
    access_profiles: list[ProfileName] = Field(default_factory=list, max_length=20)
    project_root: str | None = Field(default=None, max_length=2_000)
    exclude_capabilities: list[str] = Field(default_factory=list)
    confirmation_required_capabilities: list[str] = Field(default_factory=list)
    guardrail_required_capabilities: list[str] = Field(default_factory=list)

    @field_validator("provider", "model")
    @classmethod
    def _trim_gateway_selection(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("gateway provider and model cannot be empty")
        return value

    @field_validator("access_profiles")
    @classmethod
    def _unique_access_profiles(cls, values: list[str]) -> list[str]:
        normalized = [validate_profile_name(value) for value in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError("gateway access_profiles must be unique")
        return normalized

    def profile_scope(self) -> ProfileScope:
        """Return the immutable profile scope pinned by this route."""

        return ProfileScope.create(
            self.primary_profile,
            access_profiles=self.access_profiles,
        )

    @field_validator("project_root")
    @classmethod
    def _validate_gateway_project_root(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("gateway project_root must be a path or omitted for no project")
        return value

    @field_validator(
        "exclude_capabilities",
        "confirmation_required_capabilities",
        "guardrail_required_capabilities",
    )
    @classmethod
    def _route_capability_ids(cls, values: list[str]) -> list[str]:
        return AgentCapabilityPolicySettings._capability_ids(values)


class GatewayRetentionSettings(BaseModel):
    """Typed pruning ceilings for finished, unreferenced gateway evidence.

    Every limit counts *terminal* records only. Retention never
    considers an open, queued, leased, uncertain, or in-doubt record, so a
    limit of zero still cannot delete unresolved responsibility.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    min_age_seconds: float = Field(default=604_800.0, ge=0, le=31_536_000)
    inbound_messages: int = Field(default=5_000, ge=0, le=1_000_000)
    turn_results: int = Field(default=5_000, ge=0, le=1_000_000)
    archived_conversations: int = Field(default=500, ge=0, le=100_000)
    execution_requests: int = Field(default=1_000, ge=0, le=100_000)
    notifications: int = Field(default=5_000, ge=0, le=1_000_000)
    health_errors: int = Field(default=200, ge=0, le=10_000)
    log_byte_limit: int = Field(default=10_000_000, ge=10_000, le=1_000_000_000)
    log_file_limit: int = Field(default=5, ge=1, le=100)


class GatewayServiceSettings(BaseModel):
    """Managed POSIX ``systemd --user`` unit generation limits.

    Ricky renders and installs exactly one marked unit. It never writes model
    authored unit content and never places a secret on the command line.
    """

    model_config = ConfigDict(extra="forbid")

    unit_name: str = Field(default="ricky-gateway.service", min_length=5, max_length=100)
    unit_dir: str | None = Field(
        default=None,
        min_length=1,
        validate_default=True,
        description="Managed systemd user unit directory; defaults to the XDG user location.",
    )
    description: str = Field(default="Ricky foreground gateway", min_length=1, max_length=200)
    restart_seconds: float = Field(default=5.0, ge=1, le=300)
    start_timeout_seconds: float = Field(default=60.0, ge=5, le=3_600)
    stop_timeout_seconds: float = Field(default=60.0, ge=5, le=3_600)
    log_dir: str = Field(default="logs", min_length=1)

    @field_validator("unit_name")
    @classmethod
    def _validate_unit_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.service", value):
            raise ValueError("gateway.service.unit_name must be a simple *.service file name")
        return value

    @field_validator("unit_dir", mode="after")
    @classmethod
    def _resolve_unit_dir(cls, value: str | None) -> str:
        if value is None:
            config_home = os.environ.get("XDG_CONFIG_HOME")
            root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
            path = root / "systemd" / "user"
        else:
            path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("gateway.service.unit_dir must resolve to an absolute path")
        return str(path.resolve())

    @field_validator("log_dir")
    @classmethod
    def _validate_log_dir(cls, value: str) -> str:
        return _user_data_relative_path(value, setting="gateway.service.log_dir")


class GatewaySettings(BaseModel):
    """Foreground gateway storage, routing, concurrency, and context bounds."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    store_path: str = Field(default="gateway/gateway.sqlite3", min_length=1)
    sqlite_busy_timeout_ms: int = Field(default=5_000, ge=1, le=60_000)
    concurrency: int = Field(default=4, ge=1, le=100)
    inbox_poll_seconds: float = Field(default=0.25, gt=0, le=300)
    maintenance_seconds: float = Field(default=30.0, gt=0, le=3_600)
    recent_activity_limit: int = Field(default=10, ge=0, le=100)
    activity_char_limit: int = Field(default=12_000, ge=500, le=100_000)
    operator_route: str | None = Field(default=None, min_length=1, max_length=200)
    lock_path: str = Field(default="gateway/gateway.lock", min_length=1)
    """Host-local single-instance lock file. One gateway per user_data_dir."""
    startup_recovery: bool = True
    """Run the deterministic Section 3 recovery sweep before any loop starts."""
    recent_error_limit: int = Field(default=20, ge=0, le=500)
    routes: dict[str, GatewayRouteSettings] = Field(default_factory=dict)
    retention: GatewayRetentionSettings = Field(default_factory=GatewayRetentionSettings)
    service: GatewayServiceSettings = Field(default_factory=GatewayServiceSettings)

    @model_validator(mode="after")
    def _validate_gateway(self) -> GatewaySettings:
        path = Path(self.store_path)
        if (
            path.is_absolute()
            or self.store_path in {".", ".."}
            or ".." in path.parts
            or path.name != "gateway.sqlite3"
        ):
            raise ValueError(
                "gateway.store_path must be a confined relative path ending in gateway.sqlite3"
            )
        lock = Path(self.lock_path)
        if (
            lock.is_absolute()
            or self.lock_path in {".", ".."}
            or ".." in lock.parts
            or lock.suffix != ".lock"
        ):
            raise ValueError("gateway.lock_path must be a confined relative path ending in .lock")
        for name, route in self.routes.items():
            if not name.strip() or len(name) > 200 or name.startswith("conversation:"):
                raise ValueError("gateway route names must contain 1 to 200 characters")
            if route.provider not in ProvidersSettings.model_fields:
                raise ValueError(f"gateway route {name!r} uses unknown provider {route.provider!r}")
        return self


class JobSettings(BaseModel):
    """User-global storage and retention limits for ephemeral agent runs."""

    spec_dir: str = Field(default="jobs", min_length=1)
    run_dir: str = Field(default="agent-runs", min_length=1)
    transcript_retention: int = Field(default=100, ge=1, le=10_000)
    sqlite_busy_timeout_ms: int = Field(default=5_000, ge=1, le=60_000)
    candidate_limit: int = Field(default=100, ge=1, le=500)
    history_summary_limit: int = Field(default=10, ge=0, le=100)
    batch_retention: int = Field(default=100, ge=1, le=10_000)
    source_item_text_limit: int = Field(default=12_000, ge=500, le=100_000)

    @model_validator(mode="after")
    def _validate_relative_dirs(self) -> JobSettings:
        for field_name in ("spec_dir", "run_dir"):
            value = getattr(self, field_name)
            path = Path(value)
            if path.is_absolute() or value in {".", ".."} or ".." in path.parts:
                raise ValueError(f"jobs.{field_name} must stay below its configured root")
        return self


class ExecutionSettings(BaseModel):
    """User-global durable execution queue and worker limits."""

    store_path: str = Field(default="executions/executions.sqlite3", min_length=1)
    contract_snapshot_dir: str = Field(default="executions/contracts", min_length=1)
    draft_ttl_seconds: float = Field(default=3_600.0, gt=0, le=604_800)
    confirmation_ttl_seconds: float = Field(default=900.0, gt=0, le=86_400)
    source_snapshot_chars: int = Field(default=4_000, ge=100, le=20_000)
    claim_seconds: float = Field(default=120.0, gt=0, le=86_400)
    heartbeat_seconds: float = Field(default=30.0, gt=0, le=3_600)
    concurrency: int = Field(default=2, ge=1, le=100)
    poll_seconds: float = Field(default=2.0, gt=0, le=300)
    result_text_limit: int = Field(default=4_000, ge=100, le=100_000)
    retention: int = Field(default=1_000, ge=1, le=100_000)
    sqlite_busy_timeout_ms: int = Field(default=5_000, ge=1, le=60_000)

    @model_validator(mode="after")
    def _validate_paths_and_heartbeat(self) -> ExecutionSettings:
        for field_name in (
            "store_path",
            "contract_snapshot_dir",
        ):
            value = getattr(self, field_name)
            path = Path(value)
            if path.is_absolute() or value in {".", ".."} or ".." in path.parts:
                raise ValueError(f"executions.{field_name} must stay below its configured root")
        if self.heartbeat_seconds >= self.claim_seconds:
            raise ValueError("executions.heartbeat_seconds must be shorter than claim_seconds")
        return self


class AuthorityCapabilitySettings(BaseModel):
    """One owner ceiling for a single delegable effect capability."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    max_effect_calls: int = Field(default=1, ge=0, le=100)
    max_ttl_seconds: float = Field(default=86_400.0, gt=0, le=2_592_000)
    max_financial_limit_minor: int = Field(default=0, ge=0, le=100_000_000)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    allowed_profiles: list[ProfileName] = Field(
        default_factory=lambda: [SHARED_PROFILE, "personal"]
    )

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().upper()
        if not value.isalpha():
            raise ValueError("authority currency must be an ISO 4217 alphabetic code")
        return value

    @field_validator("allowed_profiles")
    @classmethod
    def _profiles(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("authority allowed_profiles must be unique")
        return [validate_profile_name(item) for item in value]

    @model_validator(mode="after")
    def _money_requires_currency(self) -> AuthorityCapabilitySettings:
        if self.enabled and not self.allowed_profiles:
            raise ValueError("an enabled delegable capability must allow at least one profile")
        if self.max_financial_limit_minor > 0 and self.currency is None:
            raise ValueError("a financial ceiling requires an explicit currency")
        return self


class AuthoritySettings(BaseModel):
    """Owner ceiling for task-scoped delegated authority."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    store_path: str = Field(default="authority/authority.sqlite3", min_length=1)
    sqlite_busy_timeout_ms: int = Field(default=5_000, ge=1, le=60_000)
    allowed_principals: list[str] = Field(default_factory=list, max_length=100)
    default_ttl_seconds: float = Field(default=3_600.0, gt=0, le=2_592_000)
    max_ttl_seconds: float = Field(default=86_400.0, gt=0, le=2_592_000)
    max_effect_calls: int = Field(default=1, ge=0, le=100)
    source_snapshot_chars: int = Field(default=2_000, ge=100, le=20_000)
    capabilities: dict[str, AuthorityCapabilitySettings] = Field(default_factory=dict)

    @field_validator("allowed_principals")
    @classmethod
    def _principals(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item or len(item) > 500 for item in cleaned):
            raise ValueError("authority principals must contain 1 to 500 characters")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("authority allowed_principals must be unique")
        return cleaned

    @model_validator(mode="after")
    def _validate_authority(self) -> AuthoritySettings:
        path = Path(self.store_path)
        if (
            path.is_absolute()
            or self.store_path in {".", ".."}
            or ".." in path.parts
            or path.name != "authority.sqlite3"
        ):
            raise ValueError(
                "authority.store_path must be a confined relative path ending in authority.sqlite3"
            )
        if self.default_ttl_seconds > self.max_ttl_seconds:
            raise ValueError("authority.default_ttl_seconds cannot exceed max_ttl_seconds")
        for name, capability in self.capabilities.items():
            if not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,63}", name):
                raise ValueError(f"invalid delegable capability name: {name}")
            if capability.max_effect_calls > self.max_effect_calls:
                raise ValueError(
                    f"capability '{name}' effect ceiling exceeds authority.max_effect_calls"
                )
            if capability.max_ttl_seconds > self.max_ttl_seconds:
                raise ValueError(
                    f"capability '{name}' TTL ceiling exceeds authority.max_ttl_seconds"
                )
        return self

    def digest(self) -> str:
        """Stable digest of the exact owner ceiling a grant is issued under."""

        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ScheduleSettings(BaseModel):
    """Static operational limits for the managed cron adapter."""

    launcher_log_byte_limit: int = Field(default=1_000_000, ge=4_096, le=100_000_000)


class _BrowserResourceSettings(BaseModel):
    """Profile-authored non-secret browser resource metadata."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, strict=True)

    description: str = Field(min_length=1, max_length=500)

    @field_validator("description")
    @classmethod
    def _normalized_description(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("browser resource description cannot be blank")
        return normalized


class PersistentBrowserResourceSettings(_BrowserResourceSettings):
    """One Ricky-owned persistent Chrome profile."""

    kind: Literal["persistent"] = "persistent"
    headless: bool = False


class CdpBrowserResourceSettings(_BrowserResourceSettings):
    """One explicit loopback Chrome CDP attachment."""

    kind: Literal["cdp"] = "cdp"
    endpoint: str

    @field_validator("endpoint")
    @classmethod
    def _loopback_http_endpoint(cls, value: str) -> str:
        normalized = value.strip()
        try:
            parts = urlsplit(normalized)
            port = parts.port
        except ValueError as exc:
            raise ValueError("browser CDP endpoint is invalid") from exc
        hostname = parts.hostname
        if (
            parts.scheme.lower() != "http"
            or hostname is None
            or port is None
            or parts.username is not None
            or parts.password is not None
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "browser CDP endpoint must be an exact loopback HTTP endpoint with a port"
            )
        if hostname.casefold() != "localhost":
            try:
                address = ipaddress.ip_address(hostname)
            except ValueError as exc:
                raise ValueError("browser CDP endpoint must use a loopback host") from exc
            if not address.is_loopback:
                raise ValueError("browser CDP endpoint must use a loopback host")
        host = hostname.casefold()
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{port}"


BrowserResourceSettings = Annotated[
    PersistentBrowserResourceSettings | CdpBrowserResourceSettings,
    Field(discriminator="kind"),
]


class ProfileBrowserSettings(BaseModel):
    """Browser resources authored by exactly one Ricky profile."""

    model_config = ConfigDict(extra="forbid", strict=True)

    resources: dict[str, BrowserResourceSettings] = Field(default_factory=dict, max_length=100)
    screenshot_allowed_providers: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("resources")
    @classmethod
    def _resource_names(
        cls,
        value: dict[str, BrowserResourceSettings],
    ) -> dict[str, BrowserResourceSettings]:
        for name in value:
            if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name) is None:
                raise ValueError(
                    "browser resource names must use lowercase letters, digits, hyphens, "
                    "or underscores"
                )
        return value

    @field_validator("screenshot_allowed_providers")
    @classmethod
    def _screenshot_providers(cls, value: list[str]) -> list[str]:
        normalized = [provider.strip() for provider in value]
        if any(not provider for provider in normalized):
            raise ValueError("browser screenshot providers cannot be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError("browser screenshot providers must be unique")
        return normalized


class BackgroundBrowserBudgetSettings(BaseModel):
    """Installation ceilings for one background browser execution."""

    model_config = ConfigDict(extra="forbid")

    session_starts: int = Field(default=1, ge=0, le=100)
    navigations: int = Field(default=40, ge=0, le=10_000)
    scrolls: int = Field(default=100, ge=0, le=10_000)
    created_pages: int = Field(default=8, ge=0, le=1_000)
    controlled_pages: int = Field(default=8, ge=0, le=100)
    semantic_observations: int = Field(default=50, ge=0, le=10_000)
    visual_observations: int = Field(default=10, ge=0, le=1_000)
    interactions: int = Field(default=50, ge=0, le=10_000)
    protected_materializations: int = Field(default=10, ge=0, le=1_000)
    uploads: int = Field(default=10, ge=0, le=1_000)
    upload_bytes: int = Field(default=50_000_000, ge=0, le=10_000_000_000)
    downloads: int = Field(default=10, ge=0, le=1_000)
    download_bytes: int = Field(default=100_000_000, ge=0, le=10_000_000_000)
    transaction_commits: int = Field(default=3, ge=0, le=100)
    parked_browsers: int = Field(default=1, ge=0, le=10)
    approval_ttl_seconds: int = Field(default=900, ge=30, le=86_400)


class BackgroundBrowserSettings(BaseModel):
    """Disabled-by-default owner policy for background browser ownership."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    read_enabled: bool = False
    interaction_enabled: bool = False
    protected_values_enabled: bool = False
    commit_enabled: bool = False
    allow_ephemeral: bool = False
    allow_public_https_research: bool = False
    budget: BackgroundBrowserBudgetSettings = Field(default_factory=BackgroundBrowserBudgetSettings)

    @model_validator(mode="after")
    def _coherent_features(self) -> BackgroundBrowserSettings:
        features = (
            self.read_enabled,
            self.interaction_enabled,
            self.protected_values_enabled,
            self.commit_enabled,
        )
        if any(features) and not self.enabled:
            raise ValueError("browser.background features require background ownership enabled")
        if any(features[1:]) and not self.read_enabled:
            raise ValueError("background browser mutations require read access")
        if self.protected_values_enabled and not self.interaction_enabled:
            raise ValueError("background protected-value use requires browser interaction")
        if self.commit_enabled and self.budget.transaction_commits < 1:
            raise ValueError("background browser commits require a positive commit budget")
        if self.protected_values_enabled and self.budget.protected_materializations < 1:
            raise ValueError(
                "background protected-value use requires a positive materialization budget"
            )
        if self.commit_enabled and self.budget.parked_browsers < 1:
            raise ValueError("background browser commits require a positive parked-browser limit")
        return self


class BrowserSettings(BaseModel):
    """Installation-owned browser runtime, storage, and disclosure limits."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    headless: bool = False
    executable_path: Path | None = None
    ephemeral_dir: str = "browser/ephemeral"
    persistent_dir: str = "browser/persistent"
    lease_dir: str = "browser/leases"
    download_dir: str = "downloads/browser"
    navigation_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    operation_timeout_seconds: float = Field(default=10.0, gt=0, le=300)
    attachment_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    max_sessions: int = Field(default=1, ge=1, le=8)
    max_pages: int = Field(default=8, ge=1, le=32)
    snapshot_depth: int = Field(default=20, ge=1, le=100)
    snapshot_char_limit: int = Field(default=20_000, ge=1_000, le=200_000)
    upload_count_limit: int = Field(default=10, ge=1, le=50)
    upload_file_byte_limit: int = Field(default=20_000_000, ge=1, le=100_000_000)
    upload_total_byte_limit: int = Field(default=50_000_000, ge=1, le=500_000_000)
    download_file_byte_limit: int = Field(default=50_000_000, ge=1, le=500_000_000)
    visual_candidate_limit: int = Field(default=100, ge=1, le=500)
    screenshot_width_limit: int = Field(default=2_000, ge=1, le=10_000)
    screenshot_height_limit: int = Field(default=2_000, ge=1, le=10_000)
    screenshot_pixel_limit: int = Field(default=4_000_000, ge=1, le=100_000_000)
    screenshot_file_byte_limit: int = Field(default=5_000_000, ge=1, le=100_000_000)
    max_redirects: int = Field(default=10, ge=0, le=50)
    allowed_private_origins: list[str] = Field(default_factory=list, max_length=100)
    background: BackgroundBrowserSettings = Field(default_factory=BackgroundBrowserSettings)

    @field_validator("executable_path")
    @classmethod
    def _absolute_executable(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("browser.executable_path must be an absolute path")
        return value

    @field_validator("ephemeral_dir", "persistent_dir", "lease_dir", "download_dir")
    @classmethod
    def _confined_data_dir(cls, value: str, info: Any) -> str:
        return _user_data_relative_path(value, setting=f"browser.{info.field_name}")

    @field_validator("allowed_private_origins")
    @classmethod
    def _exact_private_origins(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("browser.allowed_private_origins cannot contain blank values")
        if len(normalized) != len(set(normalized)):
            raise ValueError("browser.allowed_private_origins must be unique")
        for value in normalized:
            try:
                parts = urlsplit(value)
                _ = parts.port
            except ValueError as exc:
                raise ValueError("browser allowed private origin is invalid") from exc
            if (
                parts.scheme.lower() not in {"http", "https"}
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.path not in {"", "/"}
                or parts.query
                or parts.fragment
            ):
                raise ValueError(
                    "browser.allowed_private_origins entries must be exact http(s) origins"
                )
        return normalized

    @model_validator(mode="after")
    def _coherent_phase4_limits(self) -> BrowserSettings:
        if self.upload_total_byte_limit < self.upload_file_byte_limit:
            raise ValueError(
                "browser.upload_total_byte_limit must be at least upload_file_byte_limit"
            )
        if self.background.enabled and self.background.budget.controlled_pages > self.max_pages:
            raise ValueError(
                "browser.background.budget.controlled_pages cannot exceed browser.max_pages"
            )
        if self.background.enabled and (
            self.background.budget.upload_bytes > self.upload_total_byte_limit
        ):
            raise ValueError(
                "browser.background.budget.upload_bytes cannot exceed "
                "browser.upload_total_byte_limit"
            )
        return self


class ProtectedValuesSettings(BaseModel):
    """Installation-owned protected-value storage and prompt mechanics."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    dir: str = "protected-values"
    sqlite_busy_timeout_ms: int = Field(default=5_000, ge=1, le=60_000)
    catalog_limit: int = Field(default=100, ge=1, le=1_000)
    audit_limit: int = Field(default=500, ge=1, le=10_000)
    prompt_timeout_seconds: float = Field(default=120.0, gt=0, le=3_600)
    argon2_iterations: int = Field(default=3, ge=1, le=100)
    argon2_lanes: int = Field(default=4, ge=1, le=32)
    argon2_memory_kib: int = Field(default=65_536, ge=8_192, le=1_048_576)

    @field_validator("dir")
    @classmethod
    def _confined_dir(cls, value: str) -> str:
        return _user_data_relative_path(value, setting="protected_values.dir")


class ModelSelection(BaseModel):
    """The provider and model pinned to one agent session."""

    provider: str
    model: str


class ProfileConfigSettings(BaseModel):
    """Typed agent-environment overrides loaded from one profile directory."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    profile: ProfileDefinitionSettings | None = None
    user_timezone: str | None = None
    providers: ProvidersSettings | None = None
    request_timeout_seconds: float | None = Field(default=None, gt=0)
    max_turn_iterations: int | None = Field(default=None, ge=1)
    context_char_limit: int | None = Field(default=None, ge=1)
    context: ContextSettings | None = None
    shell_timeout_seconds: float | None = Field(default=None, gt=0)
    google: GoogleSettings | None = None
    google_oauth_clients: dict[str, GoogleOAuthClientSettings] = Field(default_factory=dict)
    gmail: GmailSettings | None = None
    gcal: GcalSettings | None = None
    slack: SlackSettings | None = None
    web_search: WebSearchSettings | None = None
    memory: MemorySettings | None = None
    workflow: WorkflowSettings | None = None
    browser: ProfileBrowserSettings | None = None
    messaging: ProfileMessagingSettings | None = None
    agents: AgentClassSettings | None = None
    authority: AuthoritySettings | None = None
    openrouter_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    brave_search_api_key: SecretStr | None = None
    slack_user_token: SecretStr | None = None

    @field_validator("user_timezone")
    @classmethod
    def _valid_optional_user_timezone(cls, value: str | None) -> str | None:
        return None if value is None else validate_timezone_name(value)


def find_project_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` (default: cwd) to the project root.

    The root is the nearest ancestor containing ``pyproject.toml`` or a
    ``.git`` directory; falls back to ``start`` if no marker is found.
    """
    start = (start or Path.cwd()).resolve()
    for parent in (start, *start.parents):
        if (parent / "pyproject.toml").exists() or (parent / ".git").is_dir():
            return parent
    return start


def _resolve_user_data_root(value: object = "~/.ricky") -> Path:
    """Resolve the bootstrap value that locates Ricky's user configuration."""

    if not isinstance(value, (str, Path)):
        raise TypeError("user_data_dir must be a path string")
    return Path(value).expanduser().resolve()


class _UserDataTomlSettingsSource(TomlConfigSettingsSource):
    """TOML source whose bootstrap root cannot be redirected by its own file."""

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        *,
        path: Path,
        user_data_root: Path,
    ) -> None:
        super().__init__(settings_cls, toml_file=path)
        configured = self.toml_data.get("user_data_dir")
        if configured is not None and _resolve_user_data_root(configured) != user_data_root:
            raise ValueError(
                f"{path} sets user_data_dir outside its bootstrap root; the XDG bootstrap "
                "pointer is authoritative, so remove or align this value"
            )


def _merge_profile_documents(
    base: dict[str, Any],
    override: dict[str, Any],
) -> dict[str, Any]:
    """Recursively merge profile config while keeping the secret file authoritative."""

    merged = dict(base)
    for key, value in override.items():
        observed = merged.get(key)
        if isinstance(observed, dict) and isinstance(value, dict):
            merged[key] = _merge_profile_documents(observed, value)
        else:
            merged[key] = value
    return merged


def _merge_model_overrides[SettingsModelT: BaseModel](
    base: SettingsModelT,
    *overrides: BaseModel | None,
) -> SettingsModelT:
    """Overlay only explicitly configured model fields onto a validated base."""

    merged = base.model_dump(mode="python")
    for override in overrides:
        if override is None:
            continue
        merged = _merge_profile_documents(
            merged,
            override.model_dump(mode="python", exclude_unset=True),
        )
    return type(base).model_validate(merged)


def _read_toml_document(path: Path) -> dict[str, Any]:
    """Read one optional TOML document without exposing its contents in errors."""

    try:
        with path.open("rb") as stream:
            parsed = tomllib.load(stream)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML configuration: {path}") from exc
    return parsed


class _ProfileTomlSettingsSource(PydanticBaseSettingsSource):
    """Load typed per-profile config and secrets from enabled profile roots."""

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        *,
        user_data_root: Path,
    ) -> None:
        super().__init__(settings_cls)
        self._root = user_data_root

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        del field
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        root_document = _read_toml_document(config_file(self._root))
        profile_table = root_document.get("profiles", {})
        if not isinstance(profile_table, dict):
            raise ValueError("profiles must be a TOML table")
        configured_enabled = profile_table.get("enabled", ProfilesSettings().enabled)
        if not isinstance(configured_enabled, list) or not all(
            isinstance(item, str) for item in configured_enabled
        ):
            raise ValueError("profiles.enabled must be a list of profile names")
        enabled = [validate_profile_name(item) for item in configured_enabled]
        if SHARED_PROFILE not in enabled:
            enabled.append(SHARED_PROFILE)

        configs: dict[str, dict[str, Any]] = {}
        definitions: dict[str, dict[str, Any]] = {}
        for profile in enabled:
            non_secret = _read_toml_document(profile_config_file(profile, self._root))
            secret = _read_toml_document(profile_secrets_file(profile, self._root))
            merged = _merge_profile_documents(non_secret, secret)
            if merged:
                configs[profile] = merged
            definition = merged.get("profile")
            if isinstance(definition, dict):
                definitions[profile] = definition

        result: dict[str, Any] = {}
        if configs:
            result["profile_configs"] = configs
        if definitions:
            result["profiles"] = {"definitions": definitions}
        return result


class _UserDataBootstrapSettingsSource(PydanticBaseSettingsSource):
    """Supply the root selected by Ricky's immutable bootstrap boundary."""

    def __init__(self, settings_cls: type[BaseSettings], *, root: Path) -> None:
        super().__init__(settings_cls)
        self._root = root

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        del field
        if field_name == "user_data_dir":
            return str(self._root), field_name, False
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return {"user_data_dir": str(self._root)}


def config_file(root: Path | None = None) -> Path:
    """Path to the user-global, non-secret config file."""

    selected = root if root is not None else resolve_bootstrap_user_data_dir()
    return _resolve_user_data_root(selected) / CONFIG_FILENAME


def secrets_file(root: Path | None = None) -> Path:
    """Path to the user-global secrets file."""

    selected = root if root is not None else resolve_bootstrap_user_data_dir()
    return _resolve_user_data_root(selected) / SECRETS_FILENAME


def profile_config_file(profile: str, root: Path | None = None) -> Path:
    """Path to one profile-owned non-secret configuration file."""

    selected = root if root is not None else resolve_bootstrap_user_data_dir()
    name = validate_profile_name(profile)
    return _resolve_user_data_root(selected) / "profiles" / name / CONFIG_FILENAME


def profile_secrets_file(profile: str, root: Path | None = None) -> Path:
    """Path to one profile-owned secrets file."""

    selected = root if root is not None else resolve_bootstrap_user_data_dir()
    name = validate_profile_name(profile)
    return _resolve_user_data_root(selected) / "profiles" / name / SECRETS_FILENAME


class RickySettings(BaseSettings):
    """Top-level settings for the ricky harness."""

    model_config = SettingsConfigDict(
        extra="ignore",
    )

    user_data_dir: str = "~/.ricky"
    project_data_dir: str = ".ricky"
    profiles: ProfilesSettings = Field(default_factory=ProfilesSettings)
    profile_configs: dict[str, ProfileConfigSettings] = Field(default_factory=dict)
    user_timezone: str = Field(
        default="UTC",
        description="Default IANA timezone for new agent sessions.",
    )

    _valid_user_timezone = field_validator("user_timezone")(validate_timezone_name)

    # --- Provider / LLM ---------------------------------------------------
    default_provider: str = Field(
        default="openrouter",
        description="Default provider for new sessions.",
    )
    providers: ProvidersSettings = Field(default_factory=ProvidersSettings)
    openrouter_api_key: SecretStr | None = Field(
        default=None,
        description="OpenRouter API key from the owning profile's .secrets.toml.",
    )
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        description="Anthropic API key from the owning profile's .secrets.toml.",
    )
    request_timeout_seconds: float = 120.0

    # --- Agent loop -------------------------------------------------------
    max_turn_iterations: int = Field(
        default=25,
        description="Safety guard on tool-call iterations within a single turn.",
    )
    context_char_limit: int = Field(
        default=120_000,
        description="Fail-loudly ceiling on assembled request size, in characters.",
    )
    context: ContextSettings = Field(default_factory=ContextSettings)

    # --- Agent-usable protected values -----------------------------------
    protected_values: ProtectedValuesSettings = Field(default_factory=ProtectedValuesSettings)

    def resolve_selection(
        self,
        provider: str | None = None,
        model: str | None = None,
    ) -> ModelSelection:
        """Resolve explicit overrides against the configured provider defaults."""
        selected_provider = provider or self.default_provider
        if selected_provider not in ProvidersSettings.model_fields:
            valid = ", ".join(ProvidersSettings.model_fields)
            raise ValueError(f"Unknown provider {selected_provider!r}; valid providers: {valid}")
        provider_settings: ProviderSettings = getattr(self.providers, selected_provider)
        return ModelSelection(
            provider=selected_provider,
            model=model or provider_settings.default_model,
        )

    def resolve_profile_scope(
        self,
        primary: str | None = None,
        *,
        access_profiles: tuple[str, ...] | list[str] = (),
    ) -> ProfileScope:
        """Resolve one immutable runtime scope against the enabled registry."""

        selected_primary = validate_profile_name(primary or self.profiles.default)
        requested = {selected_primary, SHARED_PROFILE, *access_profiles}
        unknown = sorted(requested - set(self.profiles.enabled))
        if unknown:
            raise ValueError("unknown or disabled profile(s): " + ", ".join(unknown))
        return ProfileScope.create(
            selected_primary,
            access_profiles=list(access_profiles),
        )

    def resolve_profile_selection(
        self,
        scope: ProfileScope,
        provider: str | None = None,
        model: str | None = None,
    ) -> ModelSelection:
        """Resolve a model only after applying every accessible profile's policy."""

        if provider is not None and provider not in ProvidersSettings.model_fields:
            valid = ", ".join(ProvidersSettings.model_fields)
            raise ValueError(f"Unknown provider {provider!r}; valid providers: {valid}")
        if any(profile not in self.profiles.enabled for profile in scope.profiles):
            raise ValueError("profile scope contains an unknown or disabled profile")
        definitions = [self.profiles.definitions[profile] for profile in scope.profiles]
        allowed_sets = [
            set(item.allowed_providers) for item in definitions if item.allowed_providers
        ]
        allowed = set(ProvidersSettings.model_fields)
        for configured in allowed_sets:
            allowed &= configured
        primary = self.profiles.definitions[scope.primary]
        shared = self.profiles.definitions[SHARED_PROFILE]
        selected_provider = (
            provider or primary.default_provider or shared.default_provider or self.default_provider
        )
        if selected_provider not in allowed:
            if provider is None and len(allowed) == 1:
                selected_provider = next(iter(allowed))
            else:
                available = ", ".join(sorted(allowed)) or "[none]"
                raise ValueError(
                    f"provider {selected_provider!r} is not allowed by profile scope; "
                    f"allowed providers: {available}"
                )
        provider_settings = self.profile_provider_settings(scope, selected_provider)
        selected_model = (
            model
            or primary.default_models.get(selected_provider)
            or shared.default_models.get(selected_provider)
            or provider_settings.default_model
        )
        return ModelSelection(provider=selected_provider, model=selected_model)

    def profile_provider_settings(
        self,
        scope: ProfileScope,
        provider: str,
    ) -> ProviderSettings:
        """Resolve provider configuration from primary, shared, then installation defaults."""

        base: ProviderSettings = getattr(self.providers, provider)
        overrides = []
        for profile in (SHARED_PROFILE, scope.primary):
            configured = self.profile_configs.get(profile)
            if configured is not None and configured.providers is not None:
                overrides.append(getattr(configured.providers, provider))
        return _merge_model_overrides(base, *overrides)

    def resolve_profile_runtime_settings(self, scope: ProfileScope) -> RickySettings:
        """Return a non-widening settings view for one issued profile scope.

        Installation mechanics stay rooted in the bootstrap settings. Ordinary
        environment defaults resolve primary then shared. Resource catalogs are
        built only from accessible profile documents and use qualified ids.
        """

        if any(profile not in self.profiles.enabled for profile in scope.profiles):
            raise ValueError("profile scope contains an unknown or disabled profile")

        primary = self.profile_configs.get(scope.primary)
        shared = self.profile_configs.get(SHARED_PROFILE)

        def ordinary(field: str) -> Any:
            for configured in (primary, shared):
                if configured is None:
                    continue
                value = getattr(configured, field)
                if value is not None:
                    return value
            return getattr(self, field)

        def merged_section(field: str) -> Any:
            """Merge sparse shared and primary sections over installation settings."""

            base = getattr(self, field)
            shared_override = None if shared is None else getattr(shared, field)
            primary_override = None if primary is None else getattr(primary, field)
            return _merge_model_overrides(base, shared_override, primary_override)

        def profile_section(field: str, profile: str) -> Any | None:
            """Return one profile's sparse section merged over installation settings."""

            configured = self.profile_configs.get(profile)
            if configured is None or (override := getattr(configured, field)) is None:
                return None
            return _merge_model_overrides(getattr(self, field), override)

        def owned(field: str) -> tuple[Any, str | None]:
            for profile, configured in (
                (scope.primary, primary),
                (SHARED_PROFILE, shared),
            ):
                if configured is None:
                    continue
                value = getattr(configured, field)
                if value is not None:
                    return value, profile
            return getattr(self, field), None

        slack_settings = merged_section("slack")
        _, slack_config_owner = owned("slack")
        _, slack_credential_owner = owned("slack_user_token")
        slack_owner = slack_credential_owner or slack_config_owner
        web_settings = merged_section("web_search")
        _, web_owner = owned("web_search")
        if slack_owner is not None:
            prefix = f"profiles/{slack_owner}/"
            if not slack_settings.download_dir.startswith(prefix):
                slack_settings = slack_settings.model_copy(
                    update={"download_dir": f"{prefix}{slack_settings.download_dir}"}
                )
        if web_owner is not None:
            prefix = f"profiles/{web_owner}/"
            if not web_settings.download_dir.startswith(prefix):
                web_settings = web_settings.model_copy(
                    update={"download_dir": f"{prefix}{web_settings.download_dir}"}
                )

        google_accounts: dict[str, GoogleAccountSettings] = {}
        google_clients: dict[str, GoogleOAuthClientSettings] = {}
        for profile in scope.profiles:
            configured = self.profile_configs.get(profile)
            if configured is None:
                continue
            if configured.google is not None:
                for name, account in configured.google.accounts.items():
                    google_accounts[f"{profile}/{name}"] = account
            for name, client in configured.google_oauth_clients.items():
                google_clients[f"{profile}/{name}"] = client

        selected_google = merged_section("google").model_copy(update={"accounts": google_accounts})

        telegram_accounts = {
            name: account
            for name, account in self.messaging.telegram_accounts.items()
            if ProfileResourceRef.from_qualified(name).profile in scope.profiles
        }
        for profile in scope.profiles:
            configured = self.profile_configs.get(profile)
            if configured is None or configured.messaging is None:
                continue
            for name, account in configured.messaging.telegram_accounts.items():
                resource = ProfileResourceRef(profile=profile, name=name)
                telegram_accounts[resource.qualified] = account

        transports = {
            name: transport
            for name, transport in self.messaging.transports.items()
            if transport.account_ref.profile in scope.profiles
        }
        routes = {
            name: route
            for name, route in self.messaging.routes.items()
            if route.transport in transports
        }
        selected_messaging = self.messaging.model_copy(
            update={
                "telegram_accounts": telegram_accounts,
                "transports": transports,
                "routes": routes,
                "agent_routes": [route for route in self.messaging.agent_routes if route in routes],
                "job_route": (
                    self.messaging.job_route if self.messaging.job_route in routes else None
                ),
            }
        )
        selected_gateway = self.gateway.model_copy(
            update={
                "routes": {
                    name: route
                    for name, route in self.gateway.routes.items()
                    if set(route.profile_scope().profiles).issubset(scope.profiles)
                    and name in routes
                }
            }
        )
        agent_sources = [self.agents]
        agent_sources.extend(
            source
            for profile in scope.profiles
            if (source := profile_section("agents", profile)) is not None
        )

        def intersect_policy(
            field: Literal["gateway_foreground", "ad_hoc_background"],
        ) -> AgentCapabilityPolicySettings:
            policies = [getattr(source, field) for source in agent_sources]
            update: dict[str, Any] = {
                "exclude_capabilities": sorted(
                    {item for policy in policies for item in policy.exclude_capabilities}
                ),
                "confirmation_required_capabilities": sorted(
                    {
                        item
                        for policy in policies
                        for item in policy.confirmation_required_capabilities
                    }
                ),
                "guardrail_required_capabilities": sorted(
                    {item for policy in policies for item in policy.guardrail_required_capabilities}
                ),
            }
            if field == "ad_hoc_background":
                executions = [source.ad_hoc_background.execution for source in agent_sources]
                update["execution"] = AdHocExecutionPolicySettings(
                    wall_clock_seconds=min(item.wall_clock_seconds for item in executions),
                    iterations=min(item.iterations for item in executions),
                    max_completion_tokens_per_request=min(
                        item.max_completion_tokens_per_request for item in executions
                    ),
                    effect_calls=min(item.effect_calls for item in executions),
                )
            return policies[0].model_copy(update=update)

        agents = AgentClassSettings(
            gateway_foreground=intersect_policy("gateway_foreground"),
            ad_hoc_background=AdHocBackgroundSettings.model_validate(
                intersect_policy("ad_hoc_background").model_dump(mode="python")
            ),
        )

        memory_sources = [self.memory]
        workflow_sources = [self.workflow]
        for profile in scope.profiles:
            if (memory_source := profile_section("memory", profile)) is not None:
                memory_sources.append(memory_source)
            if (workflow_source := profile_section("workflow", profile)) is not None:
                workflow_sources.append(workflow_source)
        memory = MemorySettings(
            enabled=all(item.enabled for item in memory_sources),
            index_char_limit=min(item.index_char_limit for item in memory_sources),
            recall_char_limit=min(item.recall_char_limit for item in memory_sources),
            recall_note_limit=min(item.recall_note_limit for item in memory_sources),
            note_body_char_limit=min(item.note_body_char_limit for item in memory_sources),
        )
        primary_workflow = merged_section("workflow")
        workflow = primary_workflow.model_copy(
            update={
                "enabled": all(item.enabled for item in workflow_sources),
                "instruction_char_limit": min(
                    item.instruction_char_limit for item in workflow_sources
                ),
                "max_parallel_steps": min(item.max_parallel_steps for item in workflow_sources),
                "max_parallel_items": min(item.max_parallel_items for item in workflow_sources),
                "max_binding_chars": min(item.max_binding_chars for item in workflow_sources),
                "max_result_chars": min(item.max_result_chars for item in workflow_sources),
                "max_schema_depth": min(item.max_schema_depth for item in workflow_sources),
                "max_foreach_items": min(item.max_foreach_items for item in workflow_sources),
                "model_attempts": min(item.model_attempts for item in workflow_sources),
                "agent_iterations": min(item.agent_iterations for item in workflow_sources),
                "run_dir": self.workflow.run_dir,
            }
        )

        authority_sources = [self.authority]
        authority_sources.extend(
            source
            for profile in scope.profiles
            if (source := profile_section("authority", profile)) is not None
        )
        authority_max_ttl = min(item.max_ttl_seconds for item in authority_sources)
        authority_max_effect_calls = min(item.max_effect_calls for item in authority_sources)
        authority_capabilities: dict[str, AuthorityCapabilitySettings] = {}
        capability_names = {name for source in authority_sources for name in source.capabilities}
        for name in sorted(capability_names):
            ceilings = [
                source.capabilities[name]
                for source in authority_sources
                if name in source.capabilities
            ]
            allowed_profiles = set(ceilings[0].allowed_profiles)
            for ceiling in ceilings[1:]:
                allowed_profiles &= set(ceiling.allowed_profiles)
            currencies = {ceiling.currency for ceiling in ceilings if ceiling.currency is not None}
            max_financial_limit_minor = min(
                ceiling.max_financial_limit_minor for ceiling in ceilings
            )
            currency = next(iter(currencies)) if len(currencies) == 1 else None
            if len(currencies) > 1:
                max_financial_limit_minor = 0
            authority_capabilities[name] = AuthorityCapabilitySettings(
                enabled=all(ceiling.enabled for ceiling in ceilings) and bool(allowed_profiles),
                max_effect_calls=min(
                    authority_max_effect_calls,
                    *(ceiling.max_effect_calls for ceiling in ceilings),
                ),
                max_ttl_seconds=min(
                    authority_max_ttl,
                    *(ceiling.max_ttl_seconds for ceiling in ceilings),
                ),
                max_financial_limit_minor=max_financial_limit_minor,
                currency=currency,
                allowed_profiles=sorted(
                    allowed_profiles,
                    key=lambda item: (item != SHARED_PROFILE, item),
                ),
            )
        allowed_principals = set(authority_sources[0].allowed_principals)
        for source in authority_sources[1:]:
            allowed_principals &= set(source.allowed_principals)
        authority = AuthoritySettings(
            enabled=all(item.enabled for item in authority_sources),
            store_path=self.authority.store_path,
            sqlite_busy_timeout_ms=self.authority.sqlite_busy_timeout_ms,
            allowed_principals=sorted(allowed_principals),
            default_ttl_seconds=min(
                authority_max_ttl,
                *(item.default_ttl_seconds for item in authority_sources),
            ),
            max_ttl_seconds=authority_max_ttl,
            max_effect_calls=authority_max_effect_calls,
            source_snapshot_chars=min(item.source_snapshot_chars for item in authority_sources),
            capabilities=authority_capabilities,
        )
        if len(authority_sources) == 1:
            authority = self.authority

        def strict_scalar(field: str) -> Any:
            values = [getattr(self, field)]
            values.extend(
                value
                for profile in scope.profiles
                if (configured := self.profile_configs.get(profile)) is not None
                and (value := getattr(configured, field)) is not None
            )
            return min(values)

        updates: dict[str, Any] = {
            "user_timezone": ordinary("user_timezone"),
            "default_provider": self.resolve_profile_selection(scope).provider,
            "providers": merged_section("providers"),
            "request_timeout_seconds": strict_scalar("request_timeout_seconds"),
            "max_turn_iterations": strict_scalar("max_turn_iterations"),
            "context_char_limit": strict_scalar("context_char_limit"),
            "context": merged_section("context"),
            "shell_timeout_seconds": strict_scalar("shell_timeout_seconds"),
            "google": selected_google,
            "google_oauth_clients": google_clients,
            "gmail": merged_section("gmail"),
            "gcal": merged_section("gcal"),
            "slack": slack_settings,
            "web_search": web_settings,
            "memory": memory,
            "workflow": workflow,
            "messaging": selected_messaging,
            "gateway": selected_gateway,
            "agents": agents,
            "authority": authority,
            "openrouter_api_key": ordinary("openrouter_api_key"),
            "anthropic_api_key": ordinary("anthropic_api_key"),
            "brave_search_api_key": ordinary("brave_search_api_key"),
            "slack_user_token": ordinary("slack_user_token"),
            "profile_configs": {
                profile: self.profile_configs[profile]
                for profile in scope.profiles
                if profile in self.profile_configs
            },
        }
        return self.model_copy(update=updates, deep=True)

    # --- Tools ------------------------------------------------------------
    shell_timeout_seconds: float = 120.0
    google: GoogleSettings = Field(default_factory=GoogleSettings)
    google_oauth_clients: dict[str, GoogleOAuthClientSettings] = Field(default_factory=dict)
    gmail: GmailSettings = Field(default_factory=GmailSettings)
    gcal: GcalSettings = Field(default_factory=GcalSettings)
    slack: SlackSettings = Field(default_factory=SlackSettings)
    web_search: WebSearchSettings = Field(default_factory=WebSearchSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    durable_tasks: DurableTaskSettings = Field(default_factory=DurableTaskSettings)
    sessions: SessionSettings = Field(default_factory=SessionSettings)
    messaging: MessagingSettings = Field(default_factory=MessagingSettings)
    gateway: GatewaySettings = Field(default_factory=GatewaySettings)
    agents: AgentClassSettings = Field(default_factory=AgentClassSettings)
    jobs: JobSettings = Field(default_factory=JobSettings)
    executions: ExecutionSettings = Field(default_factory=ExecutionSettings)
    authority: AuthoritySettings = Field(default_factory=AuthoritySettings)
    schedules: ScheduleSettings = Field(default_factory=ScheduleSettings)
    workflow: WorkflowSettings = Field(default_factory=WorkflowSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    brave_search_api_key: SecretStr | None = Field(
        default=None,
        description="Brave Search API key from the owning profile's .secrets.toml.",
    )
    slack_user_token: SecretStr | None = Field(
        default=None,
        description="Slack user OAuth token from the owning profile's .secrets.toml.",
    )

    @model_validator(mode="after")
    def _validate_profile_references(self) -> RickySettings:
        valid = set(ProvidersSettings.model_fields)
        unknown_profile_configs = sorted(set(self.profile_configs) - set(self.profiles.enabled))
        if unknown_profile_configs:
            raise ValueError(
                "profile configuration exists for disabled profile(s): "
                + ", ".join(unknown_profile_configs)
            )
        for profile, configured in self.profile_configs.items():
            accounts = configured.google.accounts if configured.google is not None else {}
            for name in {*accounts, *configured.google_oauth_clients}:
                ProfileResourceRef(profile=profile, name=name)
            browser_resources = (
                configured.browser.resources if configured.browser is not None else {}
            )
            for name in browser_resources:
                ProfileResourceRef(profile=profile, name=name)
            screenshot_providers = (
                configured.browser.screenshot_allowed_providers
                if configured.browser is not None
                else []
            )
            unknown_screenshot_providers = sorted(set(screenshot_providers) - valid)
            if unknown_screenshot_providers:
                raise ValueError(
                    f"profile {profile!r} browser screenshots contain unknown provider(s): "
                    + ", ".join(unknown_screenshot_providers)
                )
            orphaned_clients = sorted(set(configured.google_oauth_clients) - set(accounts))
            if orphaned_clients:
                raise ValueError(
                    f"profile {profile!r} has Google OAuth clients without accounts: "
                    + ", ".join(orphaned_clients)
                )
        telegram_accounts = set(self.messaging.telegram_accounts)
        for profile, configured in self.profile_configs.items():
            if configured.messaging is None:
                continue
            telegram_accounts.update(
                ProfileResourceRef(profile=profile, name=name).qualified
                for name in configured.messaging.telegram_accounts
            )
        for profile, definition in self.profiles.definitions.items():
            configured = set(definition.allowed_providers)
            if definition.default_provider is not None:
                configured.add(definition.default_provider)
            configured.update(definition.default_models)
            unknown_profile_providers = sorted(configured - valid)
            if unknown_profile_providers:
                raise ValueError(
                    f"profile {profile!r} contains unknown provider(s): "
                    + ", ".join(unknown_profile_providers)
                )
            if (
                definition.default_provider is not None
                and definition.allowed_providers
                and definition.default_provider not in definition.allowed_providers
            ):
                raise ValueError(
                    f"profile {profile!r} default_provider must be allowed by that profile"
                )
        enabled = set(self.profiles.enabled)
        for name, route in self.gateway.routes.items():
            scope = route.profile_scope()
            unknown_route_profiles = sorted(set(scope.profiles) - enabled)
            if unknown_route_profiles:
                raise ValueError(
                    f"gateway route {name!r} contains disabled profile(s): "
                    + ", ".join(unknown_route_profiles)
                )
            selection = self.resolve_profile_selection(scope, route.provider, route.model)
            if selection.model != route.model:
                raise ValueError(f"gateway route {name!r} model did not resolve exactly")
            messaging_route = self.messaging.routes.get(name)
            if messaging_route is None:
                raise ValueError(f"gateway route {name!r} requires a same-named messaging route")
            missing_clearance = sorted(set(scope.profiles) - set(messaging_route.accepted_profiles))
            if missing_clearance:
                raise ValueError(
                    f"messaging route {name!r} does not accept gateway profile(s): "
                    + ", ".join(missing_clearance)
                )
        for name, route in self.messaging.routes.items():
            unknown_route_profiles = sorted(
                {route.owner_profile, *route.accepted_profiles} - enabled
            )
            if unknown_route_profiles:
                raise ValueError(
                    f"messaging route {name!r} contains disabled profile(s): "
                    + ", ".join(unknown_route_profiles)
                )
            transport = self.messaging.transports[route.transport]
            if route.owner_profile != transport.account_ref.profile:
                raise ValueError(
                    f"messaging route {name!r} owner_profile must own transport account "
                    f"{transport.account!r}"
                )
        for name, transport in self.messaging.transports.items():
            account = transport.account_ref
            if account.profile not in enabled:
                raise ValueError(
                    f"messaging transport {name!r} references disabled profile {account.profile!r}"
                )
            if transport.type == "telegram" and account.qualified not in telegram_accounts:
                raise ValueError(
                    f"messaging transport {name!r} references unknown Telegram account "
                    f"{account.qualified!r}"
                )
        for name, ceiling in self.authority.capabilities.items():
            unknown_ceiling_profiles = sorted(set(ceiling.allowed_profiles) - enabled)
            if unknown_ceiling_profiles:
                raise ValueError(
                    f"authority capability {name!r} contains disabled profile(s): "
                    + ", ".join(unknown_ceiling_profiles)
                )
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Load installation config and enabled profile config from the bootstrap root."""

        init_values = init_settings()
        del env_settings, dotenv_settings, file_secret_settings
        explicit_root = init_values.get("user_data_dir")
        root = resolve_bootstrap_user_data_dir(explicit_root)
        bootstrap = _UserDataBootstrapSettingsSource(settings_cls, root=root)
        return (
            init_settings,
            bootstrap,
            _ProfileTomlSettingsSource(
                settings_cls,
                user_data_root=root,
            ),
            _UserDataTomlSettingsSource(
                settings_cls,
                path=config_file(root),
                user_data_root=root,
            ),
        )


def load_settings(**overrides: Any) -> RickySettings:
    """Load settings, applying any explicit overrides (highest precedence)."""
    return RickySettings(**overrides)


def load_settings_at(user_data_root: Path, **overrides: Any) -> RickySettings:
    """Load settings from one explicit user-data root.

    This is primarily an isolation seam for tests. Normal callers should use
    :func:`load_settings` and configure an alternate root with
    ``RICKY_USER_DATA_DIR``.
    """

    resolved = _resolve_user_data_root(user_data_root)
    return RickySettings(user_data_dir=str(resolved), **overrides)


def user_data_path(settings: RickySettings) -> Path:
    """Resolve the configured user-global assistant data directory."""

    return Path(settings.user_data_dir).expanduser().resolve()


def ensure_private_user_data_root(settings: RickySettings) -> Path:
    """Create the user-global data root and keep it owner-only on POSIX.

    ``Path.mkdir(parents=True, mode=...)`` applies its mode only to the final
    component, so a root created as a parent of a store directory inherits the
    process umask. Callers that own the root's lifecycle tighten it here rather
    than each store guessing.
    """

    root = user_data_path(settings)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(root, 0o700)
    return root


def user_data_subpath(settings: RickySettings, configured: str) -> Path:
    """Resolve a validated configured path below the user-global data root."""

    relative = _user_data_relative_path(configured, setting="configured path")
    root = user_data_path(settings)
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("configured path escapes user_data_dir")
    return resolved


def profile_data_path(settings: RickySettings, profile: str) -> Path:
    """Resolve one enabled profile's private data root below ``user_data_dir``."""

    name = validate_profile_name(profile)
    if name not in settings.profiles.enabled:
        raise ValueError(f"unknown or disabled profile: {name}")
    root = user_data_path(settings)
    resolved = (root / "profiles" / name).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("profile data path escapes user_data_dir")
    return resolved


def profile_data_subpath(
    settings: RickySettings,
    profile: str,
    configured: str,
) -> Path:
    """Resolve a validated configured path below one enabled profile root."""

    relative = _user_data_relative_path(configured, setting="configured path")
    root = profile_data_path(settings, profile)
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("configured path escapes profile data directory")
    return resolved


def project_data_path(settings: RickySettings, root: Path | None = None) -> Path:
    """Resolve the configured project-local assistant data directory."""

    # An explicit ProjectScope is already canonical and must not silently
    # rebind to an unrelated ancestor marker (for example /tmp/.git).
    project_root = find_project_root() if root is None else root.expanduser().resolve()
    configured = Path(settings.project_data_dir).expanduser()
    if configured.is_absolute():
        return configured.resolve()
    return (project_root / configured).resolve()


def write_default_selection(
    selection: ModelSelection,
    root: Path | None = None,
    *,
    profile: str | None = None,
) -> Path:
    """Persist a non-secret installation or profile default selection."""
    import tomlkit

    path = config_file(root) if profile is None else profile_config_file(profile, root)
    document = _open_config_document(path, "configuration file")

    if profile is None:
        document["default_provider"] = selection.provider
        providers = document.get("providers")
        if not isinstance(providers, dict):
            providers = tomlkit.table()
            document["providers"] = providers
        provider_table = providers.get(selection.provider)
        if not isinstance(provider_table, dict):
            provider_table = tomlkit.table()
            providers[selection.provider] = provider_table
        provider_table["default_model"] = selection.model
    else:
        definition = document.get("profile")
        if not isinstance(definition, dict):
            definition = tomlkit.table()
            document["profile"] = definition
        definition["default_provider"] = selection.provider
        models = definition.get("default_models")
        if not isinstance(models, dict):
            models = tomlkit.table()
            definition["default_models"] = models
        models[selection.provider] = selection.model

    write_private_file(path, tomlkit.dumps(document))
    return path


def write_profile_secret(
    name: Literal["openrouter_api_key", "anthropic_api_key"],
    value: SecretStr,
    *,
    profile: str,
    root: Path | None = None,
) -> Path:
    """Atomically persist one provider credential at its owning profile boundary."""

    import tomlkit

    path = profile_secrets_file(profile, root)
    document = _open_config_document(path, "profile secrets file")
    document[name] = value.get_secret_value()
    write_private_file(path, tomlkit.dumps(document))
    return path


class GoogleAccountSetupError(RuntimeError):
    """Account setup failed without successfully restoring its original files."""


class _GoogleAccountImport(BaseModel):
    """Validated fields imported from a downloaded Desktop OAuth client."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    client_id: str = Field(min_length=1)
    client_secret: SecretStr = Field(min_length=1)


def add_google_account(
    name: str,
    *,
    profile: str,
    email: str,
    client_json: Path,
) -> ProfileResourceRef:
    """Create an account and its private OAuth client in one enabled profile."""
    import tomlkit

    resource = ProfileResourceRef(profile=profile, name=name)
    name = resource.name
    profile = resource.profile
    email = email.strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+", email):
        raise ValueError("expected email must contain a local part and domain without whitespace")
    try:
        downloaded = json.loads(client_json.expanduser().read_text(encoding="utf-8"))
        installed = downloaded["installed"]
        client = _GoogleAccountImport.model_validate(
            {"client_id": installed["client_id"], "client_secret": installed["client_secret"]}
        )
        if not client.client_id.strip():
            raise ValueError("empty client id")
    except (OSError, ValueError, KeyError, TypeError):
        # Parser and validation errors may contain the imported secret.
        raise ValueError("could not read a valid Google Desktop OAuth client JSON file") from None

    expected_pointer, expected_manifest = require_compatible_installation()
    with installation_operation_lock(
        mode="exclusive", timeout_seconds=5.0, operation="google_account_add"
    ):
        pointer, manifest = require_compatible_installation()
        if pointer != expected_pointer or manifest != expected_manifest:
            raise ValueError("Ricky installation changed while adding the Google account")
        try:
            settings = load_settings()
        except ValueError:
            raise ValueError(
                "could not load the existing configuration; check profile settings and secrets"
            ) from None
        root = user_data_path(settings)
        if root != Path(pointer.user_data_dir):
            raise ValueError("resolved configuration does not match the installation")
        if profile not in settings.profiles.enabled:
            raise ValueError(f"profile is not enabled: {profile}")
        configured = settings.profile_configs.get(profile)
        if configured is not None and (
            name in configured.google_oauth_clients
            or (configured.google is not None and name in configured.google.accounts)
        ):
            raise ValueError(f"Google account already exists: {resource.qualified}")
        config_path = profile_config_file(profile, root)
        secrets_path = profile_secrets_file(profile, root)
        if (
            config_path.parent.is_symlink()
            or not config_path.parent.is_dir()
            or config_path.parent.parent.is_symlink()
        ):
            raise ValueError("owning profile directory must exist and cannot be a symbolic link")
        try:
            document = _open_config_document(config_path, "profile configuration file")
            secrets = _open_config_document(secrets_path, "profile secrets file")
        except (OSError, ValueError):
            raise ValueError("could not safely read profile configuration and secrets") from None
        google = document.setdefault("google", tomlkit.table())
        accounts = google.setdefault("accounts", tomlkit.table())
        clients = secrets.setdefault("google_oauth_clients", tomlkit.table())
        if name in accounts or name in clients:
            raise ValueError(f"Google account already exists: {resource.qualified}")
        accounts[name] = {"email": email}
        clients[name] = {
            "client_id": client.client_id,
            "client_secret": client.client_secret.get_secret_value(),
        }
        originals = {
            path: path.read_text(encoding="utf-8") if path.exists() else None
            for path in (config_path, secrets_path)
        }
        attempted: list[Path] = []
        try:
            for path, content in (
                (config_path, tomlkit.dumps(document)),
                (secrets_path, tomlkit.dumps(secrets)),
            ):
                attempted.append(path)
                write_private_file(path, content)
            # Verify the complete pair through the normal settings boundary.
            load_settings()
        except BaseException as exc:
            rollback_failed = False
            for path in reversed(attempted):
                try:
                    original = originals[path]
                    if original is None:
                        path.unlink(missing_ok=True)
                        fsync_directory(path.parent)
                    else:
                        write_private_file(path, original)
                except BaseException:
                    rollback_failed = True
            if rollback_failed:
                raise GoogleAccountSetupError(
                    "Google account setup failed and could not restore configuration"
                ) from None
            if isinstance(exc, Exception):
                raise ValueError(
                    "Google account setup failed; original configuration restored"
                ) from None
            raise
    return resource


def _open_config_document(path: Path, label: str) -> Any:
    """Parse an existing owner-only document, or start a new one, refusing links."""

    import tomlkit

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError(f"{label} cannot be a symbolic link")
    if path.exists():
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    return tomlkit.document()
