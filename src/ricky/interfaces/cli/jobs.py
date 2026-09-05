"""Bounded job execution and verified schedule commands."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, Literal

import typer

from ricky.agent.events import TextDeltaEvent
from ricky.config import RickySettings, find_project_root, load_settings
from ricky.interfaces.cli.render import CliRenderer
from ricky.jobs.registry import JobRegistry
from ricky.jobs.render import render_job, render_run
from ricky.jobs.runner import JobConfigurationError, JobRunner
from ricky.jobs.store import JobRunStore, JobStoreError
from ricky.llm import ProviderError
from ricky.profiles import ProfileScope
from ricky.schedules.cron import CronError
from ricky.schedules.render import (
    render_approval,
    render_doctor,
    render_schedule,
    render_schedule_list,
    render_sync,
)
from ricky.schedules.service import ScheduleService, ScheduleServiceError
from ricky.schedules.store import ScheduleStoreError

_SCHEDULE_PROJECT_OPTION = typer.Option(None, "--project", help="Explicit project root.")

_SCHEDULE_INVOKE_PROJECT_OPTION = typer.Option(
    ..., "--project", help="Approved absolute project root."
)

_JOB_TOOLS_OPTION = typer.Option([], "--tool", help="Read-only tool; repeatable.")

_JOB_DRY_RUN_OPTION = typer.Option(False, "--dry-run")

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


def job_list(
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """List discovered job bundles without constructing a provider."""

    _run_job_command(lambda renderer: _job_list(profile, access_profiles or [], renderer))


def job_validate(
    name: str | None = typer.Argument(None, help="Job name; omit to validate all."),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Validate job shapes and current-machine read-only tool availability."""

    _run_job_command(lambda renderer: _job_validate(name, profile, access_profiles or [], renderer))


