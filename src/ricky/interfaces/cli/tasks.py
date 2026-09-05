"""Durable task inspection and lifecycle commands."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

import typer

from ricky.config import load_settings
from ricky.durable_tasks.artifacts import TaskArtifactStore
from ricky.durable_tasks.render import (
    render_activity,
    render_artifacts,
    render_task,
    render_task_list,
)
from ricky.durable_tasks.store import DurableTaskStore, TaskStoreError
from ricky.durable_tasks.types import TaskExecutionMode, TaskSearchQuery, canonicalize_task_tags
from ricky.interfaces.cli.render import CliRenderer
from ricky.profiles import ProfileName

_TASK_PROFILE_OPTION = typer.Option(
    None,
    "--profile",
    help="Owning task profile; defaults to the configured default profile.",
)

_TASK_INCLUDE_CLOSED_OPTION = typer.Option(
    False, "--include-closed", help="Include completed and cancelled tasks."
)

_TASK_LIMIT_OPTION = typer.Option(50, min=1, max=500)

_TASK_MODE_OPTION = typer.Option("user", "--mode")

_TASK_PRIORITY_OPTION = typer.Option(0, min=-100, max=100)

_TASK_DUE_OPTION = typer.Option(None, "--due-at")

_TASK_TAG_OPTION = typer.Option(None, "--tag")

_TASK_TAG_ADD_OPTION = typer.Option(None, "--add")

_TASK_TAG_REMOVE_OPTION = typer.Option(None, "--remove")


def task_list(
    profile: ProfileName | None = _TASK_PROFILE_OPTION,
    include_closed: bool = _TASK_INCLUDE_CLOSED_OPTION,
    limit: int = _TASK_LIMIT_OPTION,
) -> None:
    """List durable tasks without invoking a model provider."""

    _run_task_command(lambda renderer: _task_list(profile, include_closed, limit, renderer))


def task_show(
    task_id: str = typer.Argument(..., help="Durable task id."),
    profile: ProfileName | None = _TASK_PROFILE_OPTION,
) -> None:
    """Show one durable task."""

    _run_task_command(lambda renderer: _task_show(task_id, profile, renderer))


def task_activity(
    task_id: str = typer.Argument(..., help="Durable task id."),
    profile: ProfileName | None = _TASK_PROFILE_OPTION,
    limit: int = _TASK_LIMIT_OPTION,
) -> None:
    """Show append-only activity for one durable task."""

    _run_task_command(lambda renderer: _task_activity(task_id, profile, limit, renderer))


def task_artifacts(
    task_id: str = typer.Argument(..., help="Durable task id."),
    profile: ProfileName | None = _TASK_PROFILE_OPTION,
) -> None:
    """List one durable task's human-readable artifact files."""

    _run_task_command(lambda renderer: _task_artifacts(task_id, profile, renderer))


def task_create(
    title: str = typer.Option(..., "--title"),
    objective: str = typer.Option(..., "--objective"),
    closure_criteria: str = typer.Option(..., "--closure-criteria"),
    execution_mode: TaskExecutionMode = _TASK_MODE_OPTION,
    profile: ProfileName | None = _TASK_PROFILE_OPTION,
    priority: int = _TASK_PRIORITY_OPTION,
    due_at: datetime | None = _TASK_DUE_OPTION,
    tags: list[str] | None = _TASK_TAG_OPTION,
) -> None:
    """Create a durable task as a deterministic user command."""

    _run_task_command(
        lambda renderer: _task_create(
            title,
            objective,
            closure_criteria,
            execution_mode,
            profile,
            priority,
            due_at,
            tags or [],
            renderer,
        )
    )


def task_tag(
    task_id: str = typer.Argument(..., help="Durable task id."),
    add: list[str] | None = _TASK_TAG_ADD_OPTION,
    remove: list[str] | None = _TASK_TAG_REMOVE_OPTION,
    profile: ProfileName | None = _TASK_PROFILE_OPTION,
) -> None:
    """Add or remove exact open-vocabulary tags through the normal lease path."""

    _run_task_command(
        lambda renderer: _task_tag(task_id, profile, add or [], remove or [], renderer)
    )


def task_complete(
    task_id: str = typer.Argument(..., help="Durable task id."),
    summary: str = typer.Option(..., "--summary", help="How closure criteria were met."),
    profile: ProfileName | None = _TASK_PROFILE_OPTION,
) -> None:
    """Claim and complete one task unless another live lease exists."""

    _run_task_command(
        lambda renderer: _task_close(task_id, profile, "completed", summary, renderer)
    )


def task_cancel(
    task_id: str = typer.Argument(..., help="Durable task id."),
    reason: str = typer.Option(..., "--reason"),
    profile: ProfileName | None = _TASK_PROFILE_OPTION,
) -> None:
    """Claim and cancel one task without deleting its history."""

    _run_task_command(lambda renderer: _task_close(task_id, profile, "cancelled", reason, renderer))


def task_reopen(
    task_id: str = typer.Argument(..., help="Durable task id."),
    reason: str = typer.Option(..., "--reason"),
    profile: ProfileName | None = _TASK_PROFILE_OPTION,
) -> None:
    """Reopen a completed or cancelled durable task."""

    _run_task_command(lambda renderer: _task_reopen(task_id, profile, reason, renderer))


