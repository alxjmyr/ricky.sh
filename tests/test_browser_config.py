"""Browser configuration ownership and validation tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ricky.browser.resources import resolve_browser_resources
from ricky.config import (
    BrowserSettings,
    CdpBrowserResourceSettings,
    ProfileBrowserSettings,
    ProfileConfigSettings,
    RickySettings,
    load_settings_at,
)


def test_browser_defaults_are_conservative() -> None:
    browser = RickySettings().browser

    assert browser.enabled is False
    assert browser.headless is False
    assert browser.executable_path is None
    assert browser.ephemeral_dir == "browser/ephemeral"
    assert browser.persistent_dir == "browser/persistent"
    assert browser.lease_dir == "browser/leases"
    assert browser.download_dir == "downloads/browser"
    assert browser.attachment_timeout_seconds == 10.0
    assert browser.max_sessions == 1
    assert browser.upload_count_limit == 10
    assert browser.upload_file_byte_limit == 20_000_000
    assert browser.upload_total_byte_limit == 50_000_000
    assert browser.download_file_byte_limit == 50_000_000
    assert browser.visual_candidate_limit == 100
    assert browser.screenshot_width_limit == 2_000
    assert browser.screenshot_height_limit == 2_000
    assert browser.screenshot_pixel_limit == 4_000_000
    assert browser.screenshot_file_byte_limit == 5_000_000
    assert browser.allowed_private_origins == []
    assert browser.background.enabled is False
    assert browser.background.read_enabled is False
    assert browser.background.interaction_enabled is False
    assert browser.background.protected_values_enabled is False
    assert browser.background.commit_enabled is False
    assert browser.background.allow_ephemeral is False
    assert browser.background.allow_public_https_research is False
    assert browser.background.budget.session_starts == 1
    assert browser.background.budget.scrolls == 100
    assert browser.background.budget.parked_browsers == 1
    media = RickySettings().context.media
    assert media.session_byte_limit == 25_000_000
    assert media.request_image_limit == 2
    assert media.request_image_byte_limit == 10_000_000
    assert media.request_image_pixel_limit == 8_000_000
    assert media.default_image_token_estimate == 8_192


def test_background_browser_owner_policy_is_explicit_and_bounded() -> None:
    browser = BrowserSettings.model_validate(
        {
            "enabled": True,
            "background": {
                "enabled": True,
                "read_enabled": True,
                "interaction_enabled": True,
                "protected_values_enabled": True,
                "commit_enabled": True,
                "allow_ephemeral": True,
                "allow_public_https_research": True,
                "budget": {
                    "session_starts": 1,
                    "navigations": 12,
                    "created_pages": 3,
                    "controlled_pages": 3,
                    "semantic_observations": 20,
                    "visual_observations": 4,
                    "interactions": 15,
                    "protected_materializations": 2,
                    "uploads": 1,
                    "upload_bytes": 1_000,
                    "downloads": 2,
                    "download_bytes": 2_000,
                    "transaction_commits": 1,
                    "parked_browsers": 1,
                    "approval_ttl_seconds": 300,
                },
            },
        }
    )

    assert browser.background.commit_enabled is True
    assert browser.background.budget.transaction_commits == 1


@pytest.mark.parametrize(
    ("background", "message"),
    [
        ({"read_enabled": True}, "background ownership enabled"),
        (
            {"enabled": True, "interaction_enabled": True},
            "mutations require read access",
        ),
        (
            {
                "enabled": True,
                "read_enabled": True,
                "protected_values_enabled": True,
            },
            "requires browser interaction",
        ),
    ],
)
def test_background_browser_feature_dependencies_are_fail_closed(
    background: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        BrowserSettings.model_validate({"background": background})


def test_background_browser_budgets_cannot_widen_installation_limits() -> None:
    with pytest.raises(ValidationError, match="controlled_pages"):
        BrowserSettings.model_validate(
            {
                "max_pages": 2,
                "background": {"enabled": True, "budget": {"controlled_pages": 3}},
            }
        )

    with pytest.raises(ValidationError, match="upload_bytes"):
        BrowserSettings.model_validate(
            {
                "upload_total_byte_limit": 100,
                "upload_file_byte_limit": 100,
                "background": {"enabled": True, "budget": {"upload_bytes": 101}},
            }
        )


def test_browser_loads_root_toml_and_nested_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "user"
    root.mkdir()
    (root / "ricky.toml").write_text(
        """[browser]
