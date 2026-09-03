"""Browser installation CLI tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

from typer.testing import CliRunner

from ricky.browser.types import (
    BrowserInstallStatus,
    BrowserPage,
    BrowserResource,
    BrowserResourceList,
    BrowserSession,
    BrowserSessionClosed,
)
from ricky.interfaces.cli.app import app
from ricky.interfaces.cli.browser import _setup_resource
from ricky.profiles import ProfileResourceRef

runner = CliRunner()


def _status(tmp_path: Path, *, enabled: bool, ready: bool) -> BrowserInstallStatus:
    root = tmp_path / "user-data" / "browser" / "browsers"
    return BrowserInstallStatus(
        enabled=enabled,
        ready=ready,
        install_dir=str(root),
        executable=str(root / "chromium-123" / "chrome"),
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
    assert "Chromium: not installed" in result.stdout
    assert "uv run ricky browser install" in result.stdout


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
    assert "Chromium: ready" in result.stdout


def test_browser_install_does_not_implicitly_enable_control(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def install(_settings):
        return _status(tmp_path, enabled=False, ready=True)

    monkeypatch.setattr("ricky.interfaces.cli.browser.install_chromium", install)

    result = runner.invoke(app, ["browser", "install"])

    assert result.exit_code == 0
    assert "Chromium is installed and ready" in result.stdout
    assert "browser control remains disabled" in result.stdout


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


def test_browser_setup_opens_a_blank_headed_resource_without_snapshot(monkeypatch) -> None:
    session_id = "browser_session_" + "a" * 32
    page_id = "browser_page_" + "b" * 32

    class SetupService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def resource(self, resource: str) -> BrowserResource:
            self.calls.append(("resource", resource))
            return BrowserResource(
                resource=ProfileResourceRef.from_qualified(resource),
                kind="persistent",
                description="Personal browser",
                availability="available",
                headless=False,
                process_owned=True,
            )

        async def open_resource(
            self,
            resource: str,
            *,
            headless: bool,
            start_blank: bool,
        ) -> BrowserSession:
            self.calls.append(("open", (resource, headless, start_blank)))
            page = BrowserPage(
                session_id=session_id,
                page_id=page_id,
                selected=True,
                url="about:blank",
                title="",
                navigation_generation=0,
            )
            return BrowserSession(
                session_id=session_id,
                resource=ProfileResourceRef.from_qualified(resource),
                mode="owned_persistent",
                headless=False,
                selected_page_id=page_id,
                pages=(page,),
            )

        async def close_session(self, selected_session_id: str) -> BrowserSessionClosed:
            self.calls.append(("close", selected_session_id))
            return BrowserSessionClosed(session_id=selected_session_id)

        async def aclose(self) -> None:
            self.calls.append(("aclose", None))

    service = SetupService()

    async def resource_service(_resource: str):
        return service

    monkeypatch.setattr("ricky.interfaces.cli.browser._resource_service", resource_service)
    result = runner.invoke(app, ["browser", "setup", "personal/main"], input="\n")

    assert result.exit_code == 0
    assert service.calls == [
        ("resource", "personal/main"),
        ("open", ("personal/main", False, True)),
        ("close", session_id),
        ("aclose", None),
    ]
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


async def test_browser_setup_input_is_cancellable_and_closes_owned_resources(
    monkeypatch,
) -> None:
    session_id = "browser_session_" + "a" * 32
    page_id = "browser_page_" + "b" * 32

    class SetupService:
        closed = asyncio.Event()

        def resource(self, resource: str) -> BrowserResource:
            return BrowserResource(
                resource=ProfileResourceRef.from_qualified(resource),
                kind="persistent",
                description="Personal browser",
                availability="available",
                headless=False,
                process_owned=True,
            )

        async def open_resource(self, *_args, **_kwargs) -> BrowserSession:
            page = BrowserPage(
                session_id=session_id,
                page_id=page_id,
                selected=True,
                url="about:blank",
                navigation_generation=0,
            )
            return BrowserSession(
                session_id=session_id,
                resource=ProfileResourceRef(profile="personal", name="main"),
                mode="owned_persistent",
                headless=False,
                selected_page_id=page_id,
                pages=(page,),
            )

        async def close_session(self, _session_id: str) -> BrowserSessionClosed:
            raise AssertionError("cancelled setup should close through service ownership")

        async def aclose(self) -> None:
            self.closed.set()

    class BlockingRenderer:
        entered = asyncio.Event()

        def render_status(self, *_args, **_kwargs) -> None:
            return None

        async def read_line(self, _prompt: str) -> str:
            self.entered.set()
            await asyncio.Future()
            raise AssertionError("cancelled input unexpectedly resumed")

    service = SetupService()
    renderer = BlockingRenderer()

    async def resource_service(_resource: str) -> SetupService:
        return service

    monkeypatch.setattr("ricky.interfaces.cli.browser._resource_service", resource_service)
    setup = asyncio.create_task(
        _setup_resource("personal/main", renderer)  # type: ignore[arg-type]
    )
    await asyncio.wait_for(renderer.entered.wait(), timeout=1)

    setup.cancel()
    try:
        await asyncio.wait_for(setup, timeout=1)
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancelled setup did not propagate cancellation")

    assert service.closed.is_set()
