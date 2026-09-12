"""Exact uv replacement and inherited-lock handoff tests."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ricky.upgrades.environment import InstalledToolEnvironment
from ricky.upgrades.journal import UpgradeSoftwareBinding, create_upgrade_journal
from ricky.upgrades.models import MigrationPlan, ReleaseDescriptor
from ricky.upgrades.software import (
    SoftwareReplacementError,
    UpgradeHandoffComplete,
    UvToolSoftwareController,
)
from ricky.upgrades.versions import ReleaseVersion

OPERATION_ID = "b" * 32


def _release(root: Path, version: str, endpoint: str) -> tuple[ReleaseDescriptor, Path, Path]:
    cache = root / "upgrades" / OPERATION_ID / "artifacts" / endpoint
    cache.mkdir(parents=True)
    wheel = cache / f"ricky-{version}-py3-none-any.whl"
    constraints = cache / f"ricky-{version}-constraints.txt"
    wheel.write_bytes(f"wheel-{version}".encode())
    constraints.write_bytes(f"dependency=={version}\n".encode())

    def artifact(path: Path) -> dict[str, object]:
        payload = path.read_bytes()
        return {
            "name": path.name,
            "url": path.as_uri(),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }

    descriptor = ReleaseDescriptor.model_validate_json(
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
        )
    )
    return descriptor, wheel, constraints


class _Lock:
    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor
        self.transferred = False

    def require_owned_exclusive(self) -> None:
        if self.transferred:
            raise AssertionError("lock was already transferred")

    def transfer_to_child(self) -> int:
        self.transferred = True
        return self.descriptor


def _fixture(tmp_path: Path) -> tuple[Any, InstalledToolEnvironment, _Lock]:
    root = (tmp_path / "data").resolve()
    root.mkdir()
    source, source_wheel, source_constraints = _release(root, "0.6.0", "source")
    target, target_wheel, target_constraints = _release(root, "0.6.1", "target")
    binding = UpgradeSoftwareBinding(
        source_release=source,
        target_release=target,
        source_wheel_path=str(source_wheel),
        source_constraints_path=str(source_constraints),
        target_wheel_path=str(target_wheel),
        target_constraints_path=str(target_constraints),
    )
    journal = create_upgrade_journal(
        user_data_dir=root,
        installation_id="a" * 32,
        operation_id=OPERATION_ID,
        source_software_version=ReleaseVersion.parse("0.6.0"),
        target_software_version=ReleaseVersion.parse("0.6.1"),
        plan=MigrationPlan.create(source_data_generation=1, target_data_generation=1),
        backup_manifest_path=root / "upgrades" / OPERATION_ID / "backup" / "manifest.json",
        software=binding,
    )
    tool = tmp_path / "tools" / "ricky"
    executable = tool / "bin" / "ricky"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    python = tool / "bin" / "python"
    python.write_text("binary", encoding="utf-8")
    python.chmod(0o700)
    package = tool / "lib" / "ricky"
    package.mkdir(parents=True)
    bin_root = tmp_path / "bin"
    bin_root.mkdir()
    environment = InstalledToolEnvironment(
        tool_root=str((tmp_path / "tools").resolve()),
        environment=str(tool.resolve()),
        bin=str(bin_root.resolve()),
        executable=str(executable.resolve()),
        package_root=str(package.resolve()),
        current_version=ReleaseVersion.parse("0.6.0"),
    )
    lock_file = tmp_path / "lock"
    descriptor = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    return journal, environment, _Lock(descriptor)


def test_uv_install_uses_exact_cached_pair_sanitized_environment_and_no_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/test-only/bus")
    monkeypatch.setenv("UNRELATED_API_TOKEN", "test-only-credential")
    journal, environment, lock = _fixture(tmp_path)
    uv = tmp_path / "uv"
    uv.write_text("binary", encoding="utf-8")
    uv.chmod(0o700)
    calls: list[tuple[list[str], dict[str, Any]]] = []
    installed = "0.6.0"

    def run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        nonlocal installed
        calls.append((command, kwargs))
        if command[-1] == "--version" and command[0] == str(uv):
            return SimpleNamespace(returncode=0, stdout=b"uv 0.11.28\n", stderr=b"")
        if command[-1] == "--version":
            return SimpleNamespace(
                returncode=0,
                stdout=f"ricky {installed}\n".encode(),
                stderr=b"",
            )
        installed = "0.6.1"
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr("subprocess.run", run)
    controller = UvToolSoftwareController(
        environment=environment,
        lock=lock,  # type: ignore[arg-type]
        uv_executable=uv,
    )
    monkeypatch.setattr(controller, "_handoff", lambda *_args, **_kwargs: None)

    controller.install_target(journal)

    assert journal.software is not None
    install = next(command for command, _kwargs in calls if "install" in command)
    assert install[:4] == [str(uv), "tool", "install", "--force"]
    assert install[-1] == journal.software.target_wheel_path
    assert install[install.index("--constraints") + 1] == (journal.software.target_constraints_path)
    kwargs = next(kwargs for command, kwargs in calls if "install" in command)
    assert "shell" not in kwargs
    assert "PYTHONPATH" not in kwargs["env"]
    for _command, options in calls:
        assert not {"XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "UNRELATED_API_TOKEN"} & (
            options["env"].keys()
        )
    assert kwargs["env"]["UV_TOOL_DIR"] == environment.tool_root
    assert kwargs["env"]["UV_TOOL_BIN_DIR"] == environment.bin
    os.close(lock.descriptor)


def test_handoff_transfers_only_after_spawn_and_returns_child_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal, environment, lock = _fixture(tmp_path)
    uv = tmp_path / "uv"
    uv.write_text("binary", encoding="utf-8")
    uv.chmod(0o700)
    observed: dict[str, Any] = {}

    class Child:
        def wait(self, *, timeout: int) -> int:
            observed["timeout"] = timeout
            return 7

    def popen(arguments: list[str], **kwargs: Any) -> Child:
        assert lock.transferred is False
        observed["arguments"] = arguments
        observed["kwargs"] = kwargs
        return Child()

    monkeypatch.setattr("subprocess.Popen", popen)
    controller = UvToolSoftwareController(
        environment=environment,
        lock=lock,  # type: ignore[arg-type]
        uv_executable=uv,
        as_json=True,
    )

    with pytest.raises(UpgradeHandoffComplete) as completed:
        controller._handoff(journal, action="resume")

    assert completed.value.exit_code == 7
    assert lock.transferred is True
    assert observed["kwargs"]["pass_fds"] == (lock.descriptor,)
    assert observed["arguments"][-1] == "--json"
    os.close(lock.descriptor)


def test_uv_minimum_is_checked_before_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal, environment, lock = _fixture(tmp_path)
    assert journal.software is not None
    newer = journal.software.target_release.model_copy(
        update={"minimum_uv_version": ReleaseVersion.parse("99.0.0")}
    )
    binding = journal.software.model_copy(update={"target_release": newer})
    journal = journal.model_copy(update={"software": binding})
    uv = tmp_path / "uv"
    uv.write_text("binary", encoding="utf-8")
    uv.chmod(0o700)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=b"uv 0.11.28\n", stderr=b""),
    )
    controller = UvToolSoftwareController(
        environment=environment,
        lock=lock,  # type: ignore[arg-type]
        uv_executable=uv,
    )

    with pytest.raises(SoftwareReplacementError, match="requires uv"):
        controller.install_target(journal)
    os.close(lock.descriptor)


@pytest.mark.parametrize("action", ["resume", "rollback"])
@pytest.mark.parametrize(
    "session_keys",
    [
        (),
        ("XDG_RUNTIME_DIR",),
        ("DBUS_SESSION_BUS_ADDRESS",),
        ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"),
    ],
)
def test_real_handoff_preserves_session_for_gateway_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    session_keys: tuple[str, ...],
) -> None:
    """Run a child and the real unit reconciliation with an isolated fake manager."""
    journal, environment, lock = _fixture(tmp_path)
    session = {
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(tmp_path / "runtime" / "bus"),
    }
    for key, value in session.items():
        monkeypatch.delenv(key, raising=False)
        if key in session_keys:
            monkeypatch.setenv(key, value)
    monkeypatch.setenv("UNRELATED_API_TOKEN", "test-only-credential")
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "wrong-tools"))
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(tmp_path / "wrong-bin"))
    expected = {key: session[key] for key in session_keys}
    result_path = tmp_path / "result.json"
    executable = Path(environment.executable)
    executable.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(f"""\
            import asyncio
            import hashlib
            import json
            import os
            import sys
            from pathlib import Path
            from unittest.mock import patch
            from ricky.config import RickySettings
            from ricky.gateway.service_unit import GatewayServiceUnit, CommandResult
            from ricky.upgrades.integrations import (
                ManagedUpgradeController, ManagedIntegrationError,
            )
            from ricky.upgrades.journal import UpgradeManagedBinding

            expected = {expected!r}
            for key in ('XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS'):
                assert os.environ.get(key) == expected.get(key)
            assert 'UNRELATED_API_TOKEN' not in os.environ
            assert os.environ['UV_TOOL_DIR'] == {environment.tool_root!r}
            assert os.environ['UV_TOOL_BIN_DIR'] == {environment.bin!r}
            fd = int(sys.argv[sys.argv.index('--lock-fd') + 1])
            assert os.fstat(fd).st_ino == {os.fstat(lock.descriptor).st_ino}
            assert sys.argv[sys.argv.index('--action') + 1] == {action!r}
            settings = RickySettings.model_validate({{
                'user_data_dir': {str(tmp_path / "data")!r},
                'project_data_dir': {str(tmp_path / "project")!r},
                'gateway': {{'service': {{'unit_dir': {str(tmp_path / "units")!r}}}}},
            }})
            unit = GatewayServiceUnit(settings, executable={environment.executable!r})
            unit.install()
            binding = UpgradeManagedBinding(
                gateway_unit_path=str(unit.unit_path.resolve()),
                gateway_unit_sha256=hashlib.sha256(unit.render().encode()).hexdigest(),
                gateway_was_enabled=True,
            )
            calls = []
            def fake_manager(args):
                calls.append(list(args))
                assert args[0:2] == ('systemctl', '--user')
                available = bool(expected)
                return CommandResult(tuple(args), 0 if available else 1, '', '')
            controller = ManagedUpgradeController(
                user_data_dir=Path({str(tmp_path / "data")!r}),
                executable=Path({environment.executable!r}),
                run_async=asyncio.run,
            )
            with patch('ricky.gateway.service_unit.subprocess_runner', fake_manager):
                try:
                    outcome = controller._reconcile_gateway(
                        settings, binding,
                        endpoint={"source" if action == "rollback" else "target"!r},
                    )
                except ManagedIntegrationError as exc:
                    assert not expected
                    assert str(exc) == 'user service manager did not reload the gateway unit'
                    outcome = 'unavailable'
            result = {{'outcome': outcome, 'calls': calls}}
            Path({str(result_path)!r}).write_text(json.dumps(result))
            """),
        encoding="utf-8",
    )
    controller = UvToolSoftwareController(
        environment=environment,
        lock=lock,  # type: ignore[arg-type]
        uv_executable=executable,
    )
    try:
        with pytest.raises(UpgradeHandoffComplete) as completed:
            controller._handoff(journal, action=action)
        assert completed.value.exit_code == 0
        assert lock.transferred
        result = json.loads(result_path.read_text())
        assert result["outcome"] == ("inactive" if session_keys else "unavailable")
        assert result["calls"] == [
            ["systemctl", "--user", "daemon-reload"],
            *([["systemctl", "--user", "enable", "ricky-gateway.service"]] if session_keys else []),
        ]
        assert not (tmp_path / "project").exists()
    finally:
        os.close(lock.descriptor)


@pytest.mark.parametrize("spawn_fails", [False, True])
def test_handoff_keeps_lock_on_spawn_failure_and_reaps_timed_out_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spawn_fails: bool
) -> None:
    journal, environment, lock = _fixture(tmp_path)
    events: list[str] = []

    class Child:
        def wait(self, *, timeout: int | None = None) -> int:
            assert lock.transferred
            if timeout is not None:
                events.append("timeout")
                raise subprocess.TimeoutExpired("test-child", timeout)
            events.append("reaped")
            return -9

        def kill(self) -> None:
            events.append("killed")

    def popen(*args: Any, **kwargs: Any) -> Child:
        assert not lock.transferred
        if spawn_fails:
            raise OSError("test spawn failure")
        return Child()

    monkeypatch.setattr("subprocess.Popen", popen)
    controller = UvToolSoftwareController(
        environment=environment,
        lock=lock,  # type: ignore[arg-type]
        uv_executable=Path(environment.executable),
    )
    try:
        if spawn_fails:
            with pytest.raises(SoftwareReplacementError, match="could not be started"):
                controller._handoff(journal, action="resume")
            assert not lock.transferred
            assert events == []
        else:
            with pytest.raises(UpgradeHandoffComplete) as completed:
                controller._handoff(journal, action="resume")
            assert completed.value.exit_code == -9
            assert events == ["timeout", "killed", "reaped"]
    finally:
        os.close(lock.descriptor)
