"""Manual setup owns a normal process and a private, exclusively leased profile."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from ricky.browser import setup
from ricky.browser.lease import BrowserResourceLease
from ricky.browser.resources import persistent_browser_path
from ricky.browser.types import BrowserError
from ricky.config import RickySettings
from ricky.profiles import ProfileResourceRef


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / "project"),
            "profiles": {"enabled": ["shared", "personal"], "default": "personal"},
            "profile_configs": {
                "personal": {
                    "browser": {
                        "resources": {"main": {"kind": "persistent", "description": "Main"}}
                    }
                }
            },
        }
    )


@pytest.mark.parametrize("interrupt", [False, True])
async def test_manual_setup_leases_profile_and_joins_process(tmp_path, monkeypatch, interrupt):
    settings = _settings(tmp_path)
    ref = ProfileResourceRef(profile="personal", name="main")
    scope = settings.resolve_profile_scope("personal")
    calls = []
    original_spawn = asyncio.create_subprocess_exec

    async def chrome(_settings):
        return Path("/usr/bin/google-chrome")

    async def spawn(*args, **kwargs):
        calls.append(args)
        return await original_spawn(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)

    monkeypatch.setattr(setup, "require_chrome", chrome)
    monkeypatch.setattr(setup.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(setup, "chrome_environment", lambda: {"DISPLAY": ":test"})
    entered = asyncio.Event()
    observed = []

    async def operation():
        async with setup.manual_browser_setup(settings, scope=scope, resource=ref) as process:
            observed.append(process)
            assert BrowserResourceLease(settings, ref).is_active()
            with pytest.raises(BrowserError, match="already in use"):
                BrowserResourceLease(settings, ref).acquire()
            state = persistent_browser_path(settings, ref)
            assert state.is_relative_to(Path(settings.user_data_dir))
            assert state.stat().st_mode & 0o777 == 0o700
            entered.set()
            if interrupt:
                await asyncio.Future()
            else:
                process.terminate()

    task = asyncio.create_task(operation())
    await asyncio.wait_for(entered.wait(), 5)
    if interrupt:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(BrowserError, match="exited unsuccessfully"):
            await task
    assert observed[0].returncode is not None
    assert not BrowserResourceLease(settings, ref).is_active()
    assert not Path(settings.project_data_dir).exists()
    arguments = calls[0]
    assert arguments[-1] == "about:blank"
    assert "--new-window" in arguments
    assert not any("debugging" in arg or "automation" in arg for arg in arguments)
    assert persistent_browser_path(settings, ref).is_dir()


async def test_manual_setup_requires_display_before_creating_state(tmp_path, monkeypatch):
    settings = _settings(tmp_path)

    async def chrome(_settings):
        return Path("/usr/bin/google-chrome")

    monkeypatch.setattr(setup, "require_chrome", chrome)
    monkeypatch.setattr(setup, "chrome_environment", dict)
    with pytest.raises(BrowserError, match="graphical display"):
        async with setup.manual_browser_setup(
            settings,
            scope=settings.resolve_profile_scope("personal"),
            resource=ProfileResourceRef(profile="personal", name="main"),
        ):
            pytest.fail("setup must not launch without a display")
    assert not Path(settings.user_data_dir).exists()


async def test_manual_setup_rejects_out_of_scope_resource(tmp_path):
    settings = _settings(tmp_path)
    with pytest.raises(BrowserError, match="unknown browser resource"):
        async with setup.manual_browser_setup(
            settings,
            scope=settings.resolve_profile_scope("shared"),
            resource=ProfileResourceRef(profile="personal", name="main"),
        ):
            pytest.fail("setup must not widen scope")
    assert not Path(settings.user_data_dir).exists()


async def test_manual_setup_startup_cancellation_joins_late_process(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    ref = ProfileResourceRef(profile="personal", name="main")
    started = asyncio.Event()
    proceed = asyncio.Event()
    original_spawn = asyncio.create_subprocess_exec
    observed = []

    async def chrome(_settings):
        return Path("/usr/bin/google-chrome")

    async def spawn(*_args, **kwargs):
        started.set()
        await proceed.wait()
        process = await original_spawn(
            sys.executable, "-c", "import time; time.sleep(60)", **kwargs
        )
        observed.append(process)
        return process

    monkeypatch.setattr(setup, "require_chrome", chrome)
    monkeypatch.setattr(setup.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(setup, "chrome_environment", lambda: {"DISPLAY": ":test"})

    async def operation():
        async with setup.manual_browser_setup(
            settings, scope=settings.resolve_profile_scope("personal"), resource=ref
        ):
            pytest.fail("cancelled startup must not enter setup")

    task = asyncio.create_task(operation())
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert observed[0].returncode is not None
    assert not BrowserResourceLease(settings, ref).is_active()


async def test_manual_cleanup_failure_retains_lease(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    ref = ProfileResourceRef(profile="personal", name="main")
    lease = BrowserResourceLease(settings, ref)
    lease.acquire()

    class Process:
        pid = 123456
        returncode = 0

        async def wait(self):
            return 0

    def fail_inspection(_group):
        raise OSError("cannot inspect process ownership")

    monkeypatch.setattr(setup.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(setup, "_group_running", fail_inspection)
    try:
        with pytest.raises(OSError, match="cannot inspect"):
            await setup._close_setup(Process(), lease)  # type: ignore[arg-type]
        assert lease.held
    finally:
        lease.release()
