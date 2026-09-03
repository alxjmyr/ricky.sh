"""Tests for the permission vocabulary models."""

from __future__ import annotations

from ricky.permissions import GrantOption, GrantScope, PermissionResponse


def test_grant_scope_round_trips_json() -> None:
    scope = GrantScope(
        params_equal={"account": "personal"},
        label="gmail_trash on personal",
        allow_unconstrained=True,
    )

    assert GrantScope.model_validate_json(scope.model_dump_json()) == scope


def test_grant_option_round_trips_json() -> None:
    option = GrantOption(id="scoped", label="gmail_trash on personal")

    assert GrantOption.model_validate_json(option.model_dump_json()) == option


def test_permission_response_defaults_to_no_grant() -> None:
    response = PermissionResponse(decision="allow")

    assert response.grant is None
