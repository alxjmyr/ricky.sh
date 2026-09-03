"""Typed foreground gateway configuration and route validation tests."""

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from ricky.config import GatewayRouteSettings, GatewaySettings
from ricky.interfaces.cli.app import app
from ricky.profiles import ProfileScope


def test_gateway_cli_exposes_run_and_bounded_process_commands() -> None:
    result = CliRunner().invoke(app, ["gateway", "--help"])
    assert result.exit_code == 0
    assert "run" in result.stdout
    assert "process" in result.stdout
    assert "transport" in result.stdout
    assert "inbox" in result.stdout

    run_help = CliRunner().invoke(app, ["gateway", "run", "--help"])
    start_help = CliRunner().invoke(app, ["gateway", "service", "start", "--help"])
    restart_help = CliRunner().invoke(app, ["gateway", "service", "restart", "--help"])
    assert run_help.exit_code == start_help.exit_code == restart_help.exit_code == 0
    assert "--unlock-vault" in run_help.stdout
    assert "--unlock-vault" in start_help.stdout
    assert "--unlock-vault" in restart_help.stdout


def test_gateway_settings_are_strict_confined_and_validate_provider() -> None:
    with pytest.raises(ValidationError, match="gateway.store_path"):
        GatewaySettings(store_path="../gateway.sqlite3")
    with pytest.raises(ValidationError, match="unknown provider"):
        GatewaySettings(
            routes={
                "owner": GatewayRouteSettings(
                    provider="unknown",
                    model="model",
                    primary_profile="personal",
                )
            }
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        GatewayRouteSettings.model_validate(
            {
                "provider": "openrouter",
                "model": "model",
                "primary_profile": "personal",
                "destination": "raw-platform-id",
            }
        )


def test_route_profile_scope_is_explicit_and_includes_shared() -> None:
    route = GatewayRouteSettings(
        provider="openrouter",
        model="model",
        primary_profile="work",
        access_profiles=["personal"],
    )

    assert route.profile_scope() == ProfileScope.create("work", access_profiles=("personal",))
