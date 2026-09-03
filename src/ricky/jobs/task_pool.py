"""Eligibility, snapshots, cooldown, and fair ordering for durable-task work pools."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ricky.durable_tasks.scoped import ScopedDurableTaskStore
from ricky.durable_tasks.store import DurableTaskStore
from ricky.durable_tasks.types import DurableTask, TaskSearchQuery
from ricky.jobs.sources import CandidateBatch, TaskCandidate
from ricky.jobs.spec import TaskSourceSpec
from ricky.jobs.store import JobRunStore
from ricky.profiles import ProfileScope


class DurableTaskPoolAdapter:
    """Discover unassigned eligible work without inventing a task cursor."""

    async def collect(
        self,
        sources: list[TaskSourceSpec],
        *,
        task_store: DurableTaskStore | ScopedDurableTaskStore,
        job_store: JobRunStore,
        job_name: str,
        profile_scope: ProfileScope,
        total_limit: int,
        now: datetime | None = None,
    ) -> CandidateBatch:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        deduplicated: dict[tuple[str, int], tuple[tuple[object, ...], DurableTask]] = {}
        for source in sources:
            tasks: list[DurableTask] = []
            offset = 0
            while offset < total_limit:
                page_limit = min(task_store.search_limit, total_limit - offset)
                page = await task_store.search(
                    TaskSearchQuery(
                        text=source.text,
                        statuses=source.statuses,
                        execution_modes=source.execution_modes,
                        waiting_on=source.waiting_on,
                        tags_any=source.tags_any,
                        tags_all=source.tags_all,
                        tags_none=source.tags_none,
                        due_before=source.due_before,
                        include_closed=bool(
                            set(source.statuses).intersection({"completed", "cancelled"})
                        ),
                        limit=page_limit,
                        offset=offset,
                    )
                )
                tasks.extend(page)
                if len(page) < page_limit:
                    break
                offset += len(page)
            eligible: list[tuple[tuple[object, ...], DurableTask]] = []
            for task in tasks:
                consideration = await job_store.consideration(
                    job_name, task.id, task.revision, scope=profile_scope
                )
                if consideration is not None:
                    disposition, considered_at = consideration
                    if (
                        disposition == "not_actionable"
                        and considered_at + timedelta(hours=source.reconsider_after_hours) > now
                    ):
                        continue
                fresh = consideration is None
                considered_at = (
                    consideration[1]
                    if consideration is not None
                    else datetime.min.replace(tzinfo=UTC)
                )
                due = task.due_at is not None and task.due_at <= now
                rank = (
                    0 if due else 1,
                    -task.priority,
                    0 if fresh else 1,
                    considered_at,
                    task.id,
                )
                eligible.append((rank, task))
            eligible.sort(key=lambda pair: pair[0])
            for rank, task in eligible[: source.limit]:
                key = (task.id, task.revision)
                current = deduplicated.get(key)
                if current is None or rank < current[0]:
                    deduplicated[key] = (rank, task)

        ranked = list(deduplicated.values())
        ranked.sort(key=lambda pair: pair[0])
        return CandidateBatch(candidates=[_snapshot(task) for _, task in ranked[:total_limit]])


def _snapshot(task: DurableTask) -> TaskCandidate:
    return TaskCandidate(
        id=task.id,
        revision=task.revision,
        title=task.title,
        execution_mode=task.execution_mode,
        status=task.status,
        waiting_on=task.waiting_on,
        priority=task.priority,
        due_at=task.due_at,
        tags=task.tags,
        current_summary=task.current_summary,
        next_action=task.next_action,
    )