enabled = true
headless = false
executable_path = "/opt/google/chrome/google-chrome"
ephemeral_dir = "runtime/browser"
max_pages = 4
allowed_private_origins = ["http://127.0.0.1:8080"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("RICKY_USER_DATA_DIR", str(root))
    monkeypatch.setenv("RICKY_BROWSER__HEADLESS", "true")

    browser = RickySettings().browser

    assert browser.enabled is True
    assert browser.headless is False
    assert browser.executable_path == Path("/opt/google/chrome/google-chrome")
    assert browser.ephemeral_dir == "runtime/browser"
    assert browser.max_pages == 4
    assert browser.allowed_private_origins == ["http://127.0.0.1:8080"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ephemeral_dir", "/tmp/escape"),
        ("persistent_dir", "../profiles"),
        ("lease_dir", "/tmp/leases"),
        ("download_dir", "../downloads"),
    ],
)
def test_browser_data_directories_must_be_confined(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match="must stay below user_data_dir"):
        BrowserSettings.model_validate({field: value})


@pytest.mark.parametrize(
    "origin",
    [
        "ftp://127.0.0.1",
        "http://user:password@127.0.0.1",
        "http://127.0.0.1/path",
        "http://127.0.0.1?token=value",
    ],
)
def test_browser_private_destinations_require_exact_http_origins(origin: str) -> None:
    with pytest.raises(ValidationError, match=r"exact http\(s\) origins"):
        BrowserSettings(allowed_private_origins=[origin])


def test_browser_settings_are_installation_owned(tmp_path: Path) -> None:
    user_root = tmp_path / "user"
    project_root = tmp_path / "project"
    profile = user_root / "profiles" / "personal"
    profile.mkdir(parents=True)
    (profile / "ricky.toml").write_text("[browser]\nenabled = true\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="browser"):
        RickySettings(user_data_dir=str(user_root), project_data_dir=str(project_root))

    assert not project_root.exists()


def test_profile_runtime_preserves_installation_browser_settings(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    settings = RickySettings(
        user_data_dir=str(tmp_path / "user"),
        project_data_dir=str(project_root),
        browser=BrowserSettings(enabled=True, headless=True, max_pages=3),
    )

    runtime = settings.resolve_profile_runtime_settings(settings.resolve_profile_scope())

    assert runtime.browser == settings.browser
    assert not project_root.exists()


def test_profile_files_load_typed_browser_resources_in_issued_scope(tmp_path: Path) -> None:
    root = tmp_path / "user"
    personal = root / "profiles" / "personal"
    work = root / "profiles" / "work"
    personal.mkdir(parents=True)
    work.mkdir(parents=True)
    (personal / "ricky.toml").write_text(
        """[browser.resources.main]
kind = "persistent"
description = "Personal signed-in browser"
headless = false

[browser.resources.debug]
kind = "cdp"
description = "Dedicated local debug browser"
endpoint = "http://127.0.0.1:9222"
""",
        encoding="utf-8",
    )
    (work / "ricky.toml").write_text(
        """[browser.resources.main]
kind = "persistent"
description = "Work signed-in browser"
headless = true
""",
        encoding="utf-8",
    )

    settings = load_settings_at(root)
    personal_scope = settings.resolve_profile_scope("personal")
    combined_scope = settings.resolve_profile_scope("personal", access_profiles=["work"])

    personal_resources = resolve_browser_resources(settings, scope=personal_scope)
    combined_resources = resolve_browser_resources(settings, scope=combined_scope)
    assert [item.ref.qualified for item in personal_resources] == [
        "personal/debug",
        "personal/main",
    ]
    assert [item.ref.qualified for item in combined_resources] == [
        "personal/debug",
        "personal/main",
        "work/main",
    ]


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:9222",
        "http://192.168.1.20:9222",
        "http://user:password@127.0.0.1:9222",
        "http://127.0.0.1:9222/json/version",
        "http://127.0.0.1:9222?token=value",
        "http://127.0.0.1:9222/#fragment",
        "http://127.0.0.1",
    ],
)
def test_cdp_resources_require_an_exact_loopback_http_endpoint(endpoint: str) -> None:
    with pytest.raises(ValidationError, match="loopback"):
        CdpBrowserResourceSettings(
            description="Local browser",
            endpoint=endpoint,
        )


