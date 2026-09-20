"""Explicit legacy uv-tool binding from an isolated release coordinator."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ricky.upgrades import environment as module


def _layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, str]]:
    source = tmp_path / "tools" / "ricky"
    bootstrap = tmp_path / "bootstrap"
    bins = tmp_path / "bin"
    bins.mkdir()
    for root in (source, bootstrap):
        (root / "bin").mkdir(parents=True)
        (root / "site-packages" / "ricky").mkdir(parents=True)
        for name in ("python", "ricky"):
            executable = root / "bin" / name
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o700)
    (bins / "ricky").symlink_to(source / "bin" / "ricky")
    monkeypatch.setenv("UV_TOOL_DIR", str(source.parent))
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(bins))
    monkeypatch.setenv("PYTHONPATH", "do-not-inherit")
    monkeypatch.setattr(sys, "prefix", str(bootstrap))
    monkeypatch.setattr(sys, "argv", [str(bootstrap / "bin" / "ricky")])
    monkeypatch.setattr(
        module.metadata,
        "distribution",
        lambda _: SimpleNamespace(
            version="0.8.9",
            locate_file=lambda _: bootstrap / "site-packages" / "ricky",
            read_text=lambda name: "Wheel-Version: 1.0" if name == "WHEEL" else None,
        ),
    )
    probe = {
        "version": "0.8.7",
        "prefix": str(source),
        "package_root": str(source / "site-packages" / "ricky"),
    }

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert command[:4] == [str(source / "bin" / "python"), "-I", "-B", "-c"]
        assert "import ricky" not in command[4]
        assert kwargs["timeout"] == 20
        assert "PYTHONPATH" not in kwargs["env"]  # type: ignore[operator]
        return subprocess.CompletedProcess(command, 0, json.dumps(probe).encode(), b"")

    monkeypatch.setattr(module.subprocess, "run", run)
    return bins / "ricky", probe


def test_bootstrap_binds_old_tool_without_relaxing_ordinary_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, probe = _layout(tmp_path, monkeypatch)
    before = {path: path.stat().st_mtime_ns for path in tmp_path.rglob("*")}
    found = module.discover_bootstrap_tool_environment(entry)
    assert str(found.current_version) == "0.8.7"
    assert found.environment == probe["prefix"]
    assert found.executable == str(entry.resolve())
    assert module.InstalledToolEnvironment.model_validate_json(found.model_dump_json()) == found
    assert before == {path: path.stat().st_mtime_ns for path in tmp_path.rglob("*")}
    with pytest.raises(module.UpgradeEnvironmentError, match="uv-tool-installed"):
        module.discover_installed_tool_environment()


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "0.8.7rc1"),
        ("prefix", "/tmp"),
        ("package_root", "/tmp"),
        ("extra", "unexpected"),
    ],
)
def test_bootstrap_rejects_foreign_or_invalid_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    entry, probe = _layout(tmp_path, monkeypatch)
    probe[field] = value
    with pytest.raises(module.UpgradeEnvironmentError, match="exact installed"):
        module.discover_bootstrap_tool_environment(entry)


@pytest.mark.parametrize("kind", ["editable", "checkout", "missing_wheel", "foreign_entry"])
def test_bootstrap_rejects_unreleased_coordinator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    entry, _ = _layout(tmp_path, monkeypatch)
    distribution = module.metadata.distribution("ricky")
    if kind == "editable":
        monkeypatch.setattr(distribution, "read_text", lambda _: '{"dir_info":{"editable":true}}')
    elif kind == "missing_wheel":
        monkeypatch.setattr(distribution, "read_text", lambda _: None)
    elif kind == "checkout":
        monkeypatch.setattr(distribution, "locate_file", lambda _: tmp_path)
    else:
        monkeypatch.setattr(sys, "argv", [str(entry)])
    monkeypatch.setattr(module.metadata, "distribution", lambda _: distribution)
    with pytest.raises(module.UpgradeEnvironmentError, match="isolated, non-editable"):
        module.discover_bootstrap_tool_environment(entry)


@pytest.mark.parametrize("kind", ["timeout", "error", "malformed", "oversized", "failed"])
def test_probe_failures_are_bounded_and_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    entry, _ = _layout(tmp_path, monkeypatch)

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if kind == "timeout":
            raise subprocess.TimeoutExpired("secret", 20, output=b"secret")
        if kind == "error":
            raise OSError("secret")
        return subprocess.CompletedProcess(
            [],
            1 if kind == "failed" else 0,
            b"s" * 20_000 if kind == "oversized" else b"secret",
            b"secret",
        )

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(module.UpgradeEnvironmentError) as error:
        module.discover_bootstrap_tool_environment(entry)
    assert "secret" not in str(error.value)
    assert len(str(error.value)) < 200


def test_bootstrap_requires_explicit_canonical_owned_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, _ = _layout(tmp_path, monkeypatch)
    for supplied in (Path("ricky"), Path(sys.argv[0])):
        with pytest.raises(module.UpgradeEnvironmentError):
            module.discover_bootstrap_tool_environment(supplied)
    entry.unlink()
    entry.symlink_to(Path(sys.argv[0]))
    with pytest.raises(module.UpgradeEnvironmentError):
        module.discover_bootstrap_tool_environment(tmp_path / "tools" / "ricky" / "bin" / "ricky")


def test_bootstrap_entry_cannot_escape_its_prefix_through_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, _ = _layout(tmp_path, monkeypatch)
    bootstrap_entry = Path(sys.argv[0])
    foreign = tmp_path / "foreign-ricky"
    foreign.write_text("#!/bin/sh\n")
    foreign.chmod(0o700)
    bootstrap_entry.unlink()
    bootstrap_entry.symlink_to(foreign)
    with pytest.raises(module.UpgradeEnvironmentError):
        module.discover_bootstrap_tool_environment(entry)