async def _task_list(
    profile: ProfileName | None,
    include_closed: bool,
    limit: int,
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    selected = settings.resolve_profile_scope(profile).primary
    store = await DurableTaskStore.create(settings, profile=selected)
    tasks = await store.search(TaskSearchQuery(include_closed=include_closed, limit=limit))
    renderer.render_status(render_task_list(tasks), style="")


async def _task_show(task_id: str, profile: ProfileName | None, renderer: CliRenderer) -> None:
    settings = load_settings()
    selected = settings.resolve_profile_scope(profile).primary
    store = await DurableTaskStore.create(settings, profile=selected)
    task = await store.get_task(task_id)
    renderer.render_status(render_task(task), style="")


async def _task_activity(
    task_id: str,
    profile: ProfileName | None,
    limit: int,
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    selected = settings.resolve_profile_scope(profile).primary
    store = await DurableTaskStore.create(settings, profile=selected)
    activity = await store.activities(task_id, limit=limit)
    renderer.render_status(render_activity(activity), style="")


async def _task_artifacts(task_id: str, profile: ProfileName | None, renderer: CliRenderer) -> None:
    settings = load_settings()
    selected = settings.resolve_profile_scope(profile).primary
    store = await DurableTaskStore.create(settings, profile=selected)
    entries = await TaskArtifactStore(store).list(task_id)
    renderer.render_status(render_artifacts(entries), style="")


async def _task_create(
    title: str,
    objective: str,
    closure_criteria: str,
    execution_mode: TaskExecutionMode,
    profile: ProfileName | None,
    priority: int,
    due_at: datetime | None,
    tags: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    selected = settings.resolve_profile_scope(profile).primary
    store = await DurableTaskStore.create(settings, profile=selected)
    task = await store.create_task(
        title=title,
        objective=objective,
        closure_criteria=closure_criteria,
        execution_mode=execution_mode,
        priority=priority,
        due_at=due_at,
        tags=tags,
        authority="deterministic_user_command",
        executor_id="ricky_task_cli",
    )
    renderer.render_status(render_task(task), style="green")


async def _task_tag(
    task_id: str,
    profile: ProfileName | None,
    add: list[str],
    remove: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    selected = settings.resolve_profile_scope(profile).primary
    store = await DurableTaskStore.create(settings, profile=selected)
    session_id = f"cli_{uuid4().hex}"
    claimed = await store.claim(
        task_id,
        holder_session_id=session_id,
        authority="deterministic_user_command",
        executor_id="ricky_task_cli",
    )
    assert claimed.lease is not None
    try:
        additions = set(canonicalize_task_tags(add))
        removals = set(canonicalize_task_tags(remove))
        tags = sorted((set(claimed.tags) | additions) - removals)
        updated = await store.update_tags(
            task_id,
            tags=tags,
            lease=claimed.lease,
            expected_revision=claimed.revision,
            authority="deterministic_user_command",
            executor_id="ricky_task_cli",
        )
        if updated.lease is not None:
            updated = await store.release(
                task_id,
                lease=updated.lease,
                expected_revision=updated.revision,
                authority="deterministic_user_command",
                executor_id="ricky_task_cli",
                summary="Tag edit complete",
            )
    except BaseException:
        await store.release_session_leases(session_id)
        raise
    renderer.render_status(render_task(updated), style="green")


async def _task_close(
    task_id: str,
    profile: ProfileName | None,
    status: Literal["completed", "cancelled"],
    summary: str,
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    selected = settings.resolve_profile_scope(profile).primary
    store = await DurableTaskStore.create(settings, profile=selected)
    session_id = f"cli_{uuid4().hex}"
    claimed = await store.claim(
        task_id,
        holder_session_id=session_id,
        authority="deterministic_user_command",
        executor_id="ricky_task_cli",
    )
    assert claimed.lease is not None
    try:
        if status == "completed":
            task = await store.complete(
                task_id,
                lease=claimed.lease,
                expected_revision=claimed.revision,
                completion_summary=summary,
                authority="deterministic_user_command",
                executor_id="ricky_task_cli",
            )
        else:
            task = await store.cancel(
                task_id,
                lease=claimed.lease,
                expected_revision=claimed.revision,
                reason=summary,
                authority="deterministic_user_command",
                executor_id="ricky_task_cli",
            )
    except BaseException:
        await store.release_session_leases(session_id)
        raise
    renderer.render_status(render_task(task), style="green")


async def _task_reopen(
    task_id: str,
    profile: ProfileName | None,
    reason: str,
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    selected = settings.resolve_profile_scope(profile).primary
    store = await DurableTaskStore.create(settings, profile=selected)
    task = await store.reopen(
        task_id,
        reason=reason,
        authority="deterministic_user_command",
        executor_id="ricky_task_cli",
    )
    renderer.render_status(render_task(task), style="green")


def _run_task_command(
    factory: Callable[[CliRenderer], Coroutine[Any, Any, None]],
) -> None:
    renderer = CliRenderer()
    try:
        asyncio.run(factory(renderer))
    except (TaskStoreError, ValueError, OSError) as exc:
        renderer.render_error(f"Task error: {exc}")
        raise typer.Exit(1) from exc


def register_task_commands(task_app: typer.Typer) -> None:
    """Register the commands on the application composition root."""

    task_app.command("list")(task_list)
    task_app.command("show")(task_show)
    task_app.command("activity")(task_activity)
    task_app.command("artifacts")(task_artifacts)
    task_app.command("create")(task_create)
    task_app.command("tag")(task_tag)
    task_app.command("complete")(task_complete)
    task_app.command("cancel")(task_cancel)
    task_app.command("reopen")(task_reopen)
