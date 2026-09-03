"""Tests for configuration loading, selection, and persistence."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from ricky.config import (
    AuthoritySettings,
    GatewayRouteSettings,
    GatewaySettings,
    GmailSettings,
    GoogleSettings,
    MessagingRouteSettings,
    MessagingSettings,
    MessagingTransportSettings,
    ModelSelection,
    OpenRouterSettings,
    ProfileConfigSettings,
    ProfileDefinitionSettings,
    ProfilesSettings,
    ProvidersSettings,
    RickySettings,
    SlackSettings,
    WebSearchSettings,
    profile_config_file,
    profile_data_path,
    profile_secrets_file,
    user_data_path,
    write_default_selection,
    write_profile_secret,
)

ENV_VARS = (
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "RICKY_OPENROUTER_API_KEY",
    "RICKY_ANTHROPIC_API_KEY",
    "RICKY_DEFAULT_PROVIDER",
    "RICKY_PROVIDERS__OPENROUTER__DEFAULT_MODEL",
    "RICKY_PROVIDERS__ANTHROPIC__DEFAULT_MODEL",
    "SLACK_USER_TOKEN",
    "RICKY_SLACK_USER_TOKEN",
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated user-data root with relevant environment variables cleared."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='probe'\n")
    monkeypatch.chdir(tmp_path)
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    return user_data


def test_defaults_when_no_files_or_env(project: Path) -> None:
    settings = RickySettings()

    assert settings.openrouter_api_key is None
    assert settings.anthropic_api_key is None
    assert settings.default_provider == "openrouter"
    assert settings.providers.openrouter.default_model == "anthropic/claude-sonnet-4"
    assert settings.providers.anthropic.default_model == "claude-sonnet-5"
    assert settings.providers.anthropic.default_max_tokens == 8192
    assert settings.max_turn_iterations == 25


def test_repository_examples_load_as_root_and_profile_configuration(
    project: Path,
) -> None:
    repository = Path(__file__).resolve().parents[1]
    root_template = (repository / "ricky.toml.example").read_text(encoding="utf-8")
    secret_template = (repository / ".secrets.toml.example").read_text(encoding="utf-8")
    root_template = root_template.replace(
        'user_data_dir = "~/.ricky"',
        f'user_data_dir = "{project}"',
    )
    (project / "ricky.toml").write_text(root_template, encoding="utf-8")
    personal = project / "profiles" / "personal"
    personal.mkdir(parents=True)
    (personal / ".secrets.toml").write_text(secret_template, encoding="utf-8")

    settings = RickySettings()
    runtime = settings.resolve_profile_runtime_settings(settings.resolve_profile_scope())

    assert settings.profiles.default == "personal"
    assert settings.profiles.enabled == ["shared", "personal", "work"]
    assert settings.profiles.definitions["work"].default_models == {"claude_code": "sonnet"}
    assert settings.max_turn_iterations == 50
    assert settings.context_char_limit == 1_000_000
    assert runtime.openrouter_api_key is not None
    assert runtime.brave_search_api_key is not None


def test_project_local_files_are_not_configuration_sources(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = project.parent
    (project_root / "ricky.toml").write_text('default_provider = "anthropic"\n')
    (project_root / ".secrets.toml").write_text('openrouter_api_key = "project-secret"\n')

    settings = RickySettings()

    assert settings.default_provider == "openrouter"
    assert settings.openrouter_api_key is None


def test_toml_cannot_redirect_its_bootstrap_user_data_root(project: Path) -> None:
    redirected = project.parent / "redirected"
    (project / "ricky.toml").write_text(f'user_data_dir = "{redirected}"\n')

    with pytest.raises(ValueError, match="bootstrap pointer is authoritative"):
        RickySettings()

    assert not redirected.exists()


def test_nested_config_toml_by_natural_field_names(project: Path) -> None:
    (project / "ricky.toml").write_text(
        """default_provider = "anthropic"
max_turn_iterations = 7

[providers.openrouter]
default_model = "from-config/openrouter"

