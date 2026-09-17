"""Gateway service CLI teardown against a fake service manager and unit file."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from gateway_ops_support import FakeRunner, settings
from ricky.gateway.errors import GatewayConfigurationError
from ricky.gateway.service_unit import MARKER, GatewayServiceUnit, ServiceUnitError
from ricky.gateway.vault_bootstrap import GatewayVaultBootstrapError
from ricky.interfaces.cli import gateway as gateway_cli


@pytest.mark.parametrize(
    ("failure", "expected_exit"),
    [
        (GatewayConfigurationError("capability builtin.browser.read is unavailable"), 78),
        (GatewayVaultBootstrapError("requested gateway vault unlock failed"), 78),
        (ValueError("runtime failure"), 1),
        (OSError("temporary I/O failure"), 1),
    ],
)
def test_gateway_exit_classification_preserves_runtime_restarts(
    tmp_path: Path, failure: Exception, expected_exit: int, capsys
) -> None:
    async def fail(_renderer) -> None:
        raise failure

    with pytest.raises(typer.Exit) as caught:
        gateway_cli._run_gateway_command(fail)

    assert caught.value.exit_code == expected_exit
    assert str(failure) in capsys.readouterr().out
    unit = GatewayServiceUnit(
        settings(tmp_path), project_root=tmp_path, executable="/usr/bin/ricky"
    )
    assert "RestartPreventExitStatus=78" in unit.render()
    assert "Restart=always" in unit.render()


def test_invalid_gateway_settings_exit_without_retry_or_configuration_input(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    def invalid_settings():
        raise ValueError("sensitive-invalid-configuration-input")

    monkeypatch.setattr(gateway_cli, "load_settings", invalid_settings)

    with pytest.raises(typer.Exit) as caught:
        gateway_cli._run_gateway_command(lambda renderer: gateway_cli._run_service((), renderer))

    assert caught.value.exit_code == 78
    output = capsys.readouterr().out
    assert "gateway configuration could not be loaded" in output
    assert "sensitive-invalid-configuration-input" not in output


class RecordingRenderer:
    """Capture rendered command output instead of writing to a terminal."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def render_status(self, message: str, *, style: str) -> None:
        del style
        self.messages.append(message)

    def render_error(self, message: str) -> None:  # pragma: no cover - defensive
        raise AssertionError(f"uninstall must not render an error: {message}")

    def output(self) -> str:
        return "\n".join(self.messages)


def _install_unit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    runner: FakeRunner | None = None,
) -> GatewayServiceUnit:
    """Bind the CLI to one isolated unit driven by a fake systemctl."""

    config = settings(tmp_path)
    unit = GatewayServiceUnit(
        config,
        project_root=tmp_path,
        unit_dir=tmp_path / "units",
        runner=runner or FakeRunner(),
        executable="/usr/bin/ricky",
    )
    monkeypatch.setattr(gateway_cli, "load_settings", lambda: config)
    monkeypatch.setattr(gateway_cli, "GatewayServiceUnit", lambda selected: unit)
    return unit


def _verbs(runner: FakeRunner) -> list[str]:
    return [call[2] for call in runner.calls]


async def test_uninstall_clears_systemd_state_when_no_unit_file_is_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # systemd can still hold the unit loaded after the file is removed by hand.
    runner = FakeRunner()
    unit = _install_unit(tmp_path, monkeypatch, runner=runner)
    renderer = RecordingRenderer()

    await gateway_cli._service_uninstall(renderer)  # type: ignore[arg-type]

    assert _verbs(runner) == ["stop", "disable", "daemon-reload"]
    output = renderer.output()
    assert "No installed Ricky gateway unit file was present." in output
    assert "removed: False" in output
    assert not unit.unit_path.exists()


async def test_uninstall_removes_an_owned_unit_and_reloads_the_user_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeRunner()
    unit = _install_unit(tmp_path, monkeypatch, runner=runner)
    unit.install()
    renderer = RecordingRenderer()

    await gateway_cli._service_uninstall(renderer)  # type: ignore[arg-type]

    assert _verbs(runner) == ["stop", "disable", "daemon-reload"]
    output = renderer.output()
    assert "removed: True" in output
    assert "No installed Ricky gateway unit file was present." not in output
    assert not unit.unit_path.exists()


async def test_uninstall_refuses_a_foreign_unit_and_takes_no_service_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeRunner()
    unit = _install_unit(tmp_path, monkeypatch, runner=runner)
    unit.unit_dir.mkdir(parents=True, exist_ok=True)
    foreign = "[Unit]\nDescription=Someone else's unit\n"
    unit.unit_path.write_text(foreign, encoding="utf-8")
    renderer = RecordingRenderer()

    with pytest.raises(ServiceUnitError, match="was not written by Ricky"):
        await gateway_cli._service_uninstall(renderer)  # type: ignore[arg-type]

    assert runner.calls == []
    assert renderer.messages == []
    assert unit.unit_path.read_text(encoding="utf-8") == foreign


async def test_uninstall_reports_a_failing_systemctl_exit_instead_of_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A host with no user D-Bus must still be able to remove the unit file.
    runner = FakeRunner(returncode=1, stdout="")
    unit = _install_unit(tmp_path, monkeypatch, runner=runner)
    unit.install()
    renderer = RecordingRenderer()

    await gateway_cli._service_uninstall(renderer)  # type: ignore[arg-type]

    output = renderer.output()
    assert "stop: exit 1" in output
    assert "disable: exit 1" in output
    assert "daemon-reload: exit 1" in output
    assert "removed: True" in output
    assert not unit.unit_path.exists()


async def test_uninstall_reads_the_unit_file_once_before_removing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The command must not re-read the file to decide ownership, which would
    # leave a window for a foreign unit to be swapped in before removal.
    runner = FakeRunner()
    unit = _install_unit(tmp_path, monkeypatch, runner=runner)
    unit.install()
    reads = 0
    real_installed = unit.installed

    def counting_installed() -> str | None:
        nonlocal reads
        reads += 1
        return real_installed()

    monkeypatch.setattr(unit, "installed", counting_installed)
    monkeypatch.setattr(
        unit,
        "owned",
        lambda: pytest.fail("uninstall must decide ownership from the bytes it already read"),
    )
    renderer = RecordingRenderer()

    await gateway_cli._service_uninstall(renderer)  # type: ignore[arg-type]

    # One read in the command plus the one inside uninstall(), which keeps its
    # own ownership check on the bytes it is about to unlink.
    assert reads == 2
    assert unit.render().startswith(MARKER)
