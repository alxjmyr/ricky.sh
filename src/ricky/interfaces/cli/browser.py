"""Provider-free Chrome resource and readiness commands."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import typer

from ricky.browser.chrome import ChromeDiscoveryError, browser_status
from ricky.browser.service import BrowserService
from ricky.browser.setup import manual_browser_setup
from ricky.browser.types import (
    BrowserError,
    BrowserResourceList,
    BrowserStatus,
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
    """Attach browser resource and readiness commands to the main CLI composition root."""

    @browser_app.command("status")
    def browser_readiness() -> None:
        """Inspect Chrome executable availability without opening a browser."""

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
        """Delete one idle Ricky-owned persistent Chrome profile."""

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
    operation: Callable[[], Coroutine[Any, Any, BrowserStatus]],
    renderer: CliRenderer,
) -> BrowserStatus:
    try:
        return asyncio.run(operation())
    except (ChromeDiscoveryError, OSError, ValueError) as exc:
        renderer.render_error(f"Browser error: {exc}")
        raise typer.Exit(1) from exc


def _run_resource_command(
    operation: Callable[[], Coroutine[Any, Any, Any]],
    renderer: CliRenderer,
) -> Any:
    try:
        return asyncio.run(operation())
    except (BrowserError, ChromeDiscoveryError, OSError, ValueError) as exc:
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
    ref = ProfileResourceRef.from_qualified(resource)
    settings = load_settings()
    scope = settings.resolve_profile_scope(ref.profile)
    async with manual_browser_setup(settings, scope=scope, resource=ref) as process:
        renderer.render_status(
            f"Opened Chrome resource {resource}. "
            "Complete local setup, then close Chrome normally to save your sign-in. "
            "Ctrl+C cancels setup; recent sign-ins may not be saved.",
            style="green",
        )
        await process.wait()


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


def _render_status(status: BrowserStatus, settings: RickySettings) -> str:
    lines = [
        f"browser control: {'enabled' if status.enabled else 'disabled'}",
        f"interactive mode: {'headless' if settings.browser.headless else 'headed'}",
        f"Google Chrome Stable: {'available' if status.ready else 'unavailable'}",
        f"Playwright: {status.playwright_version}",
        "Readiness checks executable identity; use browser check to test launch capability.",
    ]
    if status.executable:
        lines.append(f"executable: {status.executable}")
    if status.version:
        lines.append(f"Chrome version: {status.version}")
    if status.diagnostic:
        lines.append(status.diagnostic)
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
