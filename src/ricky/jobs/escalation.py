"""Stable blocked-work escalation into correlated joint durable tasks."""

from __future__ import annotations

import hashlib

from ricky.durable_tasks.scoped import ScopedDurableTaskStore
from ricky.durable_tasks.store import DurableTaskStore, TaskStoreError
from ricky.jobs.store import JobRunStore
from ricky.profiles import ProfileScope


async def escalate_blocked(
    *,
    job_store: JobRunStore,
    task_store: DurableTaskStore | ScopedDurableTaskStore,
    job_name: str,
    run_id: str,
    source_identity: str,
    summary: str,
    profile_scope: ProfileScope,
) -> str:
    """Create at most one user-waiting joint task for a stable blocked occurrence."""

    blocked_key = hashlib.sha256(f"{job_name}\0{source_identity}".encode()).hexdigest()
    existing = await job_store.escalation_task(job_name, blocked_key, scope=profile_scope)
    if existing is not None:
        try:
            task = await task_store.get_task(existing)
            claimed = await task_store.claim(
                task.id,
                holder_session_id=run_id,
                authority="joint_work",
                executor_id=f"job:{job_name}",
                expected_revision=task.revision,
            )
            assert claimed.lease is not None
            waiting = await task_store.wait(
                claimed.id,
                lease=claimed.lease,
                expected_revision=claimed.revision,
                waiting_on="user",
                current_summary=summary,
                next_action="User: resolve the blocked decision or authorize the requested action.",
                authority="joint_work",
                executor_id=f"job:{job_name}",
            )
            assert waiting.lease is not None
            await task_store.release(
                waiting.id,
                lease=waiting.lease,
                expected_revision=waiting.revision,
                authority="joint_work",
                executor_id=f"job:{job_name}",
                summary="Recurring escalation update recorded",
            )
        except TaskStoreError:
            pass
        await job_store.correlate_escalation(
            job_name, blocked_key, existing, run_id, scope=profile_scope
        )
        return existing
    created = await task_store.create_task(
        title=f"Resolve blocked {job_name} work",
        objective=summary,
        closure_criteria="The user resolves the blocked decision or grants the needed authority.",
        execution_mode="joint",
        authority="agent_autonomy",
        executor_id=f"job:{job_name}",
        session_id=run_id,
        tags=["job-escalation", f"job:{job_name}"],
    )
    claimed = await task_store.claim(
        created.id,
        holder_session_id=run_id,
        authority="joint_work",
        executor_id=f"job:{job_name}",
        expected_revision=created.revision,
    )
    assert claimed.lease is not None
    waiting = await task_store.wait(
        claimed.id,
        lease=claimed.lease,
        expected_revision=claimed.revision,
        waiting_on="user",
        current_summary=summary,
        next_action="User: resolve the blocked decision or authorize the requested action.",
        authority="joint_work",
        executor_id=f"job:{job_name}",
    )
    return await job_store.correlate_escalation(
        job_name, blocked_key, waiting.id, run_id, scope=profile_scope
    )
