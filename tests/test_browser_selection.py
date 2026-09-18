"""Scoped browser selection never guesses across profile boundaries."""

import pytest
from pydantic import ValidationError

from ricky.browser.resources import BrowserResourceSelectionError, select_browser_resource
from ricky.config import ProfileBrowserSettings, ProfileConfigSettings, RickySettings
from ricky.profiles import ProfileScope


def settings_for(resources, default=None):
    settings = RickySettings()
    settings.profile_configs = {
        profile: ProfileConfigSettings.model_validate(
            {
                "browser": {
                    "resources": {
                        name: {
                            "kind": "persistent",
                            "headless": headless,
                            "description": "Test browser",
                        }
                        for name, headless in names.items()
                    },
                    "default_resource": default if profile == "personal" else None,
                }
            }
        )
        for profile, names in resources.items()
    }
    return settings


def test_short_name_prefers_primary_and_explicit_reference_stays_exact():
    settings = settings_for(
        {"personal": {"main": True}, "shared": {"main": True}, "work": {"secret": True}}
    )
    scope = ProfileScope.create("personal")
    assert (
        select_browser_resource(settings, scope=scope, name="main").ref.qualified == "personal/main"
    )
    assert (
        select_browser_resource(settings, scope=scope, name="shared/main").ref.qualified
        == "shared/main"
    )
    for name in ("secret", "work/secret", "unknown"):
        with pytest.raises(ValueError, match="unavailable"):
            select_browser_resource(settings, scope=scope, name=name)


def test_default_and_sole_eligible_browser_do_not_fall_back_to_other_profiles():
    scope = ProfileScope.create("personal")
    settings = settings_for(
        {"personal": {"main": True, "headed": False}, "shared": {"other": True}}
    )
    assert select_browser_resource(settings, scope=scope, background=True).ref.name == "main"
    with pytest.raises(BrowserResourceSelectionError, match="Which browser"):
        select_browser_resource(settings, scope=scope)
    settings = settings_for({"personal": {"a": True, "b": True}}, default="b")
    assert select_browser_resource(settings, scope=scope, background=True).ref.name == "b"
    settings = settings_for({"shared": {"other": True}})
    with pytest.raises(BrowserResourceSelectionError, match="your profile"):
        select_browser_resource(settings, scope=scope, background=True)


def test_ambiguous_names_and_ineligible_default_are_not_silently_substituted():
    settings = settings_for({"shared": {"same": True}, "work": {"same": True}})
    scope = ProfileScope.create("personal", access_profiles=("work",))
    with pytest.raises(BrowserResourceSelectionError, match="Which browser"):
        select_browser_resource(settings, scope=scope, name="same", background=True)
    settings = settings_for({"personal": {"headed": False, "headless": True}}, default="headed")
    with pytest.raises(ValueError, match="unavailable"):
        select_browser_resource(settings, scope=scope, background=True)
    settings = settings_for({"personal": {"main": False}, "shared": {"main": True}})
    with pytest.raises(ValueError, match="unavailable"):
        select_browser_resource(settings, scope=scope, name="main", background=True)


def test_default_must_exist_in_owning_profile():
    with pytest.raises(ValidationError, match="default_resource"):
        ProfileBrowserSettings(default_resource="missing")
