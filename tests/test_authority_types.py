"""Grants and scopes are strict and survive JSON round trips."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from authority_support import COMPLETE_CONSTRAINTS, grant_source
from ricky.authority.types import (
    AuthorityScope,
    AuthorityVerdict,
    DelegationGrant,
    GrantActivity,
    source_text_digest,
)
from ricky.profiles import ProfileScope


def _grant(**overrides: object) -> DelegationGrant:
    issued = datetime.now(UTC)
    payload: dict[str, object] = {
        "id": "grant_" + "a" * 32,
        "source": grant_source(),
        "task_id": "task_" + "b" * 32,
        "task_revision": 3,
        "profile_scope": ProfileScope.create("personal"),
        "contract_id": "contract_" + "c" * 32,
        "contract_digest": "d" * 64,
        "scopes": (
            AuthorityScope(
                capability="sandbox_reservation",
                schema_id="sandbox.reservation",
                schema_version=1,
                constraints=dict(COMPLETE_CONSTRAINTS),
            ),
        ),
        "summary": "Make at most one sandbox reservation.",
        "effect_call_limit": 1,
        "issued_at": issued,
        "expires_at": issued + timedelta(hours=1),
        "status": "active",
        "policy_digest": "d" * 64,
    }
    payload.update(overrides)
    return DelegationGrant.model_validate(payload)


def test_grant_and_scope_survive_a_json_round_trip() -> None:
    grant = _grant()
    restored = DelegationGrant.model_validate_json(grant.model_dump_json())
    assert restored == grant
    assert restored.scopes[0].constraints == COMPLETE_CONSTRAINTS
    assert restored.capabilities() == frozenset({"sandbox_reservation"})
    assert restored.scope_for("sandbox_reservation") is not None
    assert restored.scope_for("other") is None


def test_a_grant_is_immutable_and_task_namespaced() -> None:
    grant = _grant()
    with pytest.raises(ValidationError):
        grant.effect_call_limit = 99  # type: ignore[misc]
    assert grant.effect_namespace() == f"delegated:{grant.task_id}"


def test_extra_fields_naive_times_and_inverted_expiry_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _grant(unexpected="widening")
    with pytest.raises(ValidationError):
        _grant(issued_at=datetime.now())
    issued = datetime.now(UTC)
    with pytest.raises(ValidationError):
        _grant(issued_at=issued, expires_at=issued)


def test_a_grant_cannot_carry_two_scopes_for_one_capability() -> None:
    scope = AuthorityScope(
        capability="sandbox_reservation",
        schema_id="sandbox.reservation",
        schema_version=1,
        constraints=dict(COMPLETE_CONSTRAINTS),
    )
    with pytest.raises(ValidationError):
        _grant(scopes=(scope, scope))


def test_a_financial_limit_requires_a_currency() -> None:
    with pytest.raises(ValidationError):
        _grant(financial_limit_minor=500)
    assert _grant(financial_limit_minor=500, currency="USD").currency == "USD"


def test_a_priced_verdict_requires_a_currency() -> None:
    with pytest.raises(ValidationError):
        AuthorityVerdict(allowed=True, reason="ok", amount_minor=100)
    assert AuthorityVerdict(allowed=True, reason="ok", amount_minor=100, currency="USD")


def test_activity_records_are_strict_and_utc() -> None:
    activity = GrantActivity(
        id=1,
        grant_id="grant_" + "a" * 32,
        kind="used",
        profile_label=ProfileScope.create("personal").label(),
        summary="sandbox_reserve resolved performed",
        created_at=datetime.now(UTC),
    )
    assert GrantActivity.model_validate_json(activity.model_dump_json()) == activity
    with pytest.raises(ValidationError):
        GrantActivity(
            id=1,
            grant_id="not-a-grant",
            kind="used",
            profile_label=ProfileScope.create("personal").label(),
            summary="x",
            created_at=datetime.now(UTC),
        )


def test_source_text_digest_is_exact() -> None:
    assert source_text_digest("book it") == source_text_digest("book it")
    assert source_text_digest("book it") != source_text_digest("book it ")
