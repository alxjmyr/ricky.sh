"""Chrome readiness CLI tests."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import Mock

from typer.testing import CliRunner

from ricky.browser.types import (
    BrowserResource,
    BrowserResourceList,
    BrowserStatus,
)
from ricky.interfaces.cli.app import app
from ricky.interfaces.cli.browser import _setup_resource
from ricky.profiles import ProfileResourceRef

runner = CliRunner()


def _status(tmp_path: Path, *, enabled: bool, ready: bool) -> BrowserStatus:
    root = tmp_path / "user-data" / "browser" / "browsers"
    return BrowserStatus(
        enabled=enabled,
        ready=ready,
        playwright_version="1.62.0",
        diagnostic=None
        if ready
        else "Install Google Chrome Stable through your host package manager.",
        executable=str(root / "google-chrome"),
    )


def test_browser_status_reports_missing_binary_and_repair_command(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def status(_settings):
        return _status(tmp_path, enabled=True, ready=False)

    monkeypatch.setattr("ricky.interfaces.cli.browser.browser_status", status)

    result = runner.invoke(app, ["browser", "status"])

    assert result.exit_code == 1
    assert "browser control: enabled" in result.stdout
    assert "Google Chrome Stable: unavailable" in result.stdout
    assert "Install Google Chrome Stable" in result.stdout


def test_browser_status_is_successful_when_installed_but_disabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def status(_settings):
        return _status(tmp_path, enabled=False, ready=True)

    monkeypatch.setattr("ricky.interfaces.cli.browser.browser_status", status)

    result = runner.invoke(app, ["browser", "status"])

    assert result.exit_code == 0
    assert "browser control: disabled" in result.stdout
    assert "Google Chrome Stable: available" in result.stdout


def test_browser_install_command_is_removed() -> None:
    result = runner.invoke(app, ["browser", "install"])
    assert result.exit_code == 2
    assert "No such command" in result.output


def test_browser_resources_lists_safe_metadata_without_local_details(monkeypatch) -> None:
    async def resources(_profile, _access_profiles):
        return BrowserResourceList(
            resources=(
                BrowserResource(
                    resource=ProfileResourceRef(profile="personal", name="main"),
                    kind="persistent",
                    description="Personal signed-in browser",
                    availability="available",
                    headless=False,
                    process_owned=True,
                ),
                BrowserResource(
                    resource=ProfileResourceRef(profile="personal", name="debug"),
                    kind="cdp",
                    description="Dedicated local browser",
                    availability="busy",
                    process_owned=False,
                ),
            )
        )

    monkeypatch.setattr("ricky.interfaces.cli.browser._list_resources", resources)

    result = runner.invoke(app, ["browser", "resources"])

    assert result.exit_code == 0
    assert "personal/main  persistent  available  process=owned" in result.stdout
    assert "personal/debug  cdp  busy  process=external" in result.stdout
    assert "endpoint" not in result.stdout
    assert "user-data" not in result.stdout


def test_browser_check_uses_exact_qualified_resource(monkeypatch) -> None:
    observed: list[str] = []

    async def check(resource: str) -> None:
        observed.append(resource)

    monkeypatch.setattr("ricky.interfaces.cli.browser._check_resource", check)

    result = runner.invoke(app, ["browser", "check", "personal/main"])

    assert result.exit_code == 0
    assert observed == ["personal/main"]
    assert "personal/main is available" in result.stdout


def test_browser_setup_uses_manual_owner_without_snapshot(monkeypatch) -> None:
    calls = []
    settings = Mock()
    monkeypatch.setattr("ricky.interfaces.cli.browser.load_settings", lambda: settings)

    class Process:
        async def wait(self):
            return 0

    @asynccontextmanager
    async def manual(_settings, *, scope, resource):
        calls.append(("open", resource.qualified))
        try:
            yield Process()
        finally:
            calls.append(("close", resource.qualified))

    monkeypatch.setattr("ricky.interfaces.cli.browser.manual_browser_setup", manual)
    result = runner.invoke(app, ["browser", "setup", "personal/main"], input="\n")
    assert result.exit_code == 0
    assert calls == [("open", "personal/main"), ("close", "personal/main")]
    assert "snapshot" not in result.stdout.casefold()


def test_browser_reset_requires_confirmation_and_supports_explicit_yes(monkeypatch) -> None:
    observed: list[str] = []

    async def reset(resource: str) -> None:
        observed.append(resource)

    monkeypatch.setattr("ricky.interfaces.cli.browser._reset_resource", reset)

    denied = runner.invoke(app, ["browser", "reset", "personal/main"], input="n\n")
    accepted = runner.invoke(app, ["browser", "reset", "personal/main", "--yes"])

    assert denied.exit_code == 0
    assert "reset cancelled" in denied.stdout
    assert accepted.exit_code == 0
    assert observed == ["personal/main"]
    assert "Reset browser resource personal/main" in accepted.stdout


def test_browser_resource_commands_reject_unqualified_identity() -> None:
    result = runner.invoke(app, ["browser", "check", "main"])

    assert result.exit_code == 1
    assert "profile/name" in result.stdout


async def test_browser_setup_wait_is_cancellable_and_closes_owned_resources(monkeypatch) -> None:
    closed = asyncio.Event()
    entered = asyncio.Event()
    settings = Mock()
    monkeypatch.setattr("ricky.interfaces.cli.browser.load_settings", lambda: settings)

    class Process:
        async def wait(self):
            entered.set()
            await asyncio.Future()

    @asynccontextmanager
    async def manual(*_args, **_kwargs):
        try:
            yield Process()
        finally:
            closed.set()

    class Renderer:
        def render_status(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr("ricky.interfaces.cli.browser.manual_browser_setup", manual)
    renderer = Renderer()
    task = asyncio.create_task(_setup_resource("personal/main", renderer))  # type: ignore[arg-type]
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=1)
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancelled setup did not propagate cancellation")
    assert closed.is_set()
