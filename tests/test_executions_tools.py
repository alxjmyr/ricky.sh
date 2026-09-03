"""Agent-tool and CLI surface tests for durable executions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from typer.testing import CliRunner

from ricky.executions.tools import execution_tools
from ricky.interfaces.cli.app import app


def test_generic_execution_surface_cannot_create_ad_hoc_profile_requests() -> None:
    names = {tool.name for tool in execution_tools(cast(Any, object()), allowed_routes={"owner"})}
    assert names == {
        "start_named_job",
        "cancel_execution_request",
        "read_execution_request",
        "list_execution_requests",
    }
    assert "create_execution_request" not in names


def test_execution_cli_commands_are_available_and_provider_free(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["execution", "--help"])
    assert result.exit_code == 0
    for command in (
        "list",
        "show",
        "cancel",
        "retry",
        "worker",
        "draft",
        "contract",
        "capability",
    ):
        assert command in result.stdout

    result = runner.invoke(
        app,
        ["execution", "list"],
        env={"RICKY_USER_DATA_DIR": str(tmp_path / "user")},
    )
    assert result.exit_code == 0
    assert "No execution requests found" in result.stdout
