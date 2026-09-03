"""Explicit Chromium installation and version-aware readiness tests."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from ricky.browser import install
from ricky.config import BrowserSettings, RickySettings


def _settings(tmp_path: Path, *, enabled: bool = False) -> RickySettings:
    return RickySettings(
        user_data_dir=str(tmp_path / "user"),
        project_data_dir=str(tmp_path / "project"),
        browser=BrowserSettings(enabled=enabled),
    )


async def test_status_probes_locked_executable_without_creating_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, enabled=True)
    expected_root = tmp_path / "user" / "browser" / "browsers"
    executable = expected_root / "chromium-123" / "chrome"

    async def run(args, *, env):
        assert tuple(args) == (sys.executable, "-m", "ricky.browser._probe")
        assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(expected_root)
        return install._CommandResult(returncode=0, stdout=str(executable).encode(), stderr=b"")

    monkeypatch.setattr(install, "_run_command", run)

    status = await install.browser_status(settings)

    assert status.enabled is True
    assert status.ready is False
    assert status.executable == str(executable)
    assert not (tmp_path / "user").exists()
    assert not (tmp_path / "project").exists()


async def test_install_uses_exact_playwright_command_and_confined_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    expected_root = tmp_path / "user" / "browser" / "browsers"
    executable = expected_root / "chromium-123" / "chrome"
    calls: list[tuple[str, ...]] = []

    async def run(args, *, env):
        observed = tuple(args)
        calls.append(observed)
        assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(expected_root)
        if "playwright" in observed:
            executable.parent.mkdir(parents=True)
            executable.write_text("binary", encoding="utf-8")
            executable.chmod(0o700)
            return install._CommandResult(returncode=0, stdout=b"", stderr=b"")
        return install._CommandResult(returncode=0, stdout=str(executable).encode(), stderr=b"")

    monkeypatch.setattr(install, "_run_command", run)

    status = await install.install_chromium(settings)

    assert status.ready is True
    assert calls == [
        (sys.executable, "-m", "playwright", "install", "chromium"),
        (sys.executable, "-m", "ricky.browser._probe"),
    ]
    if os.name == "posix":
        assert expected_root.stat().st_mode & 0o777 == 0o700
    assert not (tmp_path / "project").exists()


async def test_install_failure_is_bounded_and_does_not_emit_child_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run(_args, *, env):
        del env
        return install._CommandResult(
            returncode=7,
            stdout=b"provider-secret",
            stderr=b"proxy-password",
        )

    monkeypatch.setattr(install, "_run_command", run)

    with pytest.raises(install.BrowserInstallationError) as caught:
        await install.install_chromium(_settings(tmp_path))

    message = str(caught.value)
    assert "exit code 7" in message
    assert "provider-secret" not in message
    assert "proxy-password" not in message


async def test_probe_rejects_an_executable_outside_the_installation_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run(_args, *, env):
        del env
        return install._CommandResult(
            returncode=0,
            stdout=str(tmp_path / "outside" / "chrome").encode(),
            stderr=b"",
        )

    monkeypatch.setattr(install, "_run_command", run)

    with pytest.raises(install.BrowserInstallationError, match="outside Ricky"):
        await install.browser_status(_settings(tmp_path))


async def test_cancelled_installer_terminates_and_awaits_its_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        returncode = None

        def __init__(self) -> None:
            self.communications = 0
            self.terminated = False

        async def communicate(self):
            self.communications += 1
            if self.communications == 1:
                raise asyncio.CancelledError
            return b"", b""

        def terminate(self) -> None:
            self.terminated = True

    process = Process()

    async def create(*_args, **_kwargs):
        return process

    monkeypatch.setattr(install.asyncio, "create_subprocess_exec", create)

    with pytest.raises(asyncio.CancelledError):
        await install._run_command(("probe",), env={})

    assert process.terminated is True
    assert process.communications == 2
