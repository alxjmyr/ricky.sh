"""Named-job read-oriented browser scope tests."""

from pathlib import Path

import pytest

from ricky.agent.session import AgentSession
from ricky.config import RickySettings
from ricky.jobs.runner import JobConfigurationError, _named_job_browser_scope
from ricky.jobs.spec import JobBrowser, JobSpec, JobTools
from ricky.profiles import ProfileResourceRef, ProfileScope

SCOPE = ProfileScope.create("personal")


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "default_provider": "openrouter",
            "providers": {"openrouter": {"default_model": "test-model"}},
            "browser": {
                "enabled": True,
                "background": {
                    "enabled": True,
                    "read_enabled": True,
                    "interaction_enabled": True,
                    "allow_ephemeral": True,
                    "allow_public_https_research": True,
                },
            },
            "profile_configs": {
                "personal": {
                    "browser": {
                        "screenshot_allowed_providers": ["openrouter"],
                        "resources": {
                            "research": {
                                "kind": "persistent",
                                "description": "Dedicated research profile.",
                                "headless": True,
                            }
                        },
                    }
                }
            },
        }
    )


def _session(settings: RickySettings) -> AgentSession:
    return AgentSession.create(
        settings,
        profile_scope=SCOPE,
        provider="openrouter",
        model="test-model",
    )


def _spec(*, tools: list[str], browser: JobBrowser | None) -> JobSpec:
    return JobSpec(
        version=3,
        name="research",
        description="Research public evidence.",
        provider="openrouter",
        model="test-model",
        goal="Find the answer.",
        tools=JobTools(allow=tools),
        browser=browser,
    )


def test_named_job_compiles_exact_ephemeral_read_scope(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    spec = _spec(
        tools=[
            "browser_session_open",
            "browser_navigate",
            "browser_snapshot",
            "browser_visual_snapshot",
        ],
        browser=JobBrowser(
            allow_public_https_research=True,
            allow_masked_visual_observations=True,
        ),
    )

    scope = _named_job_browser_scope(spec, settings=settings, session=_session(settings))

    assert scope is not None
    assert scope.mode == "read_only"
    assert scope.allow_ephemeral is True
    assert scope.allow_public_https_research is True
    assert scope.allow_masked_visual_observations is True
    assert scope.resources == ()
    assert set(scope.allowed_operations) == {
        "controlled_pages",
        "navigations",
        "semantic_observations",
        "session_starts",
        "visual_observations",
    }


def test_named_job_pins_headless_persistent_resource_and_origins(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    resource = ProfileResourceRef(profile="personal", name="research")
    spec = _spec(
        tools=[
            "browser_session_open_resource",
            "browser_navigate",
            "browser_snapshot",
        ],
        browser=JobBrowser(
            resource=resource,
            allowed_origins=("https://example.com",),
        ),
    )

    scope = _named_job_browser_scope(spec, settings=settings, session=_session(settings))

    assert scope is not None
    assert scope.allow_ephemeral is False
    assert len(scope.resources) == 1
    assert scope.resources[0].resource == resource
    assert scope.resources[0].authenticated_origin_ceiling == ("https://example.com",)
    assert len(scope.resources[0].configuration_digest) == 64


@pytest.mark.parametrize(
    ("tools", "browser", "message"),
    [
        (["browser_snapshot"], None, r"explicit \[browser\]"),
        (
            ["browser_session_open", "browser_click"],
            JobBrowser(allow_public_https_research=True),
            "read-oriented",
        ),
        (
            ["browser_session_open", "browser_snapshot"],
            JobBrowser(
                resource=ProfileResourceRef(profile="personal", name="research"),
                allowed_origins=("https://example.com",),
            ),
            "must expose browser_session_open_resource",
        ),
    ],
)
def test_named_job_rejects_missing_or_mutating_browser_scope(
    tmp_path: Path,
    tools: list[str],
    browser: JobBrowser | None,
    message: str,
) -> None:
    settings = _settings(tmp_path)
    spec = _spec(tools=tools, browser=browser)

    with pytest.raises(JobConfigurationError, match=message):
        _named_job_browser_scope(spec, settings=settings, session=_session(settings))
