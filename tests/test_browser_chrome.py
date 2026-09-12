"""Chrome discovery, bounded probing, and host-environment isolation."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ricky.browser import chrome
from ricky.config import BrowserSettings, RickySettings


def _settings(tmp_path: Path, executable: Path | None = None) -> RickySettings:
    return RickySettings(
        user_data_dir=str(tmp_path / "user"),
        project_data_dir=str(tmp_path / "project"),
        browser=BrowserSettings(executable_path=executable),
    )


def _executable(tmp_path: Path, body: str) -> Path:
    executable = tmp_path / "google-chrome"
    executable.write_text("#!/bin/sh\n" + body)
    executable.chmod(0o700)
    return executable


async def test_missing_chrome_never_creates_browser_or_project_state(tmp_path, monkeypatch):
    monkeypatch.setattr(chrome, "_CHROME_LOCATIONS", (tmp_path / "absent",))
    status = await chrome.browser_status(_settings(tmp_path))
    assert not status.ready
    assert not status.enabled
    assert status.executable is None
    assert status.diagnostic is not None
    assert "install" in status.diagnostic
    assert not (tmp_path / "user").exists()
    assert not (tmp_path / "project").exists()


async def test_chrome_version_is_checked_again_after_host_update(tmp_path):
    executable = _executable(tmp_path, "echo 'Google Chrome 140.0.1.2'\n")
    settings = _settings(tmp_path, executable)
    first = await chrome.browser_status(settings)
    assert first.ready
    assert first.version == "140.0.1.2"
    _executable(tmp_path, "echo 'Google Chrome 141.0.2.3'\n")
    second = await chrome.browser_status(settings)
    assert second.version == "141.0.2.3"
    assert await chrome.require_chrome(settings) == executable


@pytest.mark.parametrize(
    "body",
    [
        "echo 'Chromium 140.0.1.2'",
        "echo 'Google Chrome for Testing 140.0.1.2'",
        "echo 'Google Chrome 140.0.1.2 beta'",
        "echo 'Google Chrome 140.0.1.2'; exit 7",
        "echo 'sensitive-child-diagnostic' >&2; exit 2",
        "yes sensitive-child-diagnostic",
    ],
)
async def test_wrong_or_failing_product_is_rejected_without_raw_output(tmp_path, body):
    status = await chrome.browser_status(_settings(tmp_path, _executable(tmp_path, body)))
    assert not status.ready
    assert "sensitive-child-diagnostic" not in status.model_dump_json()


async def test_explicit_missing_override_does_not_fall_back(tmp_path, monkeypatch):
    executable = _executable(tmp_path, "echo 'Google Chrome 140.0.1.2'")
    monkeypatch.setattr(chrome, "_CHROME_LOCATIONS", (executable,))
    with pytest.raises(chrome.ChromeDiscoveryError, match="unavailable"):
        await chrome.require_chrome(_settings(tmp_path, tmp_path / "missing"))


async def test_probe_timeout_kills_owned_child(tmp_path, monkeypatch):
    executable = _executable(tmp_path, "sleep 60")
    monkeypatch.setattr(chrome, "_PROBE_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(chrome.ChromeDiscoveryError, match="timed out"):
        await asyncio.wait_for(chrome._probe_version(executable), timeout=2)


async def test_probe_cancellation_reaps_owned_process(tmp_path, monkeypatch):
    original = asyncio.create_subprocess_exec
    processes = []

    async def create(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(chrome.asyncio, "create_subprocess_exec", create)
    executable = _executable(tmp_path, "sleep 60")
    task = asyncio.create_task(chrome._probe_version(executable))
    while not processes:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert processes[0].returncode is not None


def test_child_environment_removes_debug_and_bundled_path_overrides(monkeypatch):
    for key in ("DEBUG", "DEBUGP", "PWDEBUG", "PLAYWRIGHT_BROWSERS_PATH"):
        monkeypatch.setenv(key, "unsafe")
    monkeypatch.setenv("DISPLAY", ":99")
    environment = chrome.chrome_environment()
    assert environment["DISPLAY"] == ":99"
    assert not {"DEBUG", "DEBUGP", "PWDEBUG", "PLAYWRIGHT_BROWSERS_PATH"} & environment.keys()


async def test_discovery_uses_known_location_order_and_ignores_path(tmp_path, monkeypatch):
    first = _executable(tmp_path, "echo 'Google Chrome 140.0.1.2'")
    second = tmp_path / "second"
    second.write_text("#!/bin/sh\necho 'Google Chrome 141.0.2.3'\n")
    second.chmod(0o700)
    monkeypatch.setattr(chrome, "_CHROME_LOCATIONS", (first, second))
    monkeypatch.setenv("PATH", "/untrusted/bin")
    status = await chrome.browser_status(_settings(tmp_path))
    assert status.executable == str(first)
    assert status.version == "140.0.1.2"


async def test_non_executable_override_is_unavailable(tmp_path):
    executable = _executable(tmp_path, "echo 'Google Chrome 140.0.1.2'")
    executable.chmod(0o600)
    status = await chrome.browser_status(_settings(tmp_path, executable))
    assert not status.ready


async def test_executable_os_failure_is_sanitized(tmp_path):
    executable = _executable(tmp_path, "")
    executable.write_text("#!/missing-sensitive-interpreter\n")
    status = await chrome.browser_status(_settings(tmp_path, executable))
    assert not status.ready
    assert status.diagnostic == "Google Chrome could not be executed"


async def test_disabled_service_constructs_without_chrome_or_state(tmp_path, monkeypatch):
    from ricky.browser.service import BrowserService

    monkeypatch.setattr(chrome, "_CHROME_LOCATIONS", (tmp_path / "absent",))
    settings = _settings(tmp_path)
    service = await BrowserService.create(settings, scope=settings.resolve_profile_scope())
    try:
        assert (await service.resources()).resources == ()
    finally:
        await service.aclose()
    assert not (tmp_path / "user").exists()
    assert not (tmp_path / "project").exists()


async def test_owned_open_without_chrome_fails_before_state_allocation(tmp_path, monkeypatch):
    from ricky.browser.service import BrowserService
    from ricky.browser.types import BrowserError

    monkeypatch.setattr(chrome, "_CHROME_LOCATIONS", (tmp_path / "absent",))
    settings = _settings(tmp_path)
    settings.browser.enabled = True
    service = await BrowserService.create(settings, scope=settings.resolve_profile_scope())
    try:
        with pytest.raises(BrowserError, match="Google Chrome Stable is unavailable"):
            await service.open_session()
    finally:
        await service.aclose()
    assert not (tmp_path / "user").exists()
    assert not (tmp_path / "project").exists()


async def test_probe_cancellation_during_startup_joins_and_reaps(tmp_path, monkeypatch):
    original = asyncio.create_subprocess_exec
    started = asyncio.Event()
    release = asyncio.Event()
    processes = []

    async def create(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        started.set()
        await release.wait()
        return process

    monkeypatch.setattr(chrome.asyncio, "create_subprocess_exec", create)
    executable = _executable(tmp_path, "sleep 60")
    task = asyncio.create_task(chrome._probe_version(executable))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert processes[0].returncode is not None
