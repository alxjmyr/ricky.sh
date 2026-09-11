"""Job-owned projection of durable outcomes into platform-neutral notifications."""

from __future__ import annotations

from ricky.config import RickySettings
from ricky.jobs.reporting import failure_summary
from ricky.jobs.store import JobRunStore
from ricky.jobs.types import JobRun
from ricky.notifications import (
    NotificationRecord,
    NotificationService,
    job_completed,
    job_failed,
    job_needs_approval,
)
from ricky.profiles import ProfileLabel, ProfileScope


async def enqueue_job_notification(
    run: JobRun,
    *,
    route: str,
    profile_label: ProfileLabel,
    profile_scope: ProfileScope,
    service: NotificationService,
    store: JobRunStore | None = None,
) -> NotificationRecord | None:
    """Project one already-durable named-job terminal state with stable deduplication."""

    if (
        run.job_name is None
        or run.outcome is None
        or run.dry_run
        or run.result_notification == "never"
    ):
        return None
    if run.trigger == "execution":
        return None
    if run.outcome == "skipped_locked":
        return None
    if run.outcome == "succeeded":
        summary = run.final_message or "Job completed successfully."
    else:
        summary = run.error or f"Job ended with outcome {run.outcome}."
        if store is not None:
            summary = await failure_summary(
                run,
                summary,
                store=store,
                scope=profile_scope,
                limit=service.settings.body_char_limit,
            )
    common = {
        "route": route,
        "job_name": run.job_name,
        "run_id": run.id,
        "summary": summary,
        "profile_label": profile_label,
        "created_at": run.finished_at or run.started_at,
    }
    if run.outcome == "succeeded":
        request = job_completed(**common)
    elif run.outcome == "approval_required":
        request = job_needs_approval(**common)
    else:
        request = job_failed(**common)
    return await service.enqueue(request, scope=profile_scope)


async def project_job_notifications(
    settings: RickySettings,
    *,
    store: JobRunStore,
    service: NotificationService,
    route: str,
    profile_scope: ProfileScope,
    limit: int = 500,
) -> int:
    """Find recent terminal runs and idempotently fill any enqueue gap."""

    del settings
    projected = 0
    for run in reversed(await store.list(scope=profile_scope, limit=limit)):
        record = await enqueue_job_notification(
            run,
            route=route,
            profile_label=run.profile_scope.label(),
            profile_scope=profile_scope,
            service=service,
            store=store,
        )
        if record is not None:
            projected += 1
    return projected
