"""Provider-free browser installation, resource, and readiness commands."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import typer

from ricky.browser.install import (
    BrowserInstallationError,
    browser_status,
    install_chromium,
)
from ricky.browser.service import BrowserService
from ricky.browser.types import (
    BrowserError,
    BrowserFailure,
    BrowserInstallStatus,
    BrowserResourceList,
)
from ricky.config import RickySettings, load_settings
from ricky.interfaces.cli.render import CliRenderer
from ricky.profiles import ProfileResourceRef

_PROFILE_OPTION = typer.Option(
    None,
    "--profile",
    help="Primary profile; defaults to the configured default profile.",
)
_ACCESS_PROFILE_OPTION = typer.Option(
    None,
    "--access-profile",
    help="Additional accessible profile; repeatable.",
)
_RESOURCE_ARGUMENT = typer.Argument(..., help="Qualified browser resource as profile/name.")


def register_browser_commands(browser_app: typer.Typer) -> None:
    """Attach browser installation commands to the main CLI composition root."""

    @browser_app.command("install")
    def browser_install() -> None:
        """Explicitly install the Chromium build locked to Playwright."""

        settings = load_settings()
        renderer = CliRenderer()
        renderer.render_status("Installing the locked Playwright Chromium build…")
        status = _run_browser_command(lambda: install_chromium(settings), renderer)
        renderer.render_status(_render_install(status), style="green")

    @browser_app.command("status")
    def browser_readiness() -> None:
        """Inspect Chromium configuration and readiness without launching it."""

        settings = load_settings()
        renderer = CliRenderer()
        status = _run_browser_command(lambda: browser_status(settings), renderer)
        renderer.render_status(
            _render_status(status, settings),
            style="green" if status.ready else "yellow",
        )
        if not status.ready:
            raise typer.Exit(1)

    @browser_app.command("resources")
    def browser_resources(
        profile: str | None = _PROFILE_OPTION,
        access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
    ) -> None:
        """List safe configured browser-resource metadata in the selected scope."""

        renderer = CliRenderer()
        resources = _run_resource_command(
            lambda: _list_resources(profile, access_profiles or []),
            renderer,
        )
        renderer.render_status(_render_resources(resources), style="")

    @browser_app.command("setup")
    def browser_setup(resource: str = _RESOURCE_ARGUMENT) -> None:
        """Open a persistent resource headed for one local setup session."""

        renderer = CliRenderer()
        _run_resource_command(lambda: _setup_resource(resource, renderer), renderer)

    @browser_app.command("check")
    def browser_check(resource: str = _RESOURCE_ARGUMENT) -> None:
        """Check that one configured browser resource can open and close."""

        renderer = CliRenderer()
        _run_resource_command(lambda: _check_resource(resource), renderer)
        renderer.render_status(f"Browser resource {resource} is available.", style="green")

    @browser_app.command("reset")
    def browser_reset(
        resource: str = _RESOURCE_ARGUMENT,
        yes: bool = typer.Option(False, "--yes", help="Skip the destructive confirmation."),
    ) -> None:
        """Delete one idle Ricky-owned persistent Chromium profile."""

        renderer = CliRenderer()
        if not yes and not typer.confirm(
            f"Delete all persistent browser state for {resource}?",
            default=False,
        ):
            renderer.render_status("Browser resource reset cancelled.", style="yellow")
            return
        _run_resource_command(lambda: _reset_resource(resource), renderer)
        renderer.render_status(f"Reset browser resource {resource}.", style="green")


def _run_browser_command(
    operation: Callable[[], Coroutine[Any, Any, BrowserInstallStatus]],
    renderer: CliRenderer,
) -> BrowserInstallStatus:
    try:
        return asyncio.run(operation())
    except (BrowserInstallationError, OSError, ValueError) as exc:
        renderer.render_error(f"Browser error: {exc}")
        raise typer.Exit(1) from exc


def _run_resource_command(
    operation: Callable[[], Coroutine[Any, Any, Any]],
    renderer: CliRenderer,
) -> Any:
    try:
        return asyncio.run(operation())
    except (BrowserError, BrowserInstallationError, OSError, ValueError) as exc:
        renderer.render_error(f"Browser error: {exc}")
        raise typer.Exit(1) from exc


async def _list_resources(
    profile: str | None,
    access_profiles: list[str],
) -> BrowserResourceList:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    service = await BrowserService.create(settings, scope=scope)
    try:
        return await service.resources()
    finally:
        await service.aclose()


async def _setup_resource(resource: str, renderer: CliRenderer) -> None:
    service = await _resource_service(resource)
    try:
        configured = service.resource(resource)
        if configured.kind != "persistent":
            raise BrowserError(
                BrowserFailure(
                    code="resource_kind_mismatch",
                    message="browser setup is only available for persistent browser resources",
                )
            )
        session = await service.open_resource(resource, headless=False, start_blank=True)
        renderer.render_status(
            f"Opened headed browser resource {resource}. Complete local setup in Chromium.",
            style="green",
        )
        await renderer.read_line("Press Enter when setup is complete: ")
        await service.close_session(session.session_id)
    finally:
        await service.aclose()


async def _check_resource(resource: str) -> None:
    service = await _resource_service(resource)
    try:
        session = await service.open_resource(resource)
        await service.close_session(session.session_id)
    finally:
        await service.aclose()


async def _reset_resource(resource: str) -> None:
    service = await _resource_service(resource)
    try:
        await service.reset_resource(resource)
    finally:
        await service.aclose()


async def _resource_service(resource: str) -> BrowserService:
    ref = ProfileResourceRef.from_qualified(resource)
    settings = load_settings()
    scope = settings.resolve_profile_scope(ref.profile)
    service = await BrowserService.create(settings, scope=scope)
    return service


def _render_install(status: BrowserInstallStatus) -> str:
    lines = [
        "Chromium is installed and ready.",
        f"binary directory: {status.install_dir}",
    ]
    if not status.enabled:
        lines.append("browser control remains disabled; set browser.enabled = true to use it")
    return "\n".join(lines)


def _render_status(status: BrowserInstallStatus, settings: RickySettings) -> str:
    lines = [
        f"browser control: {'enabled' if status.enabled else 'disabled'}",
        f"owned browser: {settings.browser.browser_kind}",
        f"interactive mode: {'headless' if settings.browser.headless else 'headed'}",
        f"binary directory: {status.install_dir}",
        f"Chromium: {'ready' if status.ready else 'not installed'}",
    ]
    if not status.ready:
        lines.append(f"repair: {status.repair_command}")
    return "\n".join(lines)


def _render_resources(resources: BrowserResourceList) -> str:
    if not resources.resources:
        return "No configured browser resources are available in this profile scope."
    return "\n".join(
        (
            f"{item.resource.qualified}  {item.kind}  {item.availability}  "
            f"process={'owned' if item.process_owned else 'external'}  {item.description}"
        )
        for item in resources.resources
    )
