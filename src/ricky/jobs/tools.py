"""Job-only explicit accounting tools for persisted recurring candidates."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar, cast

from pydantic import BaseModel, ConfigDict, Field

from ricky.durable_tasks.scoped import ScopedDurableTaskStore
from ricky.durable_tasks.store import DurableTaskStore
from ricky.jobs.sources import (
    CandidateDispositionKind,
    Disposition,
    ItemDispositionKind,
    PersistedBatch,
)
from ricky.jobs.store import JobRunStore
from ricky.tools.base import Risk, Tool, ToolContext, ToolResult

DISPOSITION_TOOL_NAMES = ("record_item_disposition", "record_candidate_disposition")


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ItemDispositionParams(_Params):
    batch_id: str
    item_id: str
    kind: ItemDispositionKind
    linked_id: str | None = None
    summary: str | None = Field(default=None, max_length=2_000)


class CandidateDispositionParams(_Params):
    batch_id: str
    candidate_id: str
    kind: CandidateDispositionKind
    linked_id: str | None = None
    summary: str | None = Field(default=None, max_length=2_000)


class _DispositionTool:
    risk: ClassVar[Risk] = "mutating"
    capability_id = None
    effect_kind = "ricky_state"
    unattended = "allowed"
    state_guard_id = None

    def __init__(
        self,
        store: JobRunStore,
        task_store: DurableTaskStore | ScopedDurableTaskStore,
        *,
        job_name: str,
        batches: list[PersistedBatch],
        dry_run: bool,
    ) -> None:
        self._store = store
        self._task_store = task_store
        self._job_name = job_name
        self._batches = {batch.id: batch for batch in batches}
        self._dry_run = dry_run

    def _batch(self, batch_id: str, kind: str) -> PersistedBatch:
        batch = self._batches.get(batch_id)
        if batch is None or batch.kind != kind:
            raise ValueError(f"unknown {kind} batch: {batch_id}")
        return batch


class RecordItemDispositionTool(_DispositionTool):
    name = "record_item_disposition"
    description = "Account for one exact persisted stream item before its cursor may advance."
    Params = ItemDispositionParams

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = ItemDispositionParams.model_validate(params)
        batch = self._batch(args.batch_id, "stream")
        disposition = Disposition(
            batch_id=args.batch_id,
            profile_label=batch.profile_label,
            item_id=args.item_id,
            kind=args.kind,
            linked_id=args.linked_id,
            summary=args.summary,
            created_at=datetime.now(UTC),
        )
        await self._store.record_disposition(disposition, scope=ctx.session.profile_scope)
        return ToolResult(content=f"Recorded {args.kind} for stream item {args.item_id}.")


class RecordCandidateDispositionTool(_DispositionTool):
    name = "record_candidate_disposition"
    description = "Account for one exact task-id@revision candidate from this recurring run."
    Params = CandidateDispositionParams

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = CandidateDispositionParams.model_validate(params)
        batch = self._batch(args.batch_id, "task_pool")
        task_id, separator, revision_text = args.candidate_id.rpartition("@")
        if not separator or not revision_text.isdigit():
            raise ValueError("candidate_id must be task-id@revision from the batch")
        if args.kind in {"progressed", "waiting", "completed"}:
            task = await self._task_store.get_task(task_id)
            if args.linked_id != f"{task.id}@{task.revision}" or task.revision <= int(
                revision_text
            ):
                raise ValueError("progress disposition must link a newer persisted task revision")
        disposition = Disposition(
            batch_id=args.batch_id,
            profile_label=batch.profile_label,
            item_id=args.candidate_id,
            kind=args.kind,
            linked_id=args.linked_id,
            summary=args.summary,
            created_at=datetime.now(UTC),
        )
        await self._store.record_disposition(disposition, scope=ctx.session.profile_scope)
        if not self._dry_run:
            await self._store.record_consideration(
                job_name=self._job_name,
                task_id=task_id,
                revision=int(revision_text),
                disposition=args.kind,
                considered_at=disposition.created_at,
                scope=ctx.session.profile_scope,
            )
        return ToolResult(content=f"Recorded {args.kind} for task candidate {args.candidate_id}.")


def disposition_tools(
    store: JobRunStore,
    task_store: DurableTaskStore | ScopedDurableTaskStore,
    *,
    job_name: str,
    batches: list[PersistedBatch],
    dry_run: bool,
) -> list[Tool]:
    return cast(
        list[Tool],
        [
            RecordItemDispositionTool(
                store, task_store, job_name=job_name, batches=batches, dry_run=dry_run
            ),
            RecordCandidateDispositionTool(
                store, task_store, job_name=job_name, batches=batches, dry_run=dry_run
            ),
        ],
    )
