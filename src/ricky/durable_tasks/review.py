"""Composite parking of drafted work on a joint task that waits for the user."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from contextlib import suppress
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ricky.durable_tasks.artifacts import TaskArtifactStore
from ricky.durable_tasks.scoped import ScopedDurableTaskStore, ScopedTaskArtifactStore
from ricky.durable_tasks.store import DurableTaskStore, TaskLeaseError
from ricky.durable_tasks.types import (
    DurableTask,
    TaskArtifactEntry,
    TaskLease,
    TaskSearchQuery,
    TaskTag,
    canonicalize_task_tags,
)
from ricky.profiles import ProfileName

REVIEW_TAG_PREFIX = "review"
_KEY_DIGEST_CHARS = 32
_AUTHORITY = "joint_work"
_RELEASE_SUMMARY = "Parked for user review"


class ReviewArtifact(BaseModel):
    """One human-readable document parked with a review task."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)


class ParkedReview(BaseModel):
    """The parked task, its written artifacts, and whether a task was reused."""

    model_config = ConfigDict(extra="forbid")

    task: DurableTask
    artifacts: list[TaskArtifactEntry]
    reused: bool


def review_tag(dedupe_key: str) -> str:
    """Derive the canonical idempotency tag for one review subject."""

    key = dedupe_key.strip()
    if not key:
        raise ValueError("dedupe_key must be non-empty")
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:_KEY_DIGEST_CHARS]
    return f"{REVIEW_TAG_PREFIX}:{digest}"


async def park_for_review(
    *,
    store: DurableTaskStore | ScopedDurableTaskStore,
    artifacts: TaskArtifactStore | ScopedTaskArtifactStore,
    title: str,
    objective: str,
    closure_criteria: str,
    current_summary: str,
    next_action: str,
    dedupe_key: str,
    review_artifacts: Sequence[ReviewArtifact],
    executor_id: str,
    session_id: str,
    tags: Sequence[TaskTag] | None = None,
    priority: int = 0,
    due_at: datetime | None = None,
    profile: ProfileName | None = None,
) -> ParkedReview:
    """Create or refresh exactly one user-waiting joint task holding no lease.

    The caller never handles a lease. Every path releases the lease it takes, so
    a partial failure cannot leave a leased task nobody can advance.
    """

    if not review_artifacts:
        raise ValueError("parking a review requires at least one artifact")
    tag = review_tag(dedupe_key)
    query = TaskSearchQuery(tags_all=[tag], limit=1)
    if isinstance(store, ScopedDurableTaskStore):
        existing = await store.search(query, profiles=[profile] if profile is not None else ())
    else:
        existing = await store.search(query)
    reused = bool(existing)
    if reused:
        task = existing[0]
    else:
        create_values = {
            "title": title,
            "objective": objective,
            "closure_criteria": closure_criteria,
            "execution_mode": "joint",
            "authority": _AUTHORITY,
            "executor_id": executor_id,
            "session_id": session_id,
            "priority": priority,
            "due_at": due_at,
            "tags": canonicalize_task_tags([tag, *(tags or [])]),
        }
        if isinstance(store, ScopedDurableTaskStore):
            create_values["profile"] = profile
        task = await store.create_task(
            **create_values,  # type: ignore[arg-type]
        )
    claimed = await store.claim(
        task.id,
        holder_session_id=session_id,
        authority=_AUTHORITY,
        executor_id=executor_id,
        expected_revision=task.revision,
    )
    try:
        current = claimed
        entries: list[TaskArtifactEntry] = []
        for artifact in review_artifacts:
            written = await artifacts.write(
                current.id,
                artifact.path,
                artifact.content,
                lease=_lease_of(current),
                expected_revision=current.revision,
                expected_sha256=await _current_digest(artifacts, current.id, artifact.path),
                authority=_AUTHORITY,
                executor_id=executor_id,
            )
            current = written.task
            entries.append(written.entry)
        waiting = await store.wait(
            current.id,
            lease=_lease_of(current),
            expected_revision=current.revision,
            waiting_on="user",
            current_summary=current_summary,
            next_action=next_action,
            authority=_AUTHORITY,
            executor_id=executor_id,
        )
        released = await store.release(
            waiting.id,
            lease=_lease_of(waiting),
            expected_revision=waiting.revision,
            authority=_AUTHORITY,
            executor_id=executor_id,
            summary=_RELEASE_SUMMARY,
        )
    except BaseException:
        await _unwind(store, task.id, session_id, executor_id, cancel=not reused)
        raise
    return ParkedReview(task=released, artifacts=entries, reused=reused)


async def _current_digest(
    artifacts: TaskArtifactStore | ScopedTaskArtifactStore,
    task_id: str,
    path: str,
) -> str | None:
    """Return the digest a replacement must guard against, or None when absent."""

    with suppress(FileNotFoundError):
        return (await artifacts.inspect(task_id, path)).sha256
    return None


def _lease_of(task: DurableTask) -> TaskLease:
    if task.lease is None:
        raise TaskLeaseError(f"parked review lost its lease: {task.id}")
    return task.lease


async def _unwind(
    store: DurableTaskStore | ScopedDurableTaskStore,
    task_id: str,
    session_id: str,
    executor_id: str,
    *,
    cancel: bool,
) -> None:
    """Never leave this session's lease behind, whatever failed above.

    A task this call created is also cancelled, so a failed parking attempt
    leaves no half-built review task. A reused task always survives.
    """

    with suppress(Exception):
        task = await store.get_task(task_id)
        if task.lease is None or task.lease.holder_session_id != session_id:
            return
        if cancel:
            await store.cancel(
                task_id,
                lease=task.lease,
                expected_revision=task.revision,
                reason="Parking for review failed before the task was usable",
                authority=_AUTHORITY,
                executor_id=executor_id,
            )
            return
        await store.release(
            task_id,
            lease=task.lease,
            expected_revision=task.revision,
            authority=_AUTHORITY,
            executor_id=executor_id,
            summary="Parking failed; lease released",
        )