[providers.anthropic]
default_model = "from-config/anthropic"
default_max_tokens = 2048
"""
    )

    settings = RickySettings()

    assert settings.default_provider == "anthropic"
    assert settings.providers.openrouter.default_model == "from-config/openrouter"
    assert settings.providers.anthropic.default_model == "from-config/anthropic"
    assert settings.providers.anthropic.default_max_tokens == 2048
    assert settings.max_turn_iterations == 7


def test_secrets_load_only_from_owning_profile(project: Path) -> None:
    personal_root = project / "profiles" / "personal"
    personal_root.mkdir(parents=True)
    (personal_root / ".secrets.toml").write_text(
        'openrouter_api_key = "openrouter-secret"\nanthropic_api_key = "anthropic-secret"\n'
    )
    (project / ".secrets.toml").write_text('openrouter_api_key = "retired-root-secret"\n')

    settings = RickySettings()
    runtime = settings.resolve_profile_runtime_settings(settings.resolve_profile_scope())

    assert settings.openrouter_api_key is None
    assert runtime.openrouter_api_key is not None
    assert runtime.openrouter_api_key.get_secret_value() == "openrouter-secret"
    assert runtime.anthropic_api_key is not None
    assert runtime.anthropic_api_key.get_secret_value() == "anthropic-secret"
    assert "retired-root-secret" not in repr(settings)


def test_retired_root_secrets_toml_cannot_override_installation_config(project: Path) -> None:
    (project / "ricky.toml").write_text('default_provider = "openrouter"\n')
    (project / ".secrets.toml").write_text('default_provider = "anthropic"\n')

    assert RickySettings().default_provider == "openrouter"


def test_environment_settings_do_not_override_files(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project / "ricky.toml").write_text('[providers.openrouter]\ndefault_model = "from-config"\n')
    monkeypatch.setenv("RICKY_PROVIDERS__OPENROUTER__DEFAULT_MODEL", "from-env")

    assert RickySettings().providers.openrouter.default_model == "from-config"


def test_credentials_are_not_loaded_from_environment(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")

    settings = RickySettings()

    assert settings.openrouter_api_key is None
    assert settings.anthropic_api_key is None


def test_explicit_override_wins(project: Path) -> None:
    (project / "ricky.toml").write_text('[providers.openrouter]\ndefault_model = "from-config"\n')
    providers = ProvidersSettings(openrouter=OpenRouterSettings(default_model="explicit/model"))

    settings = RickySettings(providers=providers, max_turn_iterations=3)

    assert settings.providers.openrouter.default_model == "explicit/model"
    assert settings.max_turn_iterations == 3


def test_resolve_selection_uses_provider_default_and_explicit_overrides(project: Path) -> None:
    settings = RickySettings(
        default_provider="anthropic",
        providers=ProvidersSettings(
            openrouter=OpenRouterSettings(default_model="openrouter/default")
        ),
    )

    assert settings.resolve_selection() == ModelSelection(
        provider="anthropic", model="claude-sonnet-5"
    )
    assert settings.resolve_selection("openrouter") == ModelSelection(
        provider="openrouter", model="openrouter/default"
    )
    assert settings.resolve_selection("openrouter", "explicit/model") == ModelSelection(
        provider="openrouter", model="explicit/model"
    )


def test_resolve_selection_rejects_unknown_provider(project: Path) -> None:
    with pytest.raises(ValueError, match="openrouter, anthropic, claude_code"):
        RickySettings().resolve_selection("unknown")


def test_profile_registry_resolves_shared_default_and_multi_profile_scope(project: Path) -> None:
    settings = RickySettings()

    default_scope = settings.resolve_profile_scope()
    dashboard_scope = settings.resolve_profile_scope("shared", access_profiles=["personal", "work"])

    assert default_scope.primary == "personal"
    assert default_scope.profiles == ("shared", "personal")
    assert dashboard_scope.profiles == ("shared", "personal", "work")
    with pytest.raises(ValueError, match="unknown or disabled"):
        settings.resolve_profile_scope("consulting")


def test_profile_selection_uses_primary_defaults_and_intersects_provider_policy(
    project: Path,
) -> None:
    settings = RickySettings()

    assert settings.resolve_profile_selection(settings.resolve_profile_scope("personal")) == (
        ModelSelection(provider="openrouter", model="anthropic/claude-sonnet-4")
    )
    assert settings.resolve_profile_selection(settings.resolve_profile_scope("work")) == (
        ModelSelection(provider="claude_code", model="sonnet")
    )
    dashboard = settings.resolve_profile_scope("shared", access_profiles=["personal", "work"])
    assert settings.resolve_profile_selection(dashboard) == ModelSelection(
        provider="claude_code", model="sonnet"
    )
    with pytest.raises(ValueError, match="not allowed by profile scope"):
        settings.resolve_profile_selection(dashboard, provider="openrouter")


def test_profile_model_defaults_are_primary_and_added_profiles_do_not_override(
    project: Path,
) -> None:
    settings = RickySettings(
        profiles=ProfilesSettings(
            default="personal",
            enabled=["shared", "personal", "work"],
            definitions={
                "shared": ProfileDefinitionSettings(
                    default_provider="anthropic",
                    default_models={"anthropic": "shared-model"},
                ),
                "personal": ProfileDefinitionSettings(
                    default_provider="openrouter",
                    default_models={"openrouter": "personal-model"},
                ),
                "work": ProfileDefinitionSettings(
                    default_provider="claude_code",
                    allowed_providers=["openrouter", "anthropic", "claude_code"],
                ),
            },
        )
    )
    scope = settings.resolve_profile_scope("personal", access_profiles=["work"])

    assert settings.resolve_profile_selection(scope) == ModelSelection(
        provider="openrouter", model="personal-model"
    )


def test_profile_paths_are_confined_to_distinct_user_root(project: Path) -> None:
    settings = RickySettings(
        user_data_dir=str(project),
        project_data_dir=str(project.parent / "project-data"),
    )

    assert profile_data_path(settings, "work") == project / "profiles" / "work"
    assert profile_config_file("work", project) == project / "profiles" / "work" / "ricky.toml"
    assert profile_secrets_file("work", project) == (
        project / "profiles" / "work" / ".secrets.toml"
    )
    assert not Path(settings.project_data_dir).exists()


def test_profile_files_load_typed_definition_config_and_secrets(project: Path) -> None:
    personal_root = project / "profiles" / "personal"
    personal_root.mkdir(parents=True)
    (personal_root / "ricky.toml").write_text(
        """[profile]
