"""Exact uv replacement and inherited-lock handoff tests."""

from __future__ import annotations

import hashlib
import json
import os
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
