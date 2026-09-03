"""Provider-free session inspection and bounded persistent resume commands."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any
from uuid import uuid4

import typer

from ricky.config import load_settings
from ricky.interfaces.cli.render import CliRenderer
from ricky.profiles import ProfileScope
from ricky.sessions import (
    PersistentTurnError,
    PersistentTurnService,
    SessionStatus,
    SessionStore,
    SessionStoreError,
    StoredSession,
)

_STATUS_OPTION = typer.Option(None, help="Filter by session status.")
_LIMIT_OPTION = typer.Option(50, min=1, max=1_000)
_SESSION_ID_ARGUMENT = typer.Argument(..., help="Persistent session id.")
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


async def _open_store(
    profile: str | None,
    access_profiles: list[str],
) -> tuple[SessionStore, ProfileScope]:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = SessionStore(settings)
    await store.initialize()
    return store, scope


async def _list_sessions(
    status: SessionStatus | None,
    limit: int,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    store, scope = await _open_store(profile, access_profiles)
    sessions = await store.list(scope=scope, status=status, limit=limit)
    if not sessions:
        renderer.render_status("No persistent sessions found.", style="yellow")
        return
    renderer.render_status("\n".join(_session_line(item) for item in sessions), style="")


async def _show_session(
    session_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    store, scope = await _open_store(profile, access_profiles)
    stored = await store.get(session_id, scope=scope)
    turns = await store.turns(
        session_id,
        scope=scope,
        limit=min(20, store.settings.turn_retention),
    )
    turn_lines = [
        f"  {turn.id}  {turn.status}  base={turn.base_revision}  {turn.started_at.isoformat()}"
        for turn in turns
    ]
    details = [
        _session_line(stored),
        f"created: {stored.created_at.isoformat()}",
        f"provider/model: {stored.session.provider}/{stored.session.model}",
        f"profiles: {','.join(stored.session.profile_scope.profiles)}",
        f"history messages: {len(stored.session.history)}",
        f"tasks: {len(stored.session.tasks)}",
        f"artifacts: {len(stored.session.artifacts)}",
        f"checkpoints: {len(stored.session.checkpoints)}",
        "recent turns:",
        *(turn_lines or ["  none"]),
    ]
    renderer.render_status("\n".join(details), style="")


async def _archive_session(
    session_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    store, scope = await _open_store(profile, access_profiles)
    current = await store.get(session_id, scope=scope)
    archived = await store.archive(session_id, current.revision, scope=scope)
    renderer.render_status(
        f"Archived {archived.session.id} at revision {archived.revision}.",
        style="green",
    )


async def _resume_session(
    session_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = SessionStore(settings)
    await store.initialize()
    stored = await store.get(session_id, scope=scope)
    renderer.render_welcome(stored.session)
    renderer.render_status(
        "Persistent mode: each message opens and closes one bounded runtime. "
        "Commands: /debug, /quit",
        style="dim",
    )
    service = PersistentTurnService(
        settings,
        store,
        profile_scope=scope,
        runtime_kwargs={
            "permission_responder": renderer.request_permission,
            "approval_responder": renderer.request_workflow_approval,
        },
    )
    owner = f"session-cli-{uuid4().hex}"
    while True:
        try:
            user_input = (await renderer.read_user_input()).strip()
        except EOFError:
            renderer.render_status("Exiting.")
            return
        if not user_input:
            continue
        if user_input in {"/quit", "/exit", "/q"}:
            renderer.render_status("Exiting.")
            return
        if user_input == "/debug":
            renderer.debug = not renderer.debug
            renderer.render_status(f"Debug {'on' if renderer.debug else 'off'}.")
            continue
        if user_input.startswith("/"):
            renderer.render_status(
                "Persistent resume currently supports /debug and /quit.",
                style="yellow",
            )
            continue
        try:
            await service.run_turn(
                session_id,
                user_input,
                owner=owner,
                event_sink=renderer.render_event,
            )
        except PersistentTurnError as exc:
            renderer.render_error(f"Turn failed: {exc}")
            if (await store.get(session_id, scope=scope)).status != "active":
                return


def _session_line(stored: StoredSession) -> str:
    return (
        f"{stored.session.id}  {stored.status}  rev={stored.revision}  "
        f"updated={stored.updated_at.isoformat()}  "
        f"{stored.session.provider}/{stored.session.model}"
    )


def _run(factory: Callable[[CliRenderer], Coroutine[Any, Any, None]]) -> None:
    renderer = CliRenderer()
    try:
        asyncio.run(factory(renderer))
    except (SessionStoreError, ValueError, OSError) as exc:
        renderer.render_error(f"Session error: {exc}")
        raise typer.Exit(1) from exc


def session_list(
    status: SessionStatus | None = _STATUS_OPTION,
    limit: int = _LIMIT_OPTION,
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """List stored sessions without constructing a provider."""

    _run(lambda renderer: _list_sessions(status, limit, profile, access_profiles or [], renderer))


def session_show(
    session_id: str = _SESSION_ID_ARGUMENT,
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Inspect one stored session and its recent turns without a provider."""

    _run(lambda renderer: _show_session(session_id, profile, access_profiles or [], renderer))


def session_archive(
    session_id: str = _SESSION_ID_ARGUMENT,
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Archive one session at its current revision without a provider."""

    _run(lambda renderer: _archive_session(session_id, profile, access_profiles or [], renderer))


def session_resume(
    session_id: str = _SESSION_ID_ARGUMENT,
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Resume a conversation with one fresh bounded runtime per message."""

    _run(lambda renderer: _resume_session(session_id, profile, access_profiles or [], renderer))


def register_session_commands(app: typer.Typer) -> None:
    """Register the persistent-session CLI surface on its composition-root group."""

    app.command("list")(session_list)
    app.command("show")(session_show)
    app.command("archive")(session_archive)
    app.command("resume")(session_resume)