description = "Personal household context"
routing_hints = ["Family and household requests"]
default_provider = "anthropic"
allowed_providers = ["anthropic"]

[profile.default_models]
anthropic = "personal-claude"

[google.accounts.home]
email = "alex@example.com"
"""
    )
    (personal_root / ".secrets.toml").write_text('anthropic_api_key = "personal-secret"\n')

    settings = RickySettings()

    definition = settings.profiles.definitions["personal"]
    profile_config = settings.profile_configs["personal"]
    assert definition.description == "Personal household context"
    assert definition.default_provider == "anthropic"
    assert definition.default_models == {"anthropic": "personal-claude"}
    assert profile_config.google is not None
    assert profile_config.google.accounts["home"].email == "alex@example.com"
    assert profile_config.anthropic_api_key is not None
    assert profile_config.anthropic_api_key.get_secret_value() == "personal-secret"
    assert "personal-secret" not in repr(settings)
    assert settings.resolve_profile_selection(settings.resolve_profile_scope()) == ModelSelection(
        provider="anthropic", model="personal-claude"
    )


def test_profile_runtime_unions_only_accessible_messaging_accounts(project: Path) -> None:
    (project / "ricky.toml").write_text(
        """[messaging.transports.telegram-home]
type = "telegram"
account = "personal/home"

