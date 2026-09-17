"""Profile-aware capability inventory CLI regression tests."""

from pathlib import Path

from typer.testing import CliRunner

from ricky.config import AgentClassSettings, BrowserSettings, RickySettings
from ricky.interfaces.cli import capabilities as capabilities_cli
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


def test_background_browser_inventory_agrees_across_cli_commands(tmp_path, monkeypatch) -> None:
    settings = RickySettings(
        user_data_dir=str(tmp_path / "user-data"),
        project_data_dir=str(tmp_path / "project-data"),
        browser=BrowserSettings.model_validate(
            {
                "enabled": True,
                "background": {
                    "enabled": True,
                    "read_enabled": True,
                    "interaction_enabled": True,
                    "commit_enabled": True,
                },
            }
        ),
        agents=AgentClassSettings.model_validate(
            {
                "ad_hoc_background": {
                    "guardrail_required_capabilities": [
                        "builtin.browser.read",
                        "builtin.browser.interact",
                        "builtin.browser.commit",
                    ],
                },
            }
        ),
    )
    monkeypatch.setattr(capabilities_cli, "load_settings", lambda: settings)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    listed = runner.invoke(app, ["capability", "list"])
    shown = runner.invoke(app, ["capability", "show", "builtin.browser.interact"])
    validated = runner.invoke(app, ["capability", "validate"])

    for result in (listed, shown, validated):
        assert result.exit_code == 0, result.output
    for capability_id in settings.agents.ad_hoc_background.guardrail_required_capabilities:
        assert capability_id in listed.output
    assert "browser_click" in shown.output
    assert "builtin.browser.handoff" not in listed.output
    assert "builtin.protected_value.use" not in listed.output


def test_validation_does_not_substitute_interactive_browser_for_disabled_background(
    tmp_path, monkeypatch
) -> None:
    settings = RickySettings(
        user_data_dir=str(tmp_path / "user-data"),
        project_data_dir=str(tmp_path / "project-data"),
        browser=BrowserSettings.model_validate({"enabled": True, "background": {"enabled": False}}),
        agents=AgentClassSettings.model_validate(
            {"ad_hoc_background": {"guardrail_required_capabilities": ["builtin.browser.read"]}}
        ),
    )
    monkeypatch.setattr(capabilities_cli, "load_settings", lambda: settings)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(app, ["capability", "validate"])

    assert result.exit_code == 1
    assert "builtin.browser.read" in result.output
    assert "configured capability is not installed" in result.output
