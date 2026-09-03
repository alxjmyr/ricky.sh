"""Canonical first-class profile type contracts."""

import pytest
from pydantic import ValidationError

from ricky.profiles import ProfileLabel, ProfileResourceRef, ProfileRoutingDecision, ProfileScope


def test_profile_scope_is_canonical_json_safe_and_always_shared() -> None:
    scope = ProfileScope.create("work", access_profiles=["personal"])

    assert scope.primary == "work"
    assert scope.profiles == ("shared", "personal", "work")
    assert ProfileScope.model_validate_json(scope.model_dump_json()) == scope
    assert scope.digest() == ProfileScope.create("work", access_profiles=["personal"]).digest()


def test_profile_scope_rejects_missing_shared_duplicate_and_widening() -> None:
    with pytest.raises(ValidationError, match="include shared"):
        ProfileScope(primary="personal", profiles=("personal",))
    with pytest.raises(ValidationError, match="duplicates"):
        ProfileScope(primary="personal", profiles=("shared", "personal", "personal"))

    parent = ProfileScope.create("work")
    with pytest.raises(ValueError, match="cannot add profiles"):
        parent.narrow("personal")


def test_scope_checks_labels_and_resource_references_are_qualified() -> None:
    scope = ProfileScope.create("shared", access_profiles=["personal", "work"])

    assert scope.permits(ProfileLabel.owned_by("personal"))
    assert scope.permits(ProfileLabel(required_profiles=("personal", "work")))
    assert not ProfileScope.create("personal").permits(ProfileLabel.owned_by("work"))
    assert ProfileResourceRef(profile="work", name="google-primary").qualified == (
        "work/google-primary"
    )


def test_routing_decision_is_bounded_and_canonical() -> None:
    decision = ProfileRoutingDecision(
        profiles=("work", "personal"),
        reason="User requested tasks across both contexts.",
    )

    assert decision.profiles == ("personal", "work")
    assert ProfileRoutingDecision.model_validate_json(decision.model_dump_json()) == decision