[messaging.routes.home]
transport = "telegram-home"
destination = "200"
owner_profile = "personal"
accepted_profiles = ["shared", "personal"]
"""
    )
    for profile, account in (("personal", "home"), ("work", "company")):
        root = project / "profiles" / profile
        root.mkdir(parents=True)
        (root / ".secrets.toml").write_text(
            f'[messaging.telegram_accounts.{account}]\nbot_token = "{profile}-token"\n'
        )

    settings = RickySettings()
    personal = settings.resolve_profile_runtime_settings(settings.resolve_profile_scope("personal"))
    dashboard = settings.resolve_profile_runtime_settings(
        settings.resolve_profile_scope("personal", access_profiles=["work"])
    )

    assert settings.messaging.telegram_accounts == {}
    assert set(personal.messaging.telegram_accounts) == {"personal/home"}
    assert set(dashboard.messaging.telegram_accounts) == {"personal/home", "work/company"}
    assert dashboard.messaging.transports == settings.messaging.transports
    assert dashboard.messaging.routes == settings.messaging.routes


def test_profile_runtime_removes_inaccessible_messaging_topology(project: Path) -> None:
    (project / "ricky.toml").write_text(
        """[messaging.transports.telegram-home]
type = "telegram"
account = "personal/home"

[messaging.routes.home]
transport = "telegram-home"
destination = "200"
owner_profile = "personal"
accepted_profiles = ["shared", "personal", "work"]

