"""CLI coverage for initialization, guided setup, decommission, and purge."""

from __future__ import annotations

import json
import stat
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

import ricky.interfaces.cli.installation as installation_cli
from ricky.config import load_settings
from ricky.gateway.service_unit import MARKER, GatewayServiceUnit, ServiceUnitError
from ricky.installation import bootstrap_file, initialize_installation, read_bootstrap_pointer
from ricky.interfaces.cli.app import app
from ricky.schedules.cron import BEGIN_MARKER, END_MARKER, CronError

runner = CliRunner()

MANAGED_BLOCK = f"{BEGIN_MARKER}\n{END_MARKER}\n"


def _patch_crontab(monkeypatch: pytest.MonkeyPatch, *, current: str = "") -> None:
    """Replace the host crontab adapter the purge command composes."""

    class _FakeCrontab:
        def __init__(self, _settings: Any) -> None:
            pass

        async def read(self) -> str:
            return current

    monkeypatch.setattr(installation_cli, "UserCrontabBackend", _FakeCrontab)


@pytest.fixture(autouse=True)
def isolate_host_crontab(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every command in this suite away from the host crontab program."""

    _patch_crontab(monkeypatch)


def _configured_root(tmp_path: Path) -> Path:
    return tmp_path / "user-data"


def _initialize(tmp_path: Path) -> tuple[Path, str]:
    root = _configured_root(tmp_path)
    initialized = initialize_installation(root)
    return root, initialized.installation_id


class _InteractiveInput:
    interactive = True


class _ScriptedRenderer:
    def __init__(
        self,
        *,
        lines: list[str] | None = None,
        secret: SecretStr | None = None,
    ) -> None:
        self.input_session = _InteractiveInput()
        self.lines = list(lines or [])
        self.secret = secret
        self.statuses: list[str] = []
        self.errors: list[str] = []
        self.prompts: list[str] = []

    async def read_line(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.lines:
            raise AssertionError(f"unexpected prompt: {prompt}")
        return self.lines.pop(0)

    async def read_secret(self, prompt: str) -> SecretStr | None:
        self.prompts.append(prompt)
        return self.secret

    def render_status(self, message: str, *, style: str = "dim") -> None:
        del style
        self.statuses.append(message)

    def render_error(self, message: str) -> None:
        self.errors.append(message)


def test_installation_commands_are_discoverable_without_writing_state() -> None:
    root_help = runner.invoke(app, ["--help"])
    data_help = runner.invoke(app, ["data", "--help"])

    assert root_help.exit_code == 0
    assert "init" in root_help.stdout
    assert "setup" in root_help.stdout
    assert "decommission" in root_help.stdout
    assert data_help.exit_code == 0
    assert "purge" in data_help.stdout
    assert read_bootstrap_pointer() is None


def test_init_human_output_creates_custom_scaffold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RICKY_USER_DATA_DIR", raising=False)
    root = tmp_path / "custom-data"

    result = runner.invoke(app, ["init", "--user-data-dir", str(root)])

    assert result.exit_code == 0
    assert "Initialized Ricky installation" in result.stdout
    assert str(root) in result.stdout
    assert "installation id:" in result.stdout
    assert (root / "installation.json").is_file()
    assert (root / "profiles" / "shared").is_dir()


def test_init_json_is_machine_readable_and_idempotent(tmp_path: Path) -> None:
    root = _configured_root(tmp_path)

    first = runner.invoke(app, ["init", "--json"])
    second = runner.invoke(app, ["init", "--json"])

    assert first.exit_code == second.exit_code == 0
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert first_payload["created"] is True
    assert first_payload["pointer_created"] is True
    assert second_payload["created"] is False
    assert second_payload["pointer_created"] is False
    assert first_payload["installation_id"] == second_payload["installation_id"]
    assert first_payload["user_data_dir"] == str(root)


def test_init_json_error_refuses_unknown_nonempty_target_without_noise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RICKY_USER_DATA_DIR", raising=False)
    root = tmp_path / "occupied"
    root.mkdir()
    marker = root / "keep"
    marker.write_text("unchanged", encoding="utf-8")

    result = runner.invoke(
        app,
        ["init", "--user-data-dir", str(root), "--json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert "non-empty directory" in payload["error"]
    assert marker.read_text(encoding="utf-8") == "unchanged"


def test_setup_refuses_noninteractive_input_and_preserves_scaffold(tmp_path: Path) -> None:
    root, _installation_id = _initialize(tmp_path)

    result = runner.invoke(app, ["setup"])

    assert result.exit_code == 1
    assert "requires an interactive terminal" in result.stdout
    assert not (root / "profiles" / "shared" / ".secrets.toml").exists()
    assert not (root / "profiles" / "shared" / "ricky.toml").exists()


def test_interactive_setup_saves_private_shared_provider_config_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _installation_id = _initialize(tmp_path)
    secret_value = "setup-test-secret-that-must-not-be-rendered"
    renderer = _ScriptedRenderer(
        lines=["openrouter", "test/provider-model"],
        secret=SecretStr(secret_value),
    )
    monkeypatch.setattr(installation_cli, "CliRenderer", lambda: renderer)

    result = runner.invoke(app, ["setup"])

    assert result.exit_code == 0
    assert secret_value not in result.stdout
    assert secret_value not in "\n".join(renderer.statuses)
    assert renderer.errors == []
    assert renderer.lines == []
    assert any("local validation only" in status for status in renderer.statuses)
    assert all("verify" not in prompt.lower() for prompt in renderer.prompts)

    secrets_path = root / "profiles" / "shared" / ".secrets.toml"
    profile_path = root / "profiles" / "shared" / "ricky.toml"
    secrets = tomllib.loads(secrets_path.read_text(encoding="utf-8"))
    profile = tomllib.loads(profile_path.read_text(encoding="utf-8"))
    assert secrets == {"openrouter_api_key": secret_value}
    assert profile["profile"]["default_provider"] == "openrouter"
    assert profile["profile"]["default_models"]["openrouter"] == "test/provider-model"
    assert stat.S_IMODE(secrets_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(profile_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(profile_path.parent.stat().st_mode) == 0o700


def test_purge_unattended_requires_both_yes_and_exact_installation_id(tmp_path: Path) -> None:
    root, installation_id = _initialize(tmp_path)

    no_flags = runner.invoke(app, ["data", "purge"])
    no_id = runner.invoke(app, ["data", "purge", "--yes"])
    no_yes = runner.invoke(
        app,
        ["data", "purge", "--installation-id", installation_id],
    )
    wrong_id = runner.invoke(
        app,
        ["data", "purge", "--yes", "--installation-id", "0" * 32],
    )

    assert no_flags.exit_code == no_id.exit_code == no_yes.exit_code == wrong_id.exit_code == 1
    assert "requires --yes" in no_flags.stdout
    assert "exact --installation-id" in " ".join(no_id.stdout.split())
    assert "requires --yes" in no_yes.stdout
    assert "does not match" in wrong_id.stdout
    assert installation_id not in no_flags.stdout
    assert root.is_dir()
    assert read_bootstrap_pointer() is not None


def test_purge_json_refuses_an_interactive_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _installation_id = _initialize(tmp_path)
    renderer = _ScriptedRenderer()
    monkeypatch.setattr(installation_cli, "CliRenderer", lambda: renderer)

    result = runner.invoke(app, ["data", "purge", "--json"], input="n\n")

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "error": "--json purge requires --yes and the exact --installation-id"
    }
    assert root.is_dir()
    assert read_bootstrap_pointer() is not None


def test_setup_reports_a_closed_prompt_as_a_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _installation_id = _initialize(tmp_path)

    class _EofRenderer(_ScriptedRenderer):
        async def read_line(self, prompt: str) -> str:
            self.prompts.append(prompt)
            raise EOFError

    renderer = _EofRenderer()
    monkeypatch.setattr(installation_cli, "CliRenderer", lambda: renderer)

    result = runner.invoke(app, ["setup"])

    assert result.exit_code == 1
    assert any("cancelled" in message for message in renderer.errors)
    assert not (root / "profiles" / "shared" / ".secrets.toml").exists()
    assert not (root / "profiles" / "shared" / "ricky.toml").exists()


def test_purge_json_removes_exact_data_and_pointer(tmp_path: Path) -> None:
    root, installation_id = _initialize(tmp_path)

    result = runner.invoke(
        app,
        [
            "data",
            "purge",
            "--yes",
            "--installation-id",
            installation_id,
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "bootstrap_path": str(bootstrap_file()),
        "installation_id": installation_id,
        "user_data_dir": str(root),
    }
    assert not root.exists()
    assert read_bootstrap_pointer() is None


def test_purge_refuses_owned_launch_surfaces_without_removing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, installation_id = _initialize(tmp_path)
    unit_path = GatewayServiceUnit(load_settings()).unit_path
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(f"{MARKER}\n[Service]\n", encoding="utf-8")
    command = ["data", "purge", "--yes", "--installation-id", installation_id, "--json"]

    service_installed = runner.invoke(app, command)

    assert service_installed.exit_code == 1
    assert "decommission" in json.loads(service_installed.stdout)["error"]

    unit_path.unlink()
    _patch_crontab(monkeypatch, current=MANAGED_BLOCK)

    schedules_installed = runner.invoke(app, command)

    assert schedules_installed.exit_code == 1
    assert "decommission" in json.loads(schedules_installed.stdout)["error"]
    assert root.is_dir()
    assert read_bootstrap_pointer() is not None


def test_interactive_purge_cancellation_preserves_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _installation_id = _initialize(tmp_path)
    renderer = _ScriptedRenderer()
    monkeypatch.setattr(installation_cli, "CliRenderer", lambda: renderer)

    result = runner.invoke(app, ["data", "purge"], input="n\n")

    assert result.exit_code == 0
    assert any("cancelled" in status.lower() for status in renderer.statuses)
    assert root.is_dir()
    assert read_bootstrap_pointer() is not None


@pytest.mark.asyncio
async def test_decommission_service_and_schedule_absence_is_idempotent_and_preserves_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, installation_id = _initialize(tmp_path)
    calls: list[str] = []

    class FakeUnit:
        unit_path = tmp_path / "ricky-gateway.service"

        def __init__(self, _settings: Any) -> None:
            pass

        def installed(self) -> None:
            return None

        def stop(self) -> SimpleNamespace:
            calls.append("stop")
            return SimpleNamespace(returncode=0, stderr="")

        def disable(self) -> SimpleNamespace:
            calls.append("disable")
            return SimpleNamespace(returncode=0, stderr="")

        def uninstall(self) -> bool:
            calls.append("uninstall-service")
            return True

        def daemon_reload(self) -> SimpleNamespace:
            calls.append("daemon-reload")
            return SimpleNamespace(returncode=0, stderr="")

    uninstalled: list[str] = []

    class FakeCron:
        def __init__(self, _settings: Any) -> None:
            pass

        async def read(self) -> str:
            return "*/5 * * * * /usr/bin/unrelated\n"

        async def uninstall(self) -> SimpleNamespace:
            uninstalled.append("cron")
            return SimpleNamespace(changed=False, backup_path=None)

    monkeypatch.setattr(installation_cli, "GatewayServiceUnit", FakeUnit)
    monkeypatch.setattr(installation_cli, "UserCrontabBackend", FakeCron)

    result = await installation_cli._decommission()

    assert result["installation_id"] == installation_id
    assert result["user_data_dir"] == str(root)
    assert result["service"]["state"] == "not_installed"
    assert result["schedules"]["state"] == "not_installed"
    # No unit file to remove, but systemd can still hold the unit loaded.
    assert calls == ["stop", "disable", "daemon-reload"]
    assert uninstalled == []
    assert result["user_data_preserved"] is True
    assert result["bootstrap_pointer_preserved"] is True
    assert result["software_removal_command"] == "uv tool uninstall ricky"
    assert root.is_dir()
    assert read_bootstrap_pointer() is not None


@pytest.mark.asyncio
async def test_decommission_removes_only_owned_surfaces_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _installation_id = _initialize(tmp_path)
    calls: list[str] = []
    backup = root / "cron" / "backups" / "before"

    class FakeUnit:
        unit_path = tmp_path / "ricky-gateway.service"

        def __init__(self, _settings: Any) -> None:
            pass

        def installed(self) -> str:
            return f"{MARKER}\n[Service]\n"

        def stop(self) -> SimpleNamespace:
            calls.append("stop")
            return SimpleNamespace(returncode=0, stderr="")

        def disable(self) -> SimpleNamespace:
            calls.append("disable")
            return SimpleNamespace(returncode=0, stderr="")

        def uninstall(self) -> bool:
            calls.append("uninstall-service")
            return True

        def daemon_reload(self) -> SimpleNamespace:
            calls.append("daemon-reload")
            return SimpleNamespace(returncode=0, stderr="")

    class FakeCron:
        def __init__(self, _settings: Any) -> None:
            pass

        async def read(self) -> str:
            return MANAGED_BLOCK

        async def uninstall(self) -> SimpleNamespace:
            calls.append("uninstall-cron")
            return SimpleNamespace(changed=True, backup_path=backup)

    monkeypatch.setattr(installation_cli, "GatewayServiceUnit", FakeUnit)
    monkeypatch.setattr(installation_cli, "UserCrontabBackend", FakeCron)

    result = await installation_cli._decommission()

    # The installed block is removed although Ricky's cron state directory is
    # absent; the crontab read, not local state, decides what is installed.
    assert not (root / "cron").exists()
    assert calls == ["stop", "disable", "uninstall-service", "daemon-reload", "uninstall-cron"]
    assert result["service"]["state"] == "removed"
    assert result["schedules"] == {"state": "removed", "backup_path": str(backup)}
    assert root.is_dir()


@pytest.mark.asyncio
async def test_decommission_treats_an_unusable_crontab_as_no_managed_schedules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _installation_id = _initialize(tmp_path)
    (root / "cron").mkdir()
    calls: list[str] = []

    class FakeUnit:
        unit_path = tmp_path / "ricky-gateway.service"

        def __init__(self, _settings: Any) -> None:
            pass

        def installed(self) -> None:
            return None

        def stop(self) -> SimpleNamespace:
            return SimpleNamespace(returncode=0, stderr="")

        def disable(self) -> SimpleNamespace:
            return SimpleNamespace(returncode=0, stderr="")

        def daemon_reload(self) -> SimpleNamespace:
            return SimpleNamespace(returncode=0, stderr="")

    class UnusableCron:
        def __init__(self, _settings: Any) -> None:
            pass

        async def read(self) -> str:
            raise CronError("cannot execute crontab reader: [Errno 2] crontab")

        async def uninstall(self) -> SimpleNamespace:
            calls.append("uninstall-cron")
            return SimpleNamespace(changed=False, backup_path=None)

    monkeypatch.setattr(installation_cli, "GatewayServiceUnit", FakeUnit)
    monkeypatch.setattr(installation_cli, "UserCrontabBackend", UnusableCron)

    result = await installation_cli._decommission()

    assert result["schedules"] == {"state": "not_installed", "backup_path": None}
    assert calls == []
    assert root.is_dir()


@pytest.mark.asyncio
async def test_decommission_removes_the_unit_file_when_systemctl_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _installation_id = _initialize(tmp_path)
    calls: list[str] = []

    class DegradedUnit:
        unit_path = tmp_path / "ricky-gateway.service"

        def __init__(self, _settings: Any) -> None:
            pass

        def installed(self) -> str:
            return f"{MARKER}\n[Service]\n"

        def stop(self) -> SimpleNamespace:
            calls.append("stop")
            return SimpleNamespace(returncode=1, stderr="Failed to connect to bus")

        def disable(self) -> SimpleNamespace:
            calls.append("disable")
            return SimpleNamespace(returncode=1, stderr="Failed to connect to bus")

        def uninstall(self) -> bool:
            calls.append("uninstall-service")
            return True

        def daemon_reload(self) -> SimpleNamespace:
            calls.append("daemon-reload")
            return SimpleNamespace(returncode=1, stderr="Failed to connect to bus")

    monkeypatch.setattr(installation_cli, "GatewayServiceUnit", DegradedUnit)
    _patch_crontab(monkeypatch)

    result = await installation_cli._decommission()

    # A host with no user session bus must still be able to retire the unit.
    assert calls == ["stop", "disable", "uninstall-service", "daemon-reload"]
    assert result["service"]["state"] == "removed"
    assert result["service"]["stop_exit"] == 1
    assert result["service"]["disable_exit"] == 1
    assert result["service"]["daemon_reload_exit"] == 1
    rendered = installation_cli._render_decommission(result)
    assert "systemctl reported: stop exit 1, disable exit 1, daemon-reload exit 1" in rendered
    assert root.is_dir()
    assert read_bootstrap_pointer() is not None


@pytest.mark.asyncio
async def test_decommission_refuses_foreign_service_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _installation_id = _initialize(tmp_path)
    cron_called = False

    class ForeignUnit:
        unit_path = tmp_path / "ricky-gateway.service"

        def __init__(self, _settings: Any) -> None:
            pass

        def installed(self) -> str:
            return "[Service]\nExecStart=/bin/foreign\n"

        def owned(self) -> bool:
            return False

    class FakeCron:
        def __init__(self, _settings: Any) -> None:
            pass

        async def uninstall(self) -> SimpleNamespace:
            nonlocal cron_called
            cron_called = True
            return SimpleNamespace(changed=False, backup_path=None)

    monkeypatch.setattr(installation_cli, "GatewayServiceUnit", ForeignUnit)
    monkeypatch.setattr(installation_cli, "UserCrontabBackend", FakeCron)

    with pytest.raises(ServiceUnitError, match="not written by Ricky"):
        await installation_cli._decommission()

    assert cron_called is False
    assert root.is_dir()
    assert read_bootstrap_pointer() is not None


def test_decommission_cli_json_never_calls_host_commands_when_mocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, installation_id = _initialize(tmp_path)

    async def fake_decommission() -> dict[str, Any]:
        return {
            "installation_id": installation_id,
            "user_data_dir": str(root),
            "service": {
                "state": "not_installed",
                "unit_path": "/isolated/unit",
                "stop_exit": 0,
                "disable_exit": 0,
                "daemon_reload_exit": 0,
            },
            "schedules": {"state": "not_installed", "backup_path": None},
            "user_data_preserved": True,
            "bootstrap_pointer_preserved": True,
            "software_removal_command": "uv tool uninstall ricky",
        }

    monkeypatch.setattr(installation_cli, "_decommission", fake_decommission)

    result = runner.invoke(app, ["decommission", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["software_removal_command"] == "uv tool uninstall ricky"
    assert root.is_dir()
    assert read_bootstrap_pointer() is not None
