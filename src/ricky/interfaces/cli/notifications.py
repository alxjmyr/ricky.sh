"""Durable notification inspection and reconciliation commands."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import typer

from ricky.config import load_settings
from ricky.interfaces.cli.render import CliRenderer
from ricky.notifications import NotificationStore, NotificationStoreError
from ricky.notifications.types import NotificationRecord, OutboxStatus, ResolutionDisposition

_NOTIFICATION_STATUS_OPTION = typer.Option(None, "--status")

_NOTIFICATION_LIMIT_OPTION = typer.Option(50, min=1, max=1_000)

_NOTIFICATION_RESOLUTION_OPTION = typer.Option(..., "--as")

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


def notification_list(
    status: OutboxStatus | None = _NOTIFICATION_STATUS_OPTION,
    limit: int = _NOTIFICATION_LIMIT_OPTION,
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """List durable notification requests without constructing a provider."""

    _run_notification_command(
        lambda renderer: _notification_list(status, limit, profile, access_profiles or [], renderer)
    )


def notification_show(
    notification_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Show one immutable request and its current outbox state."""

    _run_notification_command(
        lambda renderer: _notification_show(
            notification_id, profile, access_profiles or [], renderer
        )
    )


def notification_retry(
    outbox_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Requeue one delivery confirmed not to have been performed."""

    _run_notification_command(
        lambda renderer: _notification_retry(outbox_id, profile, access_profiles or [], renderer)
    )


def notification_resolve(
    outbox_id: str = typer.Argument(...),
    resolution: ResolutionDisposition = _NOTIFICATION_RESOLUTION_OPTION,
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Record the user resolution of an ambiguous delivery."""

    _run_notification_command(
        lambda renderer: _notification_resolve(
            outbox_id, resolution, profile, access_profiles or [], renderer
        )
    )


def notification_cancel(
    outbox_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Cancel a notification that has not been delivered."""

    _run_notification_command(
        lambda renderer: _notification_cancel(outbox_id, profile, access_profiles or [], renderer)
    )


async def _notification_list(
    status: OutboxStatus | None,
    limit: int,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = NotificationStore(settings)
    await store.initialize()
    records = await store.list(scope=scope, status=status, limit=limit)
    if not records:
        renderer.render_status("No notifications found.", style="yellow")
        return
    renderer.render_status("\n\n".join(_render_notification(item) for item in records), style="")


async def _notification_show(
    notification_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = NotificationStore(settings)
    await store.initialize()
    renderer.render_status(
        _render_notification(await store.get(notification_id, scope=scope)), style=""
    )


async def _notification_retry(
    outbox_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = NotificationStore(settings)
    await store.initialize()
    entry = await store.retry(outbox_id, scope=scope)
    renderer.render_status(f"{entry.id}: {entry.status}", style="green")


async def _notification_resolve(
    outbox_id: str,
    resolution: ResolutionDisposition,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = NotificationStore(settings)
    await store.initialize()
    entry = await store.resolve(
        outbox_id,
        scope=scope,
        disposition=resolution,
        actor="ricky_notification_cli",
    )
    renderer.render_status(f"{entry.id}: {entry.status}", style="green")


async def _notification_cancel(
    outbox_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = NotificationStore(settings)
    await store.initialize()
    entry = await store.cancel(outbox_id, scope=scope)
    renderer.render_status(f"{entry.id}: {entry.status}", style="green")


def _render_notification(record: NotificationRecord) -> str:
    request = record.request
    outbox = record.outbox
    lines = [
        f"notification: {request.id}",
        f"outbox: {outbox.id}",
        f"status: {outbox.status}",
        f"route: {request.route}",
        f"urgency: {request.urgency}",
        f"source: {request.source_kind}/{request.source_id}",
        f"profiles: {', '.join(request.profile_label.required_profiles)}",
        f"created: {request.created_at.isoformat()}",
    ]
    if request.title is not None:
        lines.append(f"title: {request.title}")
    lines.append(f"body: {request.body}")
    if request.correlations:
        lines.append("correlations:")
        lines.extend(
            f"  - {item.kind}/{item.id} revision={item.revision} "
            "profiles=" + ",".join(item.profile_label.required_profiles)
            for item in request.correlations
        )
    if outbox.error is not None:
        lines.append(f"error: {outbox.error}")
    if outbox.platform_message_id is not None:
        lines.append(f"platform_message_id: {outbox.platform_message_id}")
    return "\n".join(lines)


def _run_notification_command(
    factory: Callable[[CliRenderer], Coroutine[Any, Any, None]],
) -> None:
    renderer = CliRenderer()
    try:
        asyncio.run(factory(renderer))
    except (NotificationStoreError, ValueError, OSError) as exc:
        renderer.render_error(f"Notification error: {exc}")
        raise typer.Exit(1) from exc


def register_notification_commands(notification_app: typer.Typer) -> None:
    """Register the commands on the application composition root."""

    notification_app.command("list")(notification_list)
    notification_app.command("show")(notification_show)
    notification_app.command("retry")(notification_retry)
    notification_app.command("resolve")(notification_resolve)
    notification_app.command("cancel")(notification_cancel)