def test_cdp_resource_canonicalizes_ipv6_loopback() -> None:
    resource = CdpBrowserResourceSettings(
        description="Local browser",
        endpoint="http://[::1]:9222/",
    )

    assert resource.endpoint == "http://[::1]:9222"


def test_invalid_cdp_endpoint_is_hidden_from_validation_errors() -> None:
    endpoint = "http://user:password@127.0.0.1:9222"

    with pytest.raises(ValidationError) as rejected:
        CdpBrowserResourceSettings(description="Local browser", endpoint=endpoint)

    assert endpoint not in str(rejected.value)
    assert "password" not in str(rejected.value)


def test_browser_resource_names_are_conservative() -> None:
    with pytest.raises(ValidationError, match="browser resource names"):
        ProfileBrowserSettings.model_validate(
            {
                "resources": {
                    "Not A Safe Name": {
                        "kind": "persistent",
                        "description": "Browser",
                    }
                }
            }
        )


def test_screenshot_disclosure_is_default_deny_and_references_known_providers() -> None:
    profile = ProfileBrowserSettings()

    assert profile.screenshot_allowed_providers == []
    allowed = RickySettings.model_validate(
        {
            "profile_configs": {
                "personal": {
                    "browser": {"screenshot_allowed_providers": ["openrouter", "anthropic"]}
                }
            }
        }
    )
    configured = allowed.profile_configs["personal"].browser
    assert configured is not None
    assert configured.screenshot_allowed_providers == ["openrouter", "anthropic"]

    with pytest.raises(ValidationError, match="unknown provider"):
        RickySettings.model_validate(
            {
                "profile_configs": {
                    "personal": {"browser": {"screenshot_allowed_providers": ["invented"]}}
                }
            }
        )


def test_browser_resource_variants_are_strict_json_round_trip_safe() -> None:
    catalog = ProfileBrowserSettings.model_validate(
        {
            "resources": {
                "main": {
                    "kind": "persistent",
                    "description": "Persistent browser",
                    "headless": True,
                },
                "debug": {
                    "kind": "cdp",
                    "description": "Debug browser",
                    "endpoint": "http://localhost:9222",
                },
            }
        }
    )

    assert ProfileBrowserSettings.model_validate_json(catalog.model_dump_json()) == catalog
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProfileBrowserSettings.model_validate(
            {
                "resources": {
                    "main": {
                        "kind": "persistent",
                        "description": "Browser",
                        "arbitrary_profile_path": "/tmp/chrome",
                    }
                }
            }
        )


@pytest.mark.parametrize(
    "resource",
    [
        {
            "kind": "persistent",
            "description": "Browser",
            "headless": "false",
        },
        {
            "kind": "cdp",
            "description": 123,
            "endpoint": "http://127.0.0.1:9222",
        },
    ],
)
def test_browser_resource_catalog_rejects_scalar_coercion(resource: object) -> None:
    with pytest.raises(ValidationError):
        ProfileBrowserSettings.model_validate({"resources": {"main": resource}})


def test_profile_runtime_keeps_resource_catalog_profile_owned(tmp_path: Path) -> None:
    settings = RickySettings(
        user_data_dir=str(tmp_path / "user"),
        project_data_dir=str(tmp_path / "project"),
        profile_configs={
            "personal": ProfileConfigSettings.model_validate(
                {
                    "browser": {
                        "resources": {
                            "main": {
                                "kind": "persistent",
                                "description": "Personal browser",
                            }
                        }
                    }
                }
            ),
            "work": ProfileConfigSettings.model_validate(
                {
                    "browser": {
                        "resources": {
                            "main": {
                                "kind": "persistent",
                                "description": "Work browser",
                            }
                        }
                    }
                }
            ),
        },
    )

    personal = settings.resolve_profile_runtime_settings(settings.resolve_profile_scope("personal"))

    assert set(personal.profile_configs) == {"personal"}
    assert "work" not in personal.profile_configs


@pytest.mark.parametrize("field", ["binary_dir", "browser_kind"])
def test_removed_browser_configuration_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        BrowserSettings.model_validate({field: "chromium"})


def test_chrome_override_requires_absolute_path() -> None:
    with pytest.raises(ValidationError, match="absolute path"):
        BrowserSettings(executable_path=Path("google-chrome"))
