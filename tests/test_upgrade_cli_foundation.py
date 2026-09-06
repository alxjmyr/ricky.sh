"""CLI and runtime-gate coverage for the Phase 1 upgrade foundation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

import ricky.gateway.service_unit as service_unit_module
import ricky.interfaces.cli.app as app_module
import ricky.interfaces.cli.installation as installation_cli
from ricky import __version__
from ricky.installation import (
    INSTALLATION_FILENAME,
    InstallationError,
    InstallationManifest,
    bootstrap_config_dir,
    bootstrap_file,
    initialize_installation,
    read_bootstrap_pointer,
)
from ricky.interfaces.cli.app import app
from ricky.upgrades.environment import InstalledToolEnvironment
from ricky.upgrades.integrations import ManagedIntegrationError
from ricky.upgrades.journal import (
    UpgradeJournal,
    UpgradeManagedBinding,
    create_upgrade_journal,
)
from ricky.upgrades.models import MigrationPlan, MigrationStep
from ricky.upgrades.versions import ReleaseVersion

runner = CliRunner()

ABORT_CAUSE = "the exclusive installation lock was unavailable for this upgrade"
RESTORE_FAILURE = "stopped gateway could not be restored after upgrade abort"


def _initialize(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "user-data"
    result = initialize_installation(root)
    return root, result.installation_id


def _manifest(root: Path) -> InstallationManifest:
    return InstallationManifest.model_validate_json(
        (root / INSTALLATION_FILENAME).read_text(encoding="utf-8")
    )


def _set_manifest_state(
    root: Path,
    *,
    state: str = "clean",
    generation: int = 1,
    operation_id: str | None = None,
) -> InstallationManifest:
    current = _manifest(root)
    candidate = current.model_copy(
        update={
            "migration_state": state,
            "data_generation": generation,
            "operation_id": operation_id,
        }
    )
    validated = InstallationManifest.model_validate(candidate.model_dump(mode="python"))
    (root / INSTALLATION_FILENAME).write_text(validated.model_dump_json(), encoding="utf-8")
    return validated


def _file_snapshot(root: Path) -> dict[str, tuple[bytes, int, int]]:
    return {
        str(path.relative_to(root)): (
            path.read_bytes(),
            path.stat().st_mode,
            path.stat().st_mtime_ns,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _forbid_upgrade_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an offline upgrade check attempted external I/O")

    async def forbidden_subprocess(*_args: object, **_kwargs: object) -> None:
        forbidden()

    monkeypatch.setattr("httpx.Client.request", forbidden)
    monkeypatch.setattr("httpx.AsyncClient.request", forbidden)
    monkeypatch.setattr("asyncio.create_subprocess_exec", forbidden_subprocess)

    async def current_uv() -> ReleaseVersion:
        return ReleaseVersion.parse("0.11.28")

    monkeypatch.setattr("ricky.upgrades.service._discover_uv_version", current_uv)


def _local_release_descriptor(tmp_path: Path, version: str) -> Path:
    root = tmp_path / f"release-{version}"
    root.mkdir()
    wheel = root / f"ricky-{version}-py3-none-any.whl"
    constraints = root / f"ricky-{version}-constraints.txt"
    wheel.write_bytes(b"wheel")
    constraints.write_bytes(b"constraints")

    def artifact(path: Path) -> dict[str, object]:
        payload = path.read_bytes()
        return {
            "name": path.name,
            "url": path.as_uri(),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }

    descriptor = root / f"ricky-{version}-release.json"
    descriptor.write_text(
        json.dumps(
            {
                "format_version": 1,
                "repository": "alxjmyr/ricky.sh",
                "channel": "stable",
                "source": "local_drill",
                "software_version": version,
                "supported_source_data_generations": [1],
                "target_data_generation": 1,
                "python_requirement": ">=3.12",
                "minimum_uv_version": "0.6.0",
                "wheel": artifact(wheel),
                "constraints": artifact(constraints),
            }
        ),
        encoding="utf-8",
    )
    return descriptor


def _patch_no_host_launchers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FakeUnit:
        unit_path = tmp_path / "isolated-ricky-gateway.service"

        def __init__(self, _settings: Any) -> None:
            pass

        def installed(self) -> None:
            return None

        def owned(self) -> bool:
            return False

        def stop(self) -> SimpleNamespace:
            return SimpleNamespace(returncode=0, stderr="")

        def disable(self) -> SimpleNamespace:
            return SimpleNamespace(returncode=0, stderr="")

        def uninstall(self) -> bool:
            raise AssertionError("an absent service cannot be uninstalled")

        def daemon_reload(self) -> SimpleNamespace:
            return SimpleNamespace(returncode=0, stderr="")

    class FakeCrontab:
        def __init__(self, _settings: Any) -> None:
            pass

        async def read(self) -> str:
            return ""

        async def uninstall(self) -> SimpleNamespace:
            raise AssertionError("an absent managed block cannot be uninstalled")

    monkeypatch.setattr(installation_cli, "GatewayServiceUnit", FakeUnit)
    monkeypatch.setattr(installation_cli, "UserCrontabBackend", FakeCrontab)
    # The guarded purge API imports this owner lazily rather than through the
    # CLI composition module, so bind the same in-memory fake at that boundary.
    monkeypatch.setattr(service_unit_module, "GatewayServiceUnit", FakeUnit)


def test_upgrade_check_with_local_release_source_is_offline_and_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _installation_id = _initialize(tmp_path)
    authored = root / "profiles" / "shared" / "SOUL.md"
    authored.write_text("preserve exactly\n", encoding="utf-8")
    _forbid_upgrade_io(monkeypatch)
    descriptor = _local_release_descriptor(tmp_path, "0.6.0")
    data_before = _file_snapshot(root)
    bootstrap_before = _file_snapshot(bootstrap_config_dir())
    bootstrap_entries = {path.name for path in bootstrap_config_dir().iterdir()}

    source = ["--release-descriptor", str(descriptor)]
    human = runner.invoke(app, ["upgrade", "--check", *source])
    machine = runner.invoke(app, ["upgrade", "--check", "--json", *source])

    assert human.exit_code == machine.exit_code == 0
    assert "Current Ricky software: 0.6.0" in human.stdout
    assert "Current data generation: 1" in human.stdout
    assert "Upgrade status: no_update" in human.stdout
    payload = json.loads(machine.stdout)
    assert payload["status"] == "no_update"
    assert payload["current_software_version"] == "0.6.0"
    assert payload["current_data_generation"] == 1
    assert payload["selected_release"]["software_version"] == "0.6.0"
    assert payload["compatibility"]["compatible"] is True
    assert _file_snapshot(root) == data_before
    assert _file_snapshot(bootstrap_config_dir()) == bootstrap_before
    assert {path.name for path in bootstrap_config_dir().iterdir()} == bootstrap_entries
    assert ".operation.json" not in bootstrap_entries


def test_upgrade_check_accepts_only_exact_release_version_grammar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _installation_id = _initialize(tmp_path)
    _forbid_upgrade_io(monkeypatch)
    descriptor = _local_release_descriptor(tmp_path, "0.6.1")
    before = _file_snapshot(root)

    source = ["--release-descriptor", str(descriptor)]
    accepted = runner.invoke(app, ["upgrade", "--check", "--to", "0.6.1", "--json", *source])
    refused = runner.invoke(app, ["upgrade", "--check", "--to", "0.6", "--json", *source])

    assert accepted.exit_code == 0
    assert json.loads(accepted.stdout)["status"] == "update_available"
    assert refused.exit_code == 1
    error = json.loads(refused.stdout)
    assert "MAJOR.MINOR.PATCH" in error["error"]
    assert _file_snapshot(root) == before


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--check", "--resume"], "choose only one"),
        (["--check", "--rollback"], "choose only one"),
        (["--resume", "--rollback"], "choose only one"),
        (["--resume", "--to", "0.6.1"], "--to is valid only"),
        (["--rollback", "--to", "0.6.1"], "--to is valid only"),
        (["--check", "--update-jobs"], "--update-jobs is valid only"),
        (["--resume", "--update-jobs"], "--update-jobs is valid only"),
        (["--rollback", "--update-jobs"], "--update-jobs is valid only"),
        (["--check", "--yes"], "--yes is not valid with --check"),
        (["--yes"], "--yes requires an exact --to"),
        (["--to", "0.6.1"], "unattended upgrade requires"),
        (["--json", "--to", "0.6.1"], "--json upgrade requires"),
        (["--rollback", "--json"], "--json rollback requires --yes"),
    ],
)
def test_upgrade_cli_rejects_incoherent_flag_combinations(
    tmp_path: Path, arguments: list[str], message: str
) -> None:
    _initialize(tmp_path)

    result = runner.invoke(app, ["upgrade", *arguments])

    assert result.exit_code == 1
    assert message in result.stdout


def test_upgrade_apply_from_development_environment_refuses_before_mutation(
    tmp_path: Path,
) -> None:
    root, _installation_id = _initialize(tmp_path)
    before_data = _file_snapshot(root)
    before_pointer = bootstrap_file().read_bytes()

    result = runner.invoke(app, ["upgrade", "--yes", "--to", "0.6.1"])

    assert result.exit_code == 1
    assert "uv-tool-installed Ricky release" in result.stdout
    assert "read-only upgrade check remains available" in result.stdout
    assert _file_snapshot(root) == before_data
    assert bootstrap_file().read_bytes() == before_pointer


@pytest.mark.parametrize(
    ("state", "generation", "operation_id", "expected"),
    [
        ("failed", 1, "a" * 32, "upgrade"),
        ("clean", 2, None, "data generation 2"),
    ],
)
def test_ordinary_command_is_gated_before_configuration_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    generation: int,
    operation_id: str | None,
    expected: str,
) -> None:
    root, _installation_id = _initialize(tmp_path)
    manifest = _set_manifest_state(
        root,
        state=state,
        generation=generation,
        operation_id=operation_id,
    )

    def forbidden_settings() -> None:
        raise AssertionError("configuration loaded before the compatibility gate")

    monkeypatch.setattr(app_module, "load_settings", forbidden_settings)

    result = runner.invoke(app, ["config"])

    assert result.exit_code == 1
    assert expected in result.stdout
    assert _manifest(root) == manifest


@pytest.mark.parametrize(
    ("state", "generation", "operation_id"),
    [("failed", 1, "a" * 32), ("clean", 2, None)],
)
def test_help_and_version_remain_available_while_data_is_gated(
    tmp_path: Path, state: str, generation: int, operation_id: str | None
) -> None:
    root, _installation_id = _initialize(tmp_path)
    _set_manifest_state(
        root,
        state=state,
        generation=generation,
        operation_id=operation_id,
    )

    help_result = runner.invoke(app, ["--help"])
    version_result = runner.invoke(app, ["--version"])

    assert help_result.exit_code == version_result.exit_code == 0
    assert "upgrade" in help_result.stdout
    assert version_result.stdout.strip() == f"ricky {__version__}"


def test_decommission_remains_available_in_failed_state_without_host_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, installation_id = _initialize(tmp_path)
    failed = _set_manifest_state(
        root,
        state="failed",
        operation_id="a" * 32,
    )
    _patch_no_host_launchers(monkeypatch, tmp_path)

    result = runner.invoke(app, ["decommission", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["installation_id"] == installation_id
    assert payload["service"]["state"] == "not_installed"
    assert payload["schedules"]["state"] == "not_installed"
    assert payload["user_data_preserved"] is True
    assert root.is_dir()
    assert _manifest(root) == failed
    assert read_bootstrap_pointer() is not None


def test_guarded_purge_remains_available_in_failed_state_with_injected_host_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, installation_id = _initialize(tmp_path)
    _set_manifest_state(
        root,
        state="failed",
        operation_id="a" * 32,
    )
    _patch_no_host_launchers(monkeypatch, tmp_path)

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
    assert payload["installation_id"] == installation_id
    assert payload["user_data_dir"] == str(root)
    assert not root.exists()
    assert read_bootstrap_pointer() is None


def _tool_environment(tmp_path: Path) -> InstalledToolEnvironment:
    tool_root = (tmp_path / "uv" / "tools").resolve()
    return InstalledToolEnvironment(
        tool_root=str(tool_root),
        environment=str((tool_root / "ricky").resolve()),
        bin=str((tmp_path / "uv" / "bin").resolve()),
        executable=str((tmp_path / "uv" / "bin" / "ricky").resolve()),
        package_root=str((tool_root / "ricky" / "lib" / "ricky").resolve()),
        current_version=ReleaseVersion.parse("0.6.0"),
    )


@pytest.mark.parametrize("restore_fails", [True, False])
def test_aborted_upgrade_reports_the_original_cause_with_any_restore_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore_fails: bool
) -> None:
    root, _installation_id = _initialize(tmp_path)
    _forbid_upgrade_io(monkeypatch)
    descriptors = [
        _local_release_descriptor(tmp_path, "0.6.0"),
        _local_release_descriptor(tmp_path, "0.6.1"),
    ]
    environment = _tool_environment(tmp_path)
    before = _file_snapshot(root)
    restores: list[str] = []

    async def fake_software_binding(**_kwargs: Any) -> object:
        return SimpleNamespace()

    async def fake_prepare(**_kwargs: Any) -> UpgradeManagedBinding:
        return UpgradeManagedBinding(
            gateway_unit_path=str((tmp_path / "ricky-gateway.service").resolve()),
            gateway_unit_sha256="d" * 64,
            gateway_was_enabled=True,
            gateway_was_active=True,
        )

    def fake_restore(**_kwargs: Any) -> None:
        restores.append("restore")
        if restore_fails:
            raise ManagedIntegrationError(RESTORE_FAILURE)

    real_lock = installation_cli.installation_operation_lock

    def gated_lock(**kwargs: Any) -> Any:
        # The abort must happen after the managed gateway was stopped but
        # before the operation journal exists.
        if kwargs.get("mode") == "exclusive":
            raise InstallationError(ABORT_CAUSE)
        return real_lock(**kwargs)

    monkeypatch.setattr(
        installation_cli,
        "discover_installed_tool_environment",
        lambda: environment,
    )
    monkeypatch.setattr(installation_cli, "create_software_binding", fake_software_binding)
    monkeypatch.setattr(installation_cli, "prepare_managed_upgrade", fake_prepare)
    monkeypatch.setattr(installation_cli, "restore_gateway_after_aborted_prepare", fake_restore)
    monkeypatch.setattr(installation_cli, "installation_operation_lock", gated_lock)

    source: list[str] = []
    for descriptor in descriptors:
        source.extend(("--release-descriptor", str(descriptor)))
    result = runner.invoke(app, ["upgrade", "--yes", "--to", "0.6.1", "--json", *source])

    assert result.exit_code == 1
    assert restores == ["restore"]
    error = json.loads(result.stdout)["error"]
    assert error.startswith(ABORT_CAUSE)
    if restore_fails:
        assert "could not be restarted" in error
        assert RESTORE_FAILURE in error
    else:
        assert error == ABORT_CAUSE
    assert _file_snapshot(root) == before


def test_aborted_upgrade_restore_failure_is_visible_in_human_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, _installation_id = _initialize(tmp_path)
    _forbid_upgrade_io(monkeypatch)
    descriptors = [
        _local_release_descriptor(tmp_path, "0.6.0"),
        _local_release_descriptor(tmp_path, "0.6.1"),
    ]
    environment = _tool_environment(tmp_path)

    async def fake_software_binding(**_kwargs: Any) -> object:
        return SimpleNamespace()

    async def fake_prepare(**_kwargs: Any) -> UpgradeManagedBinding:
        return UpgradeManagedBinding(
            gateway_unit_path=str((tmp_path / "ricky-gateway.service").resolve()),
            gateway_unit_sha256="d" * 64,
            gateway_was_enabled=True,
            gateway_was_active=True,
        )

    def failing_restore(**_kwargs: Any) -> None:
        raise ManagedIntegrationError(RESTORE_FAILURE)

    real_lock = installation_cli.installation_operation_lock

    def gated_lock(**kwargs: Any) -> Any:
        if kwargs.get("mode") == "exclusive":
            raise InstallationError(ABORT_CAUSE)
        return real_lock(**kwargs)

    monkeypatch.setattr(
        installation_cli,
        "discover_installed_tool_environment",
        lambda: environment,
    )
    monkeypatch.setattr(installation_cli, "create_software_binding", fake_software_binding)
    monkeypatch.setattr(installation_cli, "prepare_managed_upgrade", fake_prepare)
    monkeypatch.setattr(installation_cli, "restore_gateway_after_aborted_prepare", failing_restore)
    monkeypatch.setattr(installation_cli, "installation_operation_lock", gated_lock)

    source: list[str] = []
    for descriptor in descriptors:
        source.extend(("--release-descriptor", str(descriptor)))
    result = runner.invoke(app, ["upgrade", "--yes", "--to", "0.6.1", *source])

    assert result.exit_code == 1
    # The renderer wraps to the terminal width, so compare on collapsed
    # whitespace rather than on the wrapped lines.
    rendered = " ".join(result.stdout.split())
    assert f"Installation error: {ABORT_CAUSE}" in rendered
    assert f"could not be restarted: {RESTORE_FAILURE}" in rendered


def _prepared_journal(root: Path) -> UpgradeJournal:
    root.mkdir(mode=0o700, parents=True)
    operation_id = "b" * 32
    plan = MigrationPlan.create(
        source_data_generation=1,
        target_data_generation=2,
        steps=(
            MigrationStep(
                adapter_id="authority",
                step_id="schema-v4-v5",
                target_id="installation",
                physical_path=str((root / "authority.sqlite3").resolve()),
                source_schema_version=4,
                target_schema_version=5,
            ),
        ),
    )
    return create_upgrade_journal(
        user_data_dir=root,
        installation_id="a" * 32,
        operation_id=operation_id,
        source_software_version=ReleaseVersion.parse("0.6.0"),
        target_software_version=ReleaseVersion.parse("0.6.1"),
        plan=plan,
        backup_manifest_path=root / "upgrades" / operation_id / "backup" / "manifest.json",
    )


def test_unsettled_upgrade_journal_is_never_reported_as_a_finished_operation(
    tmp_path: Path,
) -> None:
    """A journal that is still mid-operation must not render as a rollback.

    The renderer used to treat every state other than ``completed`` as a
    rollback, so a recovery that had not restored anything still announced
    "Rolled back Ricky installation."
    """

    journal = _prepared_journal(tmp_path / "data")

    with pytest.raises(InstallationError, match="still prepared"):
        installation_cli._render_upgrade_result(journal)
