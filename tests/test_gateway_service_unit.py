"""Deterministic, shell-safe managed systemd user unit rendering and install."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from gateway_ops_support import FakeRunner, settings
from ricky.config import GatewayServiceSettings
from ricky.gateway.service_unit import MARKER, GatewayServiceUnit, ServiceUnitError


def _unit(tmp_path: Path, *, runner: FakeRunner | None = None, **overrides) -> GatewayServiceUnit:  # type: ignore[no-untyped-def]
    config = settings(tmp_path)
    if overrides:
        config = config.model_copy(
            update={
                "gateway": config.gateway.model_copy(
                    update={"service": GatewayServiceSettings(**overrides)}
                )
            }
        )
    return GatewayServiceUnit(
        config,
        project_root=tmp_path,
        unit_dir=tmp_path / "units",
        runner=runner or FakeRunner(),
        executable="/usr/bin/ricky",
    )


def test_rendering_is_deterministic_for_one_configuration(tmp_path: Path) -> None:
    unit = _unit(tmp_path)

    assert unit.render() == unit.render()


def test_the_rendered_unit_carries_every_required_element(tmp_path: Path) -> None:
    unit = _unit(tmp_path)

    text = unit.render()

    assert text.startswith(MARKER)
    assert "ExecStart=/usr/bin/ricky gateway run" in text
    assert "Restart=always" in text
    assert "RestartPreventExitStatus=78" in text
    assert "RestartSec=5s" in text
    assert "TimeoutStartSec=60s" in text
    assert "TimeoutStopSec=60s" in text
    assert "UMask=0077" in text
    assert "[Install]" in text


def test_no_secret_reaches_the_generated_unit(tmp_path: Path) -> None:
    unit = _unit(tmp_path)

    text = unit.render()

    assert "test-token" not in text
    assert "bot_token" not in text
    assert "Environment=" not in text


def test_a_project_root_with_shell_metacharacters_is_quoted(tmp_path: Path) -> None:
    hostile = tmp_path / "project; rm -rf ~"
    hostile.mkdir()
    config = settings(tmp_path)
    unit = GatewayServiceUnit(
        config,
        project_root=hostile,
        unit_dir=tmp_path / "units",
        runner=FakeRunner(),
        executable="/usr/bin/ricky exec",
    )

    text = unit.render()

    # Both values are shell-quoted, so a metacharacter can never split a command.
    assert "ExecStart='/usr/bin/ricky exec' gateway run" in text
    assert f"WorkingDirectory='{hostile}'" in text
    assert "rm -rf ~'" in text and "; rm" not in text.replace(f"'{hostile}'", "")


def test_install_writes_a_private_verified_unit(tmp_path: Path) -> None:
    unit = _unit(tmp_path)

    result = unit.install()

    assert result.created is True and result.verified is True
    assert result.backup_path is None
    assert unit.unit_path.read_text(encoding="utf-8") == unit.render()
    assert stat.S_IMODE(unit.unit_path.stat().st_mode) == 0o600


def test_unit_uses_a_private_deterministic_append_log_file(tmp_path: Path) -> None:
    unit = _unit(tmp_path)

    assert unit.log_path == tmp_path / "user" / "logs" / "ricky-gateway.log"
    text = unit.render()
    assert f"StandardOutput=append:{unit.log_path}" in text
    assert f"StandardError=append:{unit.log_path}" in text

    unit.install()

    assert unit.log_dir.is_dir()
    assert stat.S_IMODE(unit.log_dir.stat().st_mode) == 0o700


def test_reinstalling_a_changed_unit_backs_up_the_prior_ricky_version(tmp_path: Path) -> None:
    unit = _unit(tmp_path)
    unit.install()
    unit.unit_path.write_text(MARKER + "\n# an older Ricky rendering\n", encoding="utf-8")

    result = unit.install()

    assert result.backup_path is not None
    assert Path(result.backup_path).read_text(encoding="utf-8").endswith("older Ricky rendering\n")
    assert unit.unit_path.read_text(encoding="utf-8") == unit.render()


def test_install_refuses_to_overwrite_an_unmarked_unit(tmp_path: Path) -> None:
    unit = _unit(tmp_path)
    unit.unit_dir.mkdir(parents=True)
    unit.unit_path.write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")

    with pytest.raises(ServiceUnitError, match="not written by Ricky"):
        unit.install()

    assert unit.unit_path.read_text(encoding="utf-8") == "[Service]\nExecStart=/bin/true\n"


def test_uninstall_refuses_to_remove_an_unmarked_unit(tmp_path: Path) -> None:
    unit = _unit(tmp_path)
    unit.unit_dir.mkdir(parents=True)
    unit.unit_path.write_text("[Service]\n", encoding="utf-8")

    with pytest.raises(ServiceUnitError, match="will not be removed"):
        unit.uninstall()

    assert unit.unit_path.exists()


def test_install_and_uninstall_leave_unrelated_user_units_untouched(tmp_path: Path) -> None:
    unit = _unit(tmp_path)
    unit.unit_dir.mkdir(parents=True)
    neighbour = unit.unit_dir / "someone-else.service"
    neighbour.write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")

    unit.install()
    assert unit.uninstall() is True

    assert not unit.unit_path.exists()
    assert neighbour.read_text(encoding="utf-8") == "[Service]\nExecStart=/bin/true\n"


def test_uninstall_reports_false_when_nothing_is_installed(tmp_path: Path) -> None:
    assert _unit(tmp_path).uninstall() is False


def test_drift_detects_a_hand_edited_unit(tmp_path: Path) -> None:
    unit = _unit(tmp_path)
    assert unit.drift() is None
    unit.install()
    assert unit.drift() is None

    unit.unit_path.write_text(unit.render() + "# edited\n", encoding="utf-8")

    assert unit.drift() == f"{unit.unit_path} does not match the current configuration"


def test_drift_detects_a_foreign_unit(tmp_path: Path) -> None:
    unit = _unit(tmp_path)
    unit.unit_dir.mkdir(parents=True)
    unit.unit_path.write_text("[Service]\n", encoding="utf-8")

    assert unit.drift() == f"{unit.unit_path} exists but is not managed by Ricky"


def test_every_control_command_goes_through_the_injected_runner(tmp_path: Path) -> None:
    runner = FakeRunner()
    unit = _unit(tmp_path, runner=runner)

    unit.daemon_reload()
    unit.start()
    unit.stop()
    unit.restart()
    unit.enable()
    unit.disable()
    unit.status()

    assert runner.calls == [
        ("systemctl", "--user", "daemon-reload"),
        ("systemctl", "--user", "start", "ricky-gateway.service"),
        ("systemctl", "--user", "stop", "ricky-gateway.service"),
        ("systemctl", "--user", "restart", "ricky-gateway.service"),
        ("systemctl", "--user", "enable", "ricky-gateway.service"),
        ("systemctl", "--user", "disable", "ricky-gateway.service"),
        ("systemctl", "--user", "is-active", "ricky-gateway.service"),
    ]


def test_a_configured_unit_name_must_be_a_simple_service_file(tmp_path: Path) -> None:
    del tmp_path
    with pytest.raises(ValueError, match="simple \\*.service file name"):
        GatewayServiceSettings(unit_name="../evil.service")


def test_a_restart_loop_is_bounded_by_configuration(tmp_path: Path) -> None:
    unit = _unit(tmp_path, restart_seconds=30.0, stop_timeout_seconds=15.0)

    text = unit.render()

    # A crash loop cannot spin: systemd waits the configured delay every restart.
    assert "RestartSec=30s" in text
    assert "TimeoutStopSec=15s" in text