def job_show(
    name: str = typer.Argument(..., help="Job name."),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Show one resolved job and its current tool availability."""

    _run_job_command(lambda renderer: _job_show(name, profile, access_profiles or [], renderer))


def job_run(
    name: str = typer.Argument(..., help="Job name."),
    dry_run: bool = _JOB_DRY_RUN_OPTION,
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Run one named bounded recurring job without prompting."""

    _run_job_command(
        lambda renderer: _job_run(name, dry_run, profile, access_profiles or [], renderer)
    )


def job_once(
    goal: str = typer.Argument(..., help="One bounded read-only goal."),
    tools: list[str] = _JOB_TOOLS_OPTION,
    provider: str | None = typer.Option(None, "--provider", "-p"),
    model: str | None = typer.Option(None, "--model", "-m"),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Run one ad-hoc goal with no persistent job identity or lock."""

    _run_job_command(
        lambda renderer: _job_once(
            goal,
            tools,
            provider,
            model,
            profile,
            access_profiles or [],
            renderer,
        )
    )


def job_history(
    job_name: str | None = typer.Option(None, "--job", help="Filter by job name."),
    limit: int = typer.Option(50, min=1, max=500),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """List persisted launch attempts without constructing a provider."""

    _run_job_command(
        lambda renderer: _job_history(job_name, limit, profile, access_profiles or [], renderer)
    )


def job_report(
    run_id: str = typer.Argument(..., help="Job run id."),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Show one persisted job run report."""

    _run_job_command(lambda renderer: _job_report(run_id, profile, access_profiles or [], renderer))


def job_action_show(
    action_id: str = typer.Argument(..., help="Job action id."),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Inspect one guarded external action without constructing a provider."""

    _run_job_command(
        lambda renderer: _job_action_show(action_id, profile, access_profiles or [], renderer)
    )


def job_action_resolve(
    action_id: str = typer.Argument(..., help="In-doubt job action id."),
    performed: bool = typer.Option(False, "--performed"),
    not_performed: bool = typer.Option(False, "--not-performed"),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Append a user reconciliation after checking the external system."""

    if performed == not_performed:
        raise typer.BadParameter("choose exactly one of --performed or --not-performed")
    disposition = "performed" if performed else "not_performed"
    _run_job_command(
        lambda renderer: _job_action_resolve(
            action_id,
            disposition,
            profile,
            access_profiles or [],
            renderer,
        )
    )


def schedule_list(
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """List desired schedules and current revision/approval state."""

    _run_schedule_command(lambda renderer: _schedule_list(profile, access_profiles or [], renderer))


def schedule_show(
    schedule_id: str = typer.Argument(..., help="Opaque schedule id."),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Inspect one desired schedule and its current revision/approval state."""

    _run_schedule_command(
        lambda renderer: _schedule_show(schedule_id, profile, access_profiles or [], renderer)
    )


def schedule_add(
    job_name: str = typer.Argument(..., help="Named job to launch."),
    cron: str = typer.Option(..., "--cron", help="Numeric five-field cron expression."),
    project: Path | None = _SCHEDULE_PROJECT_OPTION,
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Create and approve desired state; does not install it."""

    _run_schedule_command(
        lambda renderer: _schedule_add(
            job_name, cron, project, profile, access_profiles or [], renderer
        )
    )


def schedule_set(
    schedule_id: str = typer.Argument(..., help="Opaque schedule id."),
    cron: str = typer.Option(..., "--cron", help="Numeric five-field cron expression."),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Update desired timing; the new cron must then be approved."""

    _run_schedule_command(
        lambda renderer: _schedule_set(schedule_id, cron, profile, access_profiles or [], renderer)
    )


def schedule_enable(
    schedule_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Enable desired state; run sync separately to install it."""

    _run_schedule_command(
        lambda renderer: _schedule_enabled(
            schedule_id, True, profile, access_profiles or [], renderer
        )
    )


def schedule_disable(
    schedule_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Disable desired state; run sync separately to remove its cron line."""

    _run_schedule_command(
        lambda renderer: _schedule_enabled(
            schedule_id, False, profile, access_profiles or [], renderer
        )
    )


def schedule_remove(
    schedule_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Remove desired state only; run sync separately to reconcile cron."""

    _run_schedule_command(
        lambda renderer: _schedule_remove(schedule_id, profile, access_profiles or [], renderer)
    )


def schedule_approve(
    schedule_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Approve the current authority envelope, cron, and exact revision."""

    _run_schedule_command(
        lambda renderer: _schedule_approve(schedule_id, profile, access_profiles or [], renderer)
    )


def schedule_refresh(
    schedule_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Validate and pin a changed job revision that does not expand authority."""

    _run_schedule_command(
        lambda renderer: _schedule_refresh(schedule_id, profile, access_profiles or [], renderer)
    )


def schedule_sync(
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Reconcile desired schedules into Ricky's verified user-crontab block."""

    _run_schedule_command(lambda renderer: _schedule_sync(profile, access_profiles or [], renderer))


def schedule_doctor(
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Check desired/installed drift and launch prerequisites without a model."""

    _run_schedule_command(
        lambda renderer: _schedule_doctor(profile, access_profiles or [], renderer)
    )


def schedule_uninstall(
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Remove only Ricky's managed block, retaining desired schedules."""

    _run_schedule_command(
        lambda renderer: _schedule_uninstall(profile, access_profiles or [], renderer)
    )


def schedule_invoke(
    schedule_id: str = typer.Argument(...),
    project: Path = _SCHEDULE_INVOKE_PROJECT_OPTION,
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Backend trigger: validate one approved schedule and launch its job."""

    _run_schedule_command(
        lambda renderer: _schedule_invoke(
            schedule_id, project, profile, access_profiles or [], renderer
        )
    )


async def _job_list(
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    registry = JobRegistry(settings, profile_scope=profile_scope)
    jobs, errors = registry.discover()
    if jobs:
        renderer.render_status(
            "\n".join(
                f"{job.resource.qualified}: {job.spec.description} "
                f"[{job.spec.provider}/{job.spec.model}]"
                for job in jobs
            ),
            style="",
        )
    else:
        renderer.render_status("No jobs found.", style="yellow")
    for error in errors:
        renderer.render_error(f"{error.source_path}: {error.message}")


async def _job_validate(
    name: str | None,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    registry = JobRegistry(settings, profile_scope=profile_scope)
    if name is not None:
        jobs = [registry.load(name)]
        errors = []
    else:
        jobs, errors = registry.discover()
    failed = bool(errors)
    for error in errors:
        renderer.render_error(f"{error.source_path}: {error.message}")
    for job in jobs:
        try:
            await _validate_job_tools(settings, job, profile_scope)
        except ValueError as exc:
            failed = True
            renderer.render_error(f"{job.spec.name}: {exc}")
        else:
            renderer.render_status(f"{job.spec.name}: valid and available", style="green")
    if failed:
        raise typer.Exit(2)


async def _job_show(
    name: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    job = JobRegistry(settings, profile_scope=profile_scope).load(name)
    renderer.render_status(render_job(job), style="")
    try:
        await _validate_job_tools(settings, job, profile_scope)
    except ValueError as exc:
        renderer.render_status(f"tool availability: unavailable ({exc})", style="yellow")
    else:
        renderer.render_status("tool availability: available", style="green")


async def _validate_job_tools(
    settings: RickySettings,
    job: Any,
    profile_scope: ProfileScope,
) -> None:
    await JobRunner(settings).validate(job.resource.qualified, profile_scope=profile_scope)


async def _job_run(
    name: str,
    dry_run: bool,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    sink, saw_text = _job_event_renderer(renderer)
    run = await JobRunner(settings, event_sink=sink).run(
        name,
        profile_scope=profile_scope,
        dry_run=dry_run,
    )
    renderer.finish_stream()
    if not saw_text[0] and run.final_message:
        renderer.render_status(run.final_message, style="")
    if run.error:
        renderer.render_error(run.error)
    renderer.render_status(
        f"Job run {run.id}: {run.outcome}",
        style="green" if run.outcome == "succeeded" else "yellow",
    )
    _raise_for_job_outcome(run.outcome)


async def _job_once(
    goal: str,
    tools: list[str],
    provider: str | None,
    model: str | None,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    sink, saw_text = _job_event_renderer(renderer)
    run = await JobRunner(settings, event_sink=sink).once(
        goal,
        profile_scope=profile_scope,
        tools=tools,
        provider_name=provider,
        model=model,
    )
    renderer.finish_stream()
    if not saw_text[0] and run.final_message:
        renderer.render_status(run.final_message, style="")
    if run.error:
        renderer.render_error(run.error)
    renderer.render_status(
        f"Job run {run.id}: {run.outcome}",
        style="green" if run.outcome == "succeeded" else "yellow",
    )
    _raise_for_job_outcome(run.outcome)


async def _job_history(
    job_name: str | None,
    limit: int,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    if job_name is not None:
        job_name = (
            JobRegistry(settings, profile_scope=profile_scope).load(job_name).resource.qualified
        )
    store = JobRunStore(settings)
    await store.initialize()
    runs = await store.list(scope=profile_scope, job_name=job_name, limit=limit)
    if not runs:
        renderer.render_status("No job runs found.", style="yellow")
        return
    renderer.render_status(
        "\n".join(
            f"{run.id}  {run.started_at.isoformat()}  "
            f"{run.job_name or '(ad-hoc)'}  {run.outcome or 'running'}"
            for run in runs
        ),
        style="",
    )


async def _job_report(
    run_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = JobRunStore(settings)
    await store.initialize()
    renderer.render_status(render_run(await store.get(run_id, scope=profile_scope)), style="")


async def _job_action_show(
    action_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = JobRunStore(settings)
    await store.initialize()
    action = await store.get_action(action_id, scope=profile_scope)
    renderer.render_status(
        "\n".join(
            [
                f"action: {action.id}",
                f"job/run: {action.job_name}/{action.run_id}",
                f"status: {action.status}",
                f"operation: {action.operation}",
                f"target: {action.target}",
                f"occurrence: {action.occurrence}",
                f"summary: {action.summary}",
                f"provider reference: {action.provider_reference or '-'}",
            ]
        ),
        style="",
    )


async def _job_action_resolve(
    action_id: str,
    disposition: Literal["performed", "not_performed"],
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = JobRunStore(settings)
    await store.initialize()
    action, resolution = await store.reconcile_action(
        action_id,
        disposition,
        scope=profile_scope,
    )
    renderer.render_status(
        f"Resolved {action.id} as {action.status}; audit record {resolution.id} appended.",
        style="green",
    )


def _schedule_service(
    project: Path | None = None,
    *,
    profile: str | None = None,
    access_profiles: list[str] | tuple[str, ...] = (),
    all_profiles_by_default: bool = False,
) -> ScheduleService:
    root = find_project_root(project) if project is not None else find_project_root()
    settings = load_settings()
    if all_profiles_by_default and profile is None and not access_profiles:
        access_profiles = settings.profiles.enabled
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    return ScheduleService(
        settings,
        profile_scope=profile_scope,
        project_root=root,
    )


async def _schedule_list(
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    service = _schedule_service(
        profile=profile,
        access_profiles=access_profiles,
        all_profiles_by_default=True,
    )
    renderer.render_status(render_schedule_list(await service.list()), style="")


async def _schedule_show(
    schedule_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    inspection = await _schedule_service(profile=profile, access_profiles=access_profiles).show(
        schedule_id
    )
    renderer.render_status(render_schedule(inspection.schedule, inspection), style="")


async def _schedule_add(
    job_name: str,
    cron: str,
    project: Path | None,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    service = _schedule_service(project, profile=profile, access_profiles=access_profiles)
    schedule = await service.create(
        job_name,
        cron,
        profile_scope=service.profile_scope,
        project_root=project,
    )
    job = JobRegistry(
        service.settings,
        profile_scope=schedule.profile_scope,
    ).load(schedule.job_name)
    renderer.render_status(render_approval(schedule, job), style="yellow")
    renderer.render_status(
        f"Desired schedule created. It is not installed; run 'ricky schedule sync'.\n"
        f"{render_schedule(schedule)}",
        style="green",
    )


async def _schedule_set(
    schedule_id: str,
    cron: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    schedule = await _schedule_service(
        profile=profile, access_profiles=access_profiles
    ).update_cron(schedule_id, cron)
    renderer.render_status(
        f"Desired cron updated; approve the new timing, then sync to install it.\n"
        f"{render_schedule(schedule)}",
        style="green",
    )


async def _schedule_enabled(
    schedule_id: str,
    enabled: bool,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    schedule = await _schedule_service(
        profile=profile, access_profiles=access_profiles
    ).set_enabled(schedule_id, enabled)
    renderer.render_status(
        f"Desired schedule {'enabled' if enabled else 'disabled'}; installed cron is "
        f"unchanged until sync.\n{render_schedule(schedule)}",
        style="green",
    )


async def _schedule_remove(
    schedule_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    removed = await _schedule_service(profile=profile, access_profiles=access_profiles).remove(
        schedule_id
    )
    renderer.render_status(
        f"Removed desired schedule {removed.id}; installed cron is unchanged until sync.",
        style="yellow",
    )


async def _schedule_approve(
    schedule_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    service = _schedule_service(
        profile=profile,
        access_profiles=access_profiles,
        all_profiles_by_default=True,
    )
    before, schedule, changes = await service.approve(schedule_id)
    job = JobRegistry(
        service.settings,
        profile_scope=schedule.profile_scope,
    ).load(schedule.job_name)
    renderer.render_status(
        f"Previous state: {before.state}\n"
        f"spec digest: {before.schedule.approved_spec_digest} -> "
        f"{schedule.approved_spec_digest}\n"
        f"runtime revision: {before.schedule.approved_runtime_policy_digest} -> "
        f"{schedule.approved_runtime_policy_digest}\n"
        f"material changes:\n- " + "\n- ".join(changes) + f"\n\n{render_approval(schedule, job)}",
        style="yellow",
    )
    renderer.render_status(
        "Approval pins updated; installed cron is unchanged until sync.",
        style="green",
    )


async def _schedule_refresh(
    schedule_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    service = _schedule_service(
        profile=profile,
        access_profiles=access_profiles,
        all_profiles_by_default=True,
    )
    before, schedule, changes = await service.refresh(schedule_id)
    renderer.render_status(
        f"Previous state: {before.state}\n"
        f"spec digest: {before.schedule.approved_spec_digest} -> "
        f"{schedule.approved_spec_digest}\n"
        f"runtime revision: {before.schedule.approved_runtime_policy_digest} -> "
        f"{schedule.approved_runtime_policy_digest}\n"
        f"validated changes:\n- " + "\n- ".join(changes),
        style="yellow",
    )
    renderer.render_status(
        "Execution revision refreshed; installed cron is unchanged until sync.",
        style="green",
    )


async def _schedule_sync(
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    service = _schedule_service(
        profile=profile,
        access_profiles=access_profiles,
        all_profiles_by_default=True,
    )
    renderer.render_status(render_sync(await service.sync()), style="green")


async def _schedule_doctor(
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    report = await _schedule_service(
        profile=profile,
        access_profiles=access_profiles,
        all_profiles_by_default=True,
    ).doctor()
    renderer.render_status(render_doctor(report), style="green" if report.healthy else "yellow")
    if not report.healthy:
        raise typer.Exit(1)


async def _schedule_uninstall(
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    backup = await _schedule_service(
        profile=profile,
        access_profiles=access_profiles,
        all_profiles_by_default=True,
    ).uninstall()
    renderer.render_status(
        "Ricky's managed crontab block was removed and verified; schedules.toml was retained."
        + (f" Backup: {backup}" if backup else " No installed block was present."),
        style="green",
    )


async def _schedule_invoke(
    schedule_id: str,
    project: Path,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    root = project.expanduser().resolve()
    run = await _schedule_service(
        root,
        profile=profile,
        access_profiles=access_profiles,
    ).invoke(schedule_id)
    if run.final_message:
        renderer.render_status(run.final_message, style="")
    if run.error:
        renderer.render_error(run.error)
    renderer.render_status(
        f"Scheduled job run {run.id}: {run.outcome}",
        style="green" if run.outcome == "succeeded" else "yellow",
    )
    _raise_for_job_outcome(run.outcome)


def _raise_for_job_outcome(outcome: str | None) -> None:
    if outcome == "succeeded":
        return
    if outcome == "skipped_locked":
        raise typer.Exit(10)
    if outcome == "budget_exceeded":
        raise typer.Exit(30)
    if outcome == "approval_required":
        raise typer.Exit(20)
    raise typer.Exit(40)


def _job_event_renderer(renderer: CliRenderer):
    saw_text = [False]

    async def emit(event: Any) -> None:
        if isinstance(event, TextDeltaEvent):
            saw_text[0] = True
        renderer.render_event(event)

    return emit, saw_text


def _run_job_command(
    factory: Callable[[CliRenderer], Coroutine[Any, Any, None]],
) -> None:
    renderer = CliRenderer()
    try:
        asyncio.run(_run_job_with_signals(factory, renderer))
    except ProviderError as exc:
        renderer.render_error(f"Provider error: {exc}")
        raise typer.Exit(40) from exc
    except (JobStoreError, ValueError, OSError) as exc:
        renderer.render_error(f"Job error: {exc}")
        raise typer.Exit(2) from exc


def _run_schedule_command(
    factory: Callable[[CliRenderer], Coroutine[Any, Any, None]],
) -> None:
    renderer = CliRenderer()
    try:
        asyncio.run(_run_job_with_signals(factory, renderer))
    except ProviderError as exc:
        renderer.render_error(f"Provider error: {exc}")
        raise typer.Exit(40) from exc
    except (
        CronError,
        JobConfigurationError,
        JobStoreError,
        ScheduleServiceError,
        ScheduleStoreError,
        ValueError,
        OSError,
    ) as exc:
        renderer.render_error(f"Schedule error: {exc}")
        raise typer.Exit(2) from exc


async def _run_job_with_signals(
    factory: Callable[[CliRenderer], Coroutine[Any, Any, None]],
    renderer: CliRenderer,
) -> None:
    """Turn SIGTERM into normal task cancellation so runs can finalize."""

    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    installed = False
    if task is not None:
        try:
            loop.add_signal_handler(signal.SIGTERM, task.cancel)
            installed = True
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await factory(renderer)
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGTERM)


def register_job_commands(job_app: typer.Typer) -> None:
    """Register the commands on the application composition root."""

    job_app.command("list")(job_list)
    job_app.command("validate")(job_validate)
    job_app.command("show")(job_show)
    job_app.command("run")(job_run)
    job_app.command("once")(job_once)
    job_app.command("history")(job_history)
    job_app.command("report")(job_report)


def register_job_action_commands(job_action_app: typer.Typer) -> None:
    """Register the commands on the application composition root."""

    job_action_app.command("show")(job_action_show)
    job_action_app.command("resolve")(job_action_resolve)


def register_schedule_commands(schedule_app: typer.Typer) -> None:
    """Register the commands on the application composition root."""

    schedule_app.command("list")(schedule_list)
    schedule_app.command("show")(schedule_show)
    schedule_app.command("add")(schedule_add)
    schedule_app.command("set")(schedule_set)
    schedule_app.command("enable")(schedule_enable)
    schedule_app.command("disable")(schedule_disable)
    schedule_app.command("remove")(schedule_remove)
    schedule_app.command("approve")(schedule_approve)
    schedule_app.command("refresh")(schedule_refresh)
    schedule_app.command("sync")(schedule_sync)
    schedule_app.command("doctor")(schedule_doctor)
    schedule_app.command("uninstall")(schedule_uninstall)
    schedule_app.command("invoke")(schedule_invoke)