[gateway.routes.home]
provider = "openrouter"
model = "openai/gpt-5.6-luna"
primary_profile = "personal"
"""
    )
    personal_root = project / "profiles" / "personal"
    personal_root.mkdir(parents=True)
    (personal_root / ".secrets.toml").write_text(
        '[messaging.telegram_accounts.home]\nbot_token = "personal-token"\n'
    )

    settings = RickySettings()
    runtime = settings.resolve_profile_runtime_settings(settings.resolve_profile_scope("work"))

    assert runtime.messaging.telegram_accounts == {}
    assert runtime.messaging.transports == {}
    assert runtime.messaging.routes == {}
    assert runtime.gateway.routes == {}
    assert RickySettings.model_validate(runtime) is runtime


def test_profile_runtime_qualifies_same_named_messaging_accounts(project: Path) -> None:
    for profile in ("personal", "work"):
        root = project / "profiles" / profile
        root.mkdir(parents=True)
        (root / ".secrets.toml").write_text(
            f'[messaging.telegram_accounts.bot]\nbot_token = "{profile}-token"\n'
        )

    settings = RickySettings()
    scope = settings.resolve_profile_scope("personal", access_profiles=["work"])

    runtime = settings.resolve_profile_runtime_settings(scope)

    assert set(runtime.messaging.telegram_accounts) == {"personal/bot", "work/bot"}


def test_profile_runtime_filters_precomposed_messaging_accounts_to_scope(project: Path) -> None:
    settings = RickySettings.model_validate(
        {
            "user_data_dir": str(project),
            "messaging": {
                "telegram_accounts": {
                    "personal/bot": {"bot_token": "personal-token"},
                    "work/bot": {"bot_token": "work-token"},
                }
            },
        }
    )

    runtime = settings.resolve_profile_runtime_settings(settings.resolve_profile_scope("personal"))

    assert set(runtime.messaging.telegram_accounts) == {"personal/bot"}


def test_messaging_transport_accounts_are_strictly_qualified_and_owned(
    project: Path,
) -> None:
    with pytest.raises(ValidationError, match="profile/name"):
        MessagingTransportSettings(type="telegram", account="bot")

    transport = MessagingTransportSettings(type="telegram", account="personal/bot")
    assert type(transport).model_validate_json(transport.model_dump_json()) == transport
    assert transport.account_ref.qualified == "personal/bot"

    messaging = MessagingSettings.model_validate(
        {
            "telegram_accounts": {"personal/bot": {"bot_token": "test-token"}},
            "transports": {
                "telegram": {"type": "telegram", "account": "personal/bot"},
            },
            "routes": {
                "work": {
                    "transport": "telegram",
                    "destination": "200",
                    "owner_profile": "work",
                    "accepted_profiles": ["shared", "work"],
                }
            },
        }
    )
    with pytest.raises(ValueError, match="must own transport account"):
        RickySettings(user_data_dir=str(project), messaging=messaging)

    unknown = MessagingSettings.model_validate(
        {
            "transports": {
                "telegram": {"type": "telegram", "account": "personal/missing"},
            }
        }
    )
    with pytest.raises(ValueError, match="unknown Telegram account"):
        RickySettings(user_data_dir=str(project), messaging=unknown)


def test_gateway_routes_require_same_named_messaging_clearance(project: Path) -> None:
    transport = MessagingTransportSettings(type="telegram", account="personal/bot")
    gateway_route = GatewayRouteSettings(
        provider="claude_code",
        model="sonnet",
        primary_profile="personal",
        access_profiles=["work"],
    )

    with pytest.raises(ValueError, match="same-named messaging route"):
        RickySettings(
            user_data_dir=str(project),
            messaging=MessagingSettings(transports={"telegram": transport}),
            gateway=GatewaySettings(routes={"owner": gateway_route}),
        )

    with pytest.raises(ValueError, match=r"does not accept gateway profile\(s\): work"):
        RickySettings(
            user_data_dir=str(project),
            messaging=MessagingSettings(
                transports={"telegram": transport},
                routes={
                    "owner": MessagingRouteSettings(
                        transport="telegram",
                        destination="200",
                        owner_profile="personal",
                        accepted_profiles=["shared", "personal"],
                    )
                },
            ),
            gateway=GatewaySettings(routes={"owner": gateway_route}),
        )


@pytest.mark.parametrize(
    (
        "section",
        "installation",
        "profile_override",
        "set_path",
        "set_value",
        "sibling_path",
        "sibling_value",
    ),
    [
        (
            "providers",
            {"anthropic": {"default_model": "installation/model", "default_max_tokens": 4096}},
            {"anthropic": {"default_max_tokens": 2048}},
            "providers.anthropic.default_max_tokens",
            2048,
            "providers.anthropic.default_model",
            "installation/model",
        ),
        (
            "context",
            {"response_reserve_tokens": 6000, "safety_margin_tokens": 777},
            {"safety_margin_tokens": 100},
            "context.safety_margin_tokens",
            100,
            "context.response_reserve_tokens",
            6000,
        ),
        (
            "gmail",
            {"body_char_limit": 9000, "attachment_count_limit": 3},
            {"default_list_limit": 12},
            "gmail.default_list_limit",
            12,
            "gmail.body_char_limit",
            9000,
        ),
        (
            "gcal",
            {"default_window_days": 14, "description_char_limit": 9000},
            {"default_list_limit": 12},
            "gcal.default_list_limit",
            12,
            "gcal.default_window_days",
            14,
        ),
        (
            "google",
            {"token_store_path": "google/installation.json", "auth_callback_timeout_seconds": 60},
            {"auth_callback_timeout_seconds": 30},
            "google.auth_callback_timeout_seconds",
            30.0,
            "google.token_store_path",
            "google/installation.json",
        ),
        (
            "workflow",
            {"run_dir": "installation-runs", "max_parallel_steps": 8, "max_result_chars": 90000},
            {"max_parallel_steps": 2},
            "workflow.max_parallel_steps",
            2,
            "workflow.run_dir",
            "installation-runs",
        ),
        (
            "agents",
            {"ad_hoc_background": {"execution": {"wall_clock_seconds": 100, "iterations": 8}}},
            {"ad_hoc_background": {"execution": {"iterations": 2}}},
            "agents.ad_hoc_background.execution.iterations",
            2,
            "agents.ad_hoc_background.execution.wall_clock_seconds",
            100.0,
        ),
        (
            "authority",
            {
                "enabled": True,
                "store_path": "installation/authority.sqlite3",
                "max_ttl_seconds": 3600,
                "source_snapshot_chars": 4000,
            },
            {"source_snapshot_chars": 1000},
            "authority.source_snapshot_chars",
            1000,
            "authority.store_path",
            "installation/authority.sqlite3",
        ),
    ],
)
def test_profile_runtime_merges_sparse_nested_sections_field_by_field(
    project: Path,
    section: str,
    installation: dict[str, object],
    profile_override: dict[str, object],
    set_path: str,
    set_value: object,
    sibling_path: str,
    sibling_value: object,
) -> None:
    project_data = project.parent / "project-data"
    settings = RickySettings.model_validate(
        {
            "user_data_dir": str(project),
            "project_data_dir": str(project_data),
            "profile_configs": {
                "personal": ProfileConfigSettings.model_validate({section: profile_override})
            },
            section: installation,
        }
    )

    runtime = settings.resolve_profile_runtime_settings(settings.resolve_profile_scope())

    def value_at(path: str) -> object:
        value: object = runtime
        for part in path.split("."):
            value = getattr(value, part)
        return value

    assert value_at(set_path) == set_value
    assert value_at(sibling_path) == sibling_value
    assert user_data_path(settings) == project
    assert not project_data.exists()


def test_profile_runtime_intersects_authority_ceilings_across_scope(project: Path) -> None:
    root_capability = {
        "enabled": True,
        "max_effect_calls": 5,
        "max_ttl_seconds": 3600,
        "max_financial_limit_minor": 100,
        "currency": "USD",
        "allowed_profiles": ["personal", "work"],
    }
    settings = RickySettings(
        user_data_dir=str(project),
        project_data_dir=str(project.parent / "project-data"),
        authority=AuthoritySettings.model_validate(
            {
                "enabled": True,
                "allowed_principals": ["principal:a", "principal:b"],
                "default_ttl_seconds": 120,
                "max_ttl_seconds": 3600,
                "max_effect_calls": 5,
                "capabilities": {"sandbox": root_capability},
            }
        ),
        profile_configs={
            "personal": ProfileConfigSettings.model_validate(
                {
                    "authority": {
                        "enabled": True,
                        "allowed_principals": ["principal:a", "principal:b"],
                        "default_ttl_seconds": 500,
                        "max_ttl_seconds": 10_000,
                        "max_effect_calls": 10,
                        "capabilities": {
                            "sandbox": {
                                **root_capability,
                                "max_effect_calls": 10,
                                "max_ttl_seconds": 10_000,
                                "allowed_profiles": ["personal"],
                            }
                        },
                    }
                }
            ),
            "work": ProfileConfigSettings.model_validate(
                {
                    "authority": {
                        "enabled": False,
                        "allowed_principals": ["principal:a"],
                        "default_ttl_seconds": 60,
                        "max_ttl_seconds": 60,
                        "max_effect_calls": 1,
                        "capabilities": {
                            "sandbox": {
                                "enabled": False,
                                "max_effect_calls": 1,
                                "max_ttl_seconds": 60,
                                "max_financial_limit_minor": 0,
                                "allowed_profiles": ["work"],
                            }
                        },
                    }
                }
            ),
        },
    )

    runtime = settings.resolve_profile_runtime_settings(
        settings.resolve_profile_scope("personal", access_profiles=["work"])
    )

    assert runtime.authority.enabled is False
    assert runtime.authority.max_effect_calls == 1
    assert runtime.authority.default_ttl_seconds == 60
    assert runtime.authority.max_ttl_seconds == 60
    assert runtime.authority.allowed_principals == ["principal:a"]
    assert runtime.authority.capabilities["sandbox"].enabled is False
    assert runtime.authority.capabilities["sandbox"].allowed_profiles == []
    assert runtime.authority.store_path == settings.authority.store_path
    assert not Path(settings.project_data_dir).exists()


def test_slack_download_owner_follows_profile_credential(project: Path) -> None:
    settings = RickySettings(
        user_data_dir=str(project),
        project_data_dir=str(project.parent / "project-data"),
        profile_configs={
            "personal": ProfileConfigSettings.model_validate({"slack_user_token": "personal-token"})
        },
    )

    runtime = settings.resolve_profile_runtime_settings(settings.resolve_profile_scope())

    assert runtime.slack_user_token is not None
    assert runtime.slack.download_dir == "profiles/personal/downloads/slack"
    assert not Path(settings.project_data_dir).exists()


def test_profile_file_rejects_installation_wide_or_unknown_settings(project: Path) -> None:
    personal_root = project / "profiles" / "personal"
    personal_root.mkdir(parents=True)
    (personal_root / "ricky.toml").write_text('user_data_dir = "/tmp/escape"\n')

    with pytest.raises(ValidationError, match="user_data_dir"):
        RickySettings()


def test_write_default_selection_preserves_comments_and_unrelated_keys(project: Path) -> None:
    config_path = project / "ricky.toml"
    config_path.write_text(
        """# keep this comment
