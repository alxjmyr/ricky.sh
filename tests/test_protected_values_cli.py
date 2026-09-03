"""Protected-values CLI exposure and non-interactive failure tests."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from ricky.config import RickySettings
from ricky.interfaces.cli.app import app
from ricky.interfaces.cli.protected_values import _parse_field

runner = CliRunner()


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / "project"),
            "protected_values": {
                "enabled": True,
                "argon2_iterations": 1,
                "argon2_lanes": 1,
                "argon2_memory_kib": 8192,
            },
        }
    )


def test_protected_values_help_exposes_management_without_raw_value_options() -> None:
    root = runner.invoke(app, ["protected-values", "--help"])
    add = runner.invoke(app, ["protected-values", "add", "--help"])
    initialize = runner.invoke(app, ["protected-values", "init", "--help"])
    policy = runner.invoke(
        app,
        ["protected-values", "policy", "set", "--help"],
        env={"COLUMNS": "200"},
    )

    assert root.exit_code == add.exit_code == initialize.exit_code == policy.exit_code == 0
    assert "policy" in root.stdout
    assert "approvals" in root.stdout
    assert "--field" in add.stdout
    assert "--value" not in add.stdout
    assert "--passphrase" not in initialize.stdout
    assert "--allow-unattended" in policy.stdout
    assert "--max-unattended-materializations" in policy.stdout
    assert "--allow-unattended-commit" in policy.stdout
    assert "--max-unattended-amount-minor" in policy.stdout


def test_stored_security_code_is_explicit_while_otp_stays_prompt_each_use() -> None:
    security_code = _parse_field("security_code:card_security_code:stored:Card security code")
    assert security_code.mode == "stored"

    try:
        _parse_field("otp:one_time_code:stored:One-time code")
    except ValueError as exc:
        assert "prompt on every use" in str(exc)
    else:
        raise AssertionError("stored one-time codes must remain unavailable")


def test_non_tty_mutation_fails_before_secret_input_or_store_creation(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr("ricky.interfaces.cli.protected_values.load_settings", lambda: settings)

    result = runner.invoke(
        app,
        [
            "protected-values",
            "add",
            "personal/example",
            "--kind",
            "credential",
            "--label",
            "Example",
            "--origin",
            "https://example.com",
        ],
    )

    assert result.exit_code == 1
    assert "requires an interactive" in result.stdout
    assert "terminal" in result.stdout
    assert not (tmp_path / "user").exists()
    assert not (tmp_path / "project").exists()


def test_status_is_provider_free_and_does_not_initialize_a_vault(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr("ricky.interfaces.cli.protected_values.load_settings", lambda: settings)

    result = runner.invoke(app, ["protected-values", "status"])

    assert result.exit_code == 0
    assert '"initialized": false' in result.stdout
    assert '"unlocked": false' in result.stdout
    assert not (tmp_path / "project").exists()
