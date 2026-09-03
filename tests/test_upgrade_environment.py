"""Read-only uv tool environment identity checks."""

from __future__ import annotations

import json
import os
import sys
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace

import pytest

from ricky.upgrades.environment import (
    InstalledToolEnvironment,
    UpgradeEnvironmentError,
    discover_installed_tool_environment,
)


def _tool_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    tool_root = tmp_path / "uv-tools"
    environment = tool_root / "ricky"
    environment_bin = environment / "bin"
    package_root = environment / "lib" / "python3.12" / "site-packages" / "ricky"
    bin_root = tmp_path / "uv-bin"
    environment_bin.mkdir(parents=True)
    package_root.mkdir(parents=True)
    bin_root.mkdir()
    executable = environment_bin / "ricky"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    entry = bin_root / "ricky"
    entry.symlink_to(executable)

    class _Distribution:
        version = "0.6.0"

        def locate_file(self, path: str) -> Path:
            assert path == "ricky"
            return package_root

    monkeypatch.setenv("UV_TOOL_DIR", str(tool_root))
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(bin_root))
    monkeypatch.setattr(metadata, "distribution", lambda name: _Distribution())
    monkeypatch.setattr(sys, "prefix", str(environment))
    monkeypatch.setattr(sys, "argv", [str(entry)])
    return {
        "tool_root": tool_root,
        "environment": environment,
        "bin": bin_root,
        "executable": executable,
        "package_root": package_root,
    }


def test_discovers_exact_canonical_uv_tool_environment_and_round_trips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _tool_layout(tmp_path, monkeypatch)

    discovered = discover_installed_tool_environment()

    assert discovered.tool_root == str(paths["tool_root"].resolve())
    assert discovered.environment == str(paths["environment"].resolve())
    assert discovered.bin == str(paths["bin"].resolve())
    assert discovered.executable == str(paths["executable"].resolve())
    assert discovered.package_root == str(paths["package_root"].resolve())
    assert str(discovered.current_version) == "0.6.0"
    assert InstalledToolEnvironment.model_validate_json(discovered.model_dump_json()) == discovered


def test_linux_defaults_are_derived_without_invoking_uv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    tool_root = home / ".local" / "share" / "uv" / "tools"
    environment = tool_root / "ricky"
    executable = environment / "bin" / "ricky"
    package_root = environment / "lib" / "python3.12" / "site-packages" / "ricky"
    bin_root = home / ".local" / "bin"
    executable.parent.mkdir(parents=True)
    package_root.mkdir(parents=True)
    bin_root.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    (bin_root / "ricky").symlink_to(executable)
    monkeypatch.setenv("HOME", str(home))
    for variable in ("UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "XDG_DATA_HOME", "XDG_BIN_HOME"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(
        metadata,
        "distribution",
        lambda _name: SimpleNamespace(
            version="0.6.0",
            locate_file=lambda _path: package_root,
        ),
    )
    monkeypatch.setattr(sys, "prefix", str(environment))
    monkeypatch.setattr(sys, "argv", [str(bin_root / "ricky")])

    discovered = discover_installed_tool_environment()

    assert discovered.tool_root == str(tool_root)
    assert discovered.bin == str(bin_root)


@pytest.mark.parametrize("version", ["0.0.0", "0.5", "01.2.3", "0.6.0rc1"])
def test_rejects_nonrelease_or_fallback_package_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str,
) -> None:
    _tool_layout(tmp_path, monkeypatch)
    monkeypatch.setattr(
        metadata,
        "distribution",
        lambda _name: SimpleNamespace(version=version, locate_file=lambda _path: tmp_path),
    )

    with pytest.raises(UpgradeEnvironmentError, match="uv-tool-installed.*check remains available"):
        discover_installed_tool_environment()


def test_rejects_a_development_prefix_or_different_console_script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _tool_layout(tmp_path, monkeypatch)
    developer_environment = tmp_path / "checkout" / ".venv"
    developer_environment.mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(developer_environment))

    with pytest.raises(UpgradeEnvironmentError, match="uv-tool-installed"):
        discover_installed_tool_environment()

    monkeypatch.setattr(sys, "prefix", str(paths["environment"]))
    other = tmp_path / "other-ricky"
    other.write_text("#!/bin/sh\n", encoding="utf-8")
    other.chmod(0o700)
    monkeypatch.setattr(sys, "argv", [str(other)])

    with pytest.raises(UpgradeEnvironmentError, match="uv-tool-installed"):
        discover_installed_tool_environment()


def test_rejects_package_code_or_entry_point_outside_the_tool_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _tool_layout(tmp_path, monkeypatch)
    outside_package = tmp_path / "outside" / "ricky"
    outside_package.mkdir(parents=True)
    monkeypatch.setattr(
        metadata,
        "distribution",
        lambda _name: SimpleNamespace(
            version="0.6.0",
            locate_file=lambda _path: outside_package,
        ),
    )

    with pytest.raises(UpgradeEnvironmentError, match="uv-tool-installed"):
        discover_installed_tool_environment()

    monkeypatch.setattr(
        metadata,
        "distribution",
        lambda _name: SimpleNamespace(
            version="0.6.0",
            locate_file=lambda _path: paths["package_root"],
        ),
    )
    outside_executable = tmp_path / "outside-ricky"
    outside_executable.write_text("#!/bin/sh\n", encoding="utf-8")
    outside_executable.chmod(0o700)
    (paths["bin"] / "ricky").unlink()
    (paths["bin"] / "ricky").symlink_to(outside_executable)

    with pytest.raises(UpgradeEnvironmentError, match="uv-tool-installed"):
        discover_installed_tool_environment()


def test_missing_metadata_and_relative_uv_roots_fail_with_bounded_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tool_layout(tmp_path, monkeypatch)

    def _missing(_name: str) -> None:
        raise metadata.PackageNotFoundError("ricky")

    monkeypatch.setattr(metadata, "distribution", _missing)
    with pytest.raises(UpgradeEnvironmentError) as missing:
        discover_installed_tool_environment()
    assert len(str(missing.value)) < 200
    assert "check remains available" in str(missing.value)

    monkeypatch.setattr(
        metadata,
        "distribution",
        lambda _name: SimpleNamespace(version="0.6.0", locate_file=lambda _path: tmp_path),
    )
    monkeypatch.setenv("UV_TOOL_DIR", "relative/tools")
    with pytest.raises(UpgradeEnvironmentError) as relative:
        discover_installed_tool_environment()
    assert str(relative.value) == str(missing.value)


def test_model_rejects_noncanonical_paths() -> None:
    with pytest.raises(ValueError, match="absolute and canonical"):
        InstalledToolEnvironment(
            tool_root="relative",
            environment="/tmp/environment",
            bin="/tmp/bin",
            executable="/tmp/environment/bin/ricky",
            package_root="/tmp/environment/site-packages/ricky",
            current_version="0.6.0",  # type: ignore[arg-type]
        )


def test_discovery_does_not_mutate_the_tool_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _tool_layout(tmp_path, monkeypatch)
    before = {
        name: (path.lstat().st_mode, path.lstat().st_mtime_ns) for name, path in paths.items()
    }

    discover_installed_tool_environment()

    after = {name: (path.lstat().st_mode, path.lstat().st_mtime_ns) for name, path in paths.items()}
    assert after == before
    assert json.loads(InstalledToolEnvironment.discover().model_dump_json())["current_version"] == (
        "0.6.0"
    )
    assert os.path.samefile(paths["executable"], paths["bin"] / "ricky")
