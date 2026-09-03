"""Profile-aware capability inventory CLI regression tests."""

from pathlib import Path

from typer.testing import CliRunner

from ricky.interfaces.cli.app import app


def test_capability_commands_use_default_profile_selection_and_policy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    profile_config = tmp_path / "user-data" / "profiles" / "personal" / "ricky.toml"
    profile_config.parent.mkdir(parents=True)
    profile_config.write_text(
        """[profile]
default_provider = "claude_code"
allowed_providers = ["claude_code"]

[profile.default_models]
claude_code = "sonnet"

[agents.gateway_foreground]
exclude_capabilities = ["builtin.project.read"]
""",
        encoding="utf-8",
    )
    runner = CliRunner()

    listed = runner.invoke(
        app,
        ["capability", "list", "--agent", "gateway_foreground"],
    )
    shown = runner.invoke(app, ["capability", "show", "builtin.project.read"])
    validated = runner.invoke(app, ["capability", "validate"])

    assert listed.exit_code == 0, listed.output
    assert "builtin.project.read" in listed.output
    assert "excluded" in next(
        line for line in listed.output.splitlines() if "builtin.project.read" in line
    )
    assert shown.exit_code == 0, shown.output
    assert "gateway_foreground: excluded" in shown.output
    assert validated.exit_code == 0, validated.output