default_provider = "openrouter"
max_turn_iterations = 9

[providers.openrouter]
default_model = "old/model"

[unrelated]
value = "keep"
"""
    )

    written = write_default_selection(
        ModelSelection(provider="anthropic", model="claude-test"),
        root=project,
    )

    text = written.read_text()
    parsed = tomllib.loads(text)
    assert written == config_path
    assert "# keep this comment" in text
    assert parsed["default_provider"] == "anthropic"
    assert parsed["providers"]["openrouter"]["default_model"] == "old/model"
    assert parsed["providers"]["anthropic"]["default_model"] == "claude-test"
    assert parsed["unrelated"]["value"] == "keep"
    assert parsed["max_turn_iterations"] == 9


def test_write_default_selection_creates_missing_file_and_tables(project: Path) -> None:
    path = write_default_selection(
        ModelSelection(provider="openrouter", model="new/model"),
        root=project,
    )

    parsed = tomllib.loads(path.read_text())
    assert parsed == {
        "default_provider": "openrouter",
        "providers": {"openrouter": {"default_model": "new/model"}},
    }


def test_write_default_selection_updates_only_selected_profile(project: Path) -> None:
    root_config = project / "ricky.toml"
    root_config.write_text('[profiles]\ndefault = "personal"\n')
    work_config = project / "profiles" / "work" / "ricky.toml"
    work_config.parent.mkdir(parents=True)
    work_config.write_text('# keep\n[profile]\ndescription = "Work"\n')

    path = write_default_selection(
        ModelSelection(provider="claude_code", model="opus"),
        root=project,
        profile="work",
    )

    parsed = tomllib.loads(path.read_text())
    assert path == work_config
    assert "# keep" in path.read_text()
    assert parsed["profile"] == {
        "description": "Work",
        "default_provider": "claude_code",
        "default_models": {"claude_code": "opus"},
    }
    assert root_config.read_text() == '[profiles]\ndefault = "personal"\n'


def test_write_profile_secret_is_private_atomic_and_preserves_other_keys(project: Path) -> None:
    secrets = project / "profiles" / "shared" / ".secrets.toml"
    secrets.parent.mkdir(parents=True)
    secrets.write_text('brave_search_api_key = "keep"\n', encoding="utf-8")

    written = write_profile_secret(
        "anthropic_api_key",
        SecretStr("new-secret"),
        profile="shared",
        root=project,
    )

    assert written == secrets
    assert tomllib.loads(secrets.read_text(encoding="utf-8")) == {
        "brave_search_api_key": "keep",
        "anthropic_api_key": "new-secret",
    }
    assert secrets.stat().st_mode & 0o777 == 0o600


def test_write_profile_secret_refuses_symlink_destination(project: Path) -> None:
    outside = project.parent / "outside-secrets"
    outside.write_text('brave_search_api_key = "keep"\n', encoding="utf-8")
    secrets = project / "profiles" / "shared" / ".secrets.toml"
    secrets.parent.mkdir(parents=True)
    secrets.symlink_to(outside)

    with pytest.raises(ValueError, match="symbolic link"):
        write_profile_secret(
            "openrouter_api_key",
            SecretStr("must-not-write"),
            profile="shared",
            root=project,
        )

    assert outside.read_text(encoding="utf-8") == 'brave_search_api_key = "keep"\n'


def test_slack_defaults_and_config_table(project: Path) -> None:
    assert RickySettings().slack.download_dir == "downloads/slack"
    assert RickySettings().slack.default_history_limit == 30
    assert RickySettings().slack.api_base_url == "https://slack.com/api"
    assert RickySettings().slack_user_token is None

    (project / "ricky.toml").write_text(
        '[slack]\ndownload_dir = "elsewhere"\ndefault_history_limit = 5\n'
    )
    settings = RickySettings()
    assert settings.slack.download_dir == "elsewhere"
    assert settings.slack.default_history_limit == 5


def test_slack_token_loads_only_from_profile_secrets(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    personal_root = project / "profiles" / "personal"
    personal_root.mkdir(parents=True)
    (personal_root / ".secrets.toml").write_text('slack_user_token = "xoxp-from-file"\n')
    settings = RickySettings()
    from_file = settings.resolve_profile_runtime_settings(
        settings.resolve_profile_scope()
    ).slack_user_token
    assert from_file is not None
    assert from_file.get_secret_value() == "xoxp-from-file"

    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-from-env")
    from_env = RickySettings().slack_user_token
    assert from_env is None


def test_secret_not_leaked_in_repr(project: Path) -> None:
    personal_root = project / "profiles" / "personal"
    personal_root.mkdir(parents=True)
    (personal_root / ".secrets.toml").write_text(
        'openrouter_api_key = "openrouter-secret"\nanthropic_api_key = "anthropic-secret"\n'
    )

    representation = repr(RickySettings())

    assert "openrouter-secret" not in representation
    assert "anthropic-secret" not in representation


def test_google_and_gmail_config_with_per_account_oauth_clients(project: Path) -> None:
    personal_root = project / "profiles" / "personal"
    personal_root.mkdir(parents=True)
    (personal_root / "ricky.toml").write_text(
        """[google]
token_store_path = "google/custom.json"
auth_callback_timeout_seconds = 45.0

[google.accounts.primary]
email = "alex.personal@example.com"

[gmail]
download_dir = "downloads/custom-gmail"
default_list_limit = 12
body_char_limit = 9000
"""
    )
    (personal_root / ".secrets.toml").write_text(
        """[google_oauth_clients.primary]
client_id = "personal-client"
client_secret = "personal-secret"
"""
    )

    settings = RickySettings()
    runtime = settings.resolve_profile_runtime_settings(settings.resolve_profile_scope())

    assert settings.google.accounts == {}
    assert runtime.google.accounts["personal/primary"].email == "alex.personal@example.com"
    assert runtime.google.token_store_path == "google/custom.json"
    assert runtime.google.auth_callback_timeout_seconds == 45.0
    assert runtime.gmail.download_dir == "downloads/custom-gmail"
    assert runtime.gmail.default_list_limit == 12
    assert runtime.gmail.body_char_limit == 9000
    assert runtime.google_oauth_clients["personal/primary"].client_id == "personal-client"
    assert (
        runtime.google_oauth_clients["personal/primary"].client_secret.get_secret_value()
        == "personal-secret"
    )
    representation = repr(settings)
    assert "personal-secret" not in representation


def test_google_and_gmail_defaults(project: Path) -> None:
    settings = RickySettings()

    assert settings.google.accounts == {}
    assert settings.google_oauth_clients == {}
    assert settings.google.token_store_path == "google/tokens.json"
    assert settings.google.userinfo_url == "https://openidconnect.googleapis.com/v1/userinfo"
    assert settings.gmail.download_dir == "downloads/gmail"
    assert settings.gmail.default_list_limit == 25
    assert settings.gmail.body_char_limit == 20_000


def test_runtime_artifact_paths_are_relative_to_user_data_dir(project: Path) -> None:
    settings = RickySettings()

    assert settings.slack.download_dir == "downloads/slack"
    assert settings.gmail.download_dir == "downloads/gmail"
    assert settings.web_search.download_dir == "downloads/web"
    assert settings.google.token_store_path == "google/tokens.json"

    invalid_settings = (
        lambda: SlackSettings(download_dir="../outside"),
        lambda: GmailSettings(download_dir="/tmp/outside"),
        lambda: WebSearchSettings(download_dir="."),
        lambda: GoogleSettings(token_store_path="google/../../outside.json"),
    )
    for build in invalid_settings:
        with pytest.raises(ValidationError, match="must stay below user_data_dir"):
            build()
