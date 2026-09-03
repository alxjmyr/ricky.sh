"""Agent tools for profile-owned durable tasks and their artifacts."""

from __future__ import annotations

import difflib
import hashlib
import json
from datetime import datetime
from typing import ClassVar, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ricky.durable_tasks.artifacts import TaskArtifactRead, TaskArtifactStore
from ricky.durable_tasks.render import (
    render_activity,
    render_artifacts,
    render_task,
    render_task_list,
)
from ricky.durable_tasks.review import ParkedReview, ReviewArtifact, park_for_review
from ricky.durable_tasks.scoped import ScopedDurableTaskStore, ScopedTaskArtifactStore
from ricky.durable_tasks.store import DurableTaskStore, TaskLeaseError
from ricky.durable_tasks.types import (
    DurableTask,
    TaskActivity,
    TaskArtifactEntry,
    TaskAuthority,
    TaskDetail,
    TaskExecutionMode,
    TaskSearchQuery,
    TaskStatus,
    TaskTag,
    TaskWaitingOn,
)
from ricky.permissions import GrantScope, Policy, PolicyRule
from ricky.profiles import ProfileName
from ricky.tools.base import (
    EffectIdentity,
    EffectReceipt,
    Risk,
    Tool,
    ToolContext,
    ToolResult,
)

_PREVIEW_LIMIT = 12_000


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("due_at", "due_before", check_fields=False)
    @classmethod
    def _valid_due_time(cls, value: str | None, info: object) -> str | None:
        if value is not None:
            field_name = getattr(info, "field_name", "due time")
            _parse_datetime(value, str(field_name))
        return value


class TaskResult(BaseModel):
    task: DurableTask


class TaskListResult(BaseModel):
    tasks: list[DurableTask]


class TaskDetailResult(BaseModel):
    detail: TaskDetail


class ActivityResult(BaseModel):
    activity: list[TaskActivity]


class ArtifactListResult(BaseModel):
    artifacts: list[TaskArtifactEntry]


class ArtifactReadResult(BaseModel):
    artifact: TaskArtifactRead


class ArtifactWriteResult(BaseModel):
    task: DurableTask
    artifact: TaskArtifactEntry
    created: bool


class ParkForReviewResult(BaseModel):
    task: DurableTask
    artifacts: list[TaskArtifactEntry]
    reused: bool


class SearchTasksParams(_Params):
    profiles: list[ProfileName] = Field(
        default_factory=list,
        description="Accessible profiles to search; empty searches the complete active scope.",
    )
    text: str | None = None
    statuses: list[TaskStatus] = Field(default_factory=list)
    execution_modes: list[TaskExecutionMode] = Field(default_factory=list)
    waiting_on: list[TaskWaitingOn] = Field(default_factory=list)
    tags_any: list[TaskTag] = Field(default_factory=list, max_length=50)
    tags_all: list[TaskTag] = Field(default_factory=list, max_length=50)
    tags_none: list[TaskTag] = Field(default_factory=list, max_length=50)
    due_before: str | None = Field(
        default=None,
        description="ISO 8601 timestamp; only tasks due before this instant match.",
    )
    include_closed: bool = False
    limit: int = Field(default=20, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class ReadTaskParams(_Params):
    task_id: str
    activity_limit: int = Field(default=20, ge=1, le=500)


class CreateTaskParams(_Params):
    profile: ProfileName | None = Field(
        default=None,
        description="Accessible destination profile; defaults to the primary profile.",
    )
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    closure_criteria: str = Field(min_length=1)
    execution_mode: TaskExecutionMode
    priority: int = Field(default=0, ge=-100, le=100)
    due_at: str | None = Field(default=None, description="Optional ISO 8601 due timestamp.")
    tags: list[TaskTag] = Field(default_factory=list, max_length=50)


class ParkForReviewParams(_Params):
    profile: ProfileName | None = Field(
        default=None,
        description="Accessible destination profile; defaults to the primary profile.",
    )
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    closure_criteria: str = Field(min_length=1)
    current_summary: str = Field(min_length=1)
    next_action: str = Field(min_length=1)
    dedupe_key: str = Field(min_length=1, max_length=500)
    artifacts: list[ReviewArtifact] = Field(min_length=1, max_length=10)
    tags: list[TaskTag] = Field(default_factory=list, max_length=50)
    priority: int = Field(default=0, ge=-100, le=100)
    due_at: str | None = Field(default=None, description="Optional ISO 8601 due timestamp.")


class TaskIdParams(_Params):
    task_id: str


class ClaimTaskParams(TaskIdParams):
    expected_revision: int | None = Field(default=None, ge=1)


class UpdateTaskTagsParams(TaskIdParams):
    tags: list[TaskTag] = Field(default_factory=list, max_length=50)


class ProgressTaskParams(TaskIdParams):
    current_summary: str = Field(min_length=1)
    next_action: str | None = None
    priority: int | None = Field(default=None, ge=-100, le=100)
    due_at: str | None = Field(default=None, description="Optional ISO 8601 due timestamp.")
    clear_due_at: bool = False


class WaitTaskParams(TaskIdParams):
    waiting_on: TaskWaitingOn
    current_summary: str = Field(min_length=1)
    next_action: str = Field(min_length=1)
    due_at: str | None = Field(default=None, description="Optional ISO 8601 due timestamp.")
    clear_due_at: bool = False


class BlockTaskParams(TaskIdParams):
    current_summary: str = Field(min_length=1)
    next_action: str = Field(min_length=1)


class CompleteTaskParams(TaskIdParams):
    completion_summary: str = Field(min_length=1)


class CancelTaskParams(TaskIdParams):
    reason: str = Field(min_length=1)


class ReopenTaskParams(TaskIdParams):
    reason: str = Field(min_length=1)


class ReleaseTaskParams(TaskIdParams):
    summary: str = Field(default="Pausing work; lease released", min_length=1)


class ArtifactPathParams(TaskIdParams):
    path: str = Field(min_length=1)


class ReadArtifactParams(ArtifactPathParams):
    start_line: int = Field(default=1, ge=1)
    line_count: int | None = Field(default=None, ge=1, le=10_000)


class WriteArtifactParams(ArtifactPathParams):
    content: str
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class EditArtifactParams(ArtifactPathParams):
    old: str = Field(min_length=1)
    new: str
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _TaskTool:
    risk: ClassVar[Risk] = "mutating"
    capability_id = "builtin.task.mutate"
    effect_kind = "ricky_state"
    unattended = "allowed"
    state_guard_id = None

    def __init__(
        self,
        store: DurableTaskStore | ScopedDurableTaskStore,
        artifacts: TaskArtifactStore | ScopedTaskArtifactStore,
    ) -> None:
        self._store = store
        self._artifacts = artifacts

    async def _task_and_authority(self, task_id: str) -> tuple[DurableTask, TaskAuthority]:
        task = await self._store.get_task(task_id)
        authority: TaskAuthority
        if task.execution_mode == "agent":
            authority = "agent_autonomy"
        elif task.execution_mode == "joint":
            authority = "joint_work"
        else:
            authority = "direct_user_instruction"
        return task, authority

    @staticmethod
    def _lease(ctx: ToolContext, task_id: str):
        lease = ctx.session.active_task_leases.get(task_id)
        if lease is None:
            raise TaskLeaseError(f"claim durable task before mutating it: {task_id}")
        return lease

    @staticmethod
    def _sync_lease(ctx: ToolContext, task: DurableTask) -> None:
        if task.lease is None:
            ctx.session.active_task_leases.pop(task.id, None)
        else:
            ctx.session.active_task_leases[task.id] = task.lease


class SearchDurableTasksTool(_TaskTool):
    name = "search_durable_tasks"
    description = "Search cross-session durable tasks in the selected profile."
    Params = SearchTasksParams
    Result = TaskListResult
    risk = "read_only"
    capability_id = "builtin.task.read"
    effect_kind = "none"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = SearchTasksParams.model_validate(params)
        values = args.model_dump(exclude={"due_before", "profiles"})
        query = TaskSearchQuery(
            **values,
            due_before=_parse_datetime(args.due_before, "due_before"),
        )
        if isinstance(self._store, ScopedDurableTaskStore):
            tasks = await self._store.search(query, profiles=args.profiles)
        else:
            tasks = await self._store.search(query)
        return ToolResult(
            content=render_task_list(tasks),
            data=TaskListResult(tasks=tasks).model_dump(mode="json"),
        )


class ReadDurableTaskTool(_TaskTool):
    name = "read_durable_task"
    description = "Read one durable task, recent activity, and artifact metadata."
    Params = ReadTaskParams
    Result = TaskDetailResult
    risk = "read_only"
    capability_id = "builtin.task.read"
    effect_kind = "none"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = ReadTaskParams.model_validate(params)
        task = await self._store.get_task(args.task_id)
        activity = await self._store.activities(args.task_id, limit=args.activity_limit)
        artifacts = await self._artifacts.list(args.task_id)
        detail = TaskDetail(task=task, recent_activity=activity, artifact_files=artifacts)
        content = "\n\n".join(
            [
                render_task(task),
                "## Recent activity",
                render_activity(activity),
                "## Artifacts",
                _render_artifacts_with_attachment_references(
                    artifacts,
                    task_id=args.task_id,
                    profile=task.profile,
                ),
            ]
        )
        return ToolResult(
            content=content,
            data=TaskDetailResult(detail=detail).model_dump(mode="json"),
        )


class CreateDurableTaskTool(_TaskTool):
    name = "create_durable_task"
    description = (
        "Create cross-session responsibility with an objective, closure criteria, and "
        "explicit agent/joint/user ownership mode."
    )
    Params = CreateTaskParams
    Result = TaskResult
    legacy_contract_never_unattended = True

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = CreateTaskParams.model_validate(params)
        if args.execution_mode == "agent":
            authority: TaskAuthority = "agent_autonomy"
        elif args.execution_mode == "joint":
            authority = "joint_work"
        else:
            authority = "direct_user_instruction"
        values = args.model_dump(exclude={"due_at"}, exclude_none=True)
        task = await self._store.create_task(
            **values,
            due_at=_parse_datetime(args.due_at, "due_at"),
            authority=authority,
            executor_id=ctx.session.id,
            session_id=ctx.session.id,
        )
        return _task_result(task, "Durable task created")


class ParkForReviewTool(_TaskTool):
    name = "park_for_review"
    description = (
        "Park drafted work and its context on a joint durable task that waits for the "
        "user, then finish. Creates or refreshes exactly one task per dedupe_key, writes "
        "the supplied artifacts, and holds no lease afterwards, so a later session can "
        "review, edit, act, and close it. Performs no outward effect of its own."
    )
    Params = ParkForReviewParams
    Result = ParkForReviewResult

    def permission_scope(self, args: dict[str, object], ctx: ToolContext) -> GrantScope:
        # Projecting on dedupe_key would re-prompt for every parked item, which is
        # the per-item identity fault that scoped grants exist to avoid.
        del args, ctx
        return GrantScope(
            params_equal={},
            label="park drafted work on a durable task for review",
            allow_unconstrained=True,
        )

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        del ctx
        parsed = ParkForReviewParams.model_validate(args)
        paths = ", ".join(artifact.path for artifact in parsed.artifacts)
        return _truncate(
            f"park for user review: {parsed.title}\n"
            f"subject key: {parsed.dedupe_key}\n"
            f"artifacts: {paths}\n\n"
            f"--- {parsed.artifacts[0].path} ---\n{parsed.artifacts[0].content}",
            _PREVIEW_LIMIT,
        )

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        del ctx
        parsed = ParkForReviewParams.model_validate(args)
        operation = "durable_tasks.park_for_review"
        default_profile = (
            self._store.primary_profile
            if isinstance(self._store, ScopedDurableTaskStore)
            else self._store.profile
        )
        target = f"durable-tasks:{parsed.profile or default_profile}"
        occurrence = parsed.dedupe_key.strip()
        encoded = json.dumps(
            {"operation": operation, "target": target, "occurrence": occurrence},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return EffectIdentity(
            operation=operation,
            target=target,
            occurrence=occurrence,
            summary=f"Park {parsed.title} for user review",
            action_key=hashlib.sha256(encoded).hexdigest(),
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = ParkForReviewParams.model_validate(params)
        values = args.model_dump(exclude={"artifacts", "due_at"}, exclude_none=True)
        parked = await park_for_review(
            store=self._store,
            artifacts=self._artifacts,
            review_artifacts=args.artifacts,
            executor_id=ctx.session.id,
            session_id=ctx.session.id,
            **values,
            due_at=_parse_datetime(args.due_at, "due_at"),
        )
        # The composite always releases its own lease; never advertise one.
        ctx.session.active_task_leases.pop(parked.task.id, None)
        return _parked_result(parked)


class ClaimDurableTaskTool(_TaskTool):
    name = "claim_durable_task"
    description = "Acquire the exclusive expiring lease required to advance a durable task."
    Params = ClaimTaskParams
    Result = TaskResult
    state_guard_id = "durable_task.lease"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = ClaimTaskParams.model_validate(params)
        _, authority = await self._task_and_authority(args.task_id)
        task = await self._store.claim(
            args.task_id,
            holder_session_id=ctx.session.id,
            authority=authority,
            executor_id=ctx.session.id,
            expected_revision=args.expected_revision,
        )
        self._sync_lease(ctx, task)
        return _task_result(task, "Durable task claimed")


class RenewDurableTaskLeaseTool(_TaskTool):
    name = "renew_durable_task_lease"
    description = "Renew this session's lease using the configured duration."
    Params = TaskIdParams
    Result = TaskResult
    state_guard_id = "durable_task.lease"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = TaskIdParams.model_validate(params)
        task, authority = await self._task_and_authority(args.task_id)
        lease = self._lease(ctx, args.task_id)
        updated = await self._store.renew(
            args.task_id,
            lease=lease,
            expected_revision=task.revision,
            authority=authority,
            executor_id=ctx.session.id,
        )
        self._sync_lease(ctx, updated)
        return _task_result(updated, "Durable task lease renewed")


class UpdateDurableTaskProgressTool(_TaskTool):
    name = "update_durable_task_progress"
    description = "Record bounded progress and the next action on a claimed durable task."
    Params = ProgressTaskParams
    Result = TaskResult
    state_guard_id = "durable_task.lease"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = ProgressTaskParams.model_validate(params)
        task, authority = await self._task_and_authority(args.task_id)
        values = args.model_dump(exclude={"task_id", "due_at"})
        updated = await self._store.progress(
            args.task_id,
            lease=self._lease(ctx, args.task_id),
            expected_revision=task.revision,
            authority=authority,
            executor_id=ctx.session.id,
            **values,
            due_at=_parse_datetime(args.due_at, "due_at"),
        )
        self._sync_lease(ctx, updated)
        return _task_result(updated, "Durable task progress updated")


class UpdateDurableTaskTagsTool(_TaskTool):
    name = "update_durable_task_tags"
    description = "Replace exact discovery tags on a claimed durable task."
    Params = UpdateTaskTagsParams
    Result = TaskResult
    state_guard_id = "durable_task.lease"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = UpdateTaskTagsParams.model_validate(params)
        task, authority = await self._task_and_authority(args.task_id)
        updated = await self._store.update_tags(
            args.task_id,
            tags=args.tags,
            lease=self._lease(ctx, args.task_id),
            expected_revision=task.revision,
            authority=authority,
            executor_id=ctx.session.id,
        )
        self._sync_lease(ctx, updated)
        return _task_result(updated, "Durable task tags updated")


class WaitDurableTaskTool(_TaskTool):
    name = "wait_durable_task"
    description = "Pause a claimed task on an agent, user, external response, or time."
    Params = WaitTaskParams
    Result = TaskResult
    state_guard_id = "durable_task.lease"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = WaitTaskParams.model_validate(params)
        task, authority = await self._task_and_authority(args.task_id)
        values = args.model_dump(exclude={"task_id", "due_at"})
        updated = await self._store.wait(
            args.task_id,
            lease=self._lease(ctx, args.task_id),
            expected_revision=task.revision,
            authority=authority,
            executor_id=ctx.session.id,
            **values,
            due_at=_parse_datetime(args.due_at, "due_at"),
        )
        self._sync_lease(ctx, updated)
        return _task_result(updated, "Durable task is waiting")


class BlockDurableTaskTool(_TaskTool):
    name = "block_durable_task"
    description = "Record an unresolved blocker and an unblock condition or next action."
    Params = BlockTaskParams
    Result = TaskResult
    state_guard_id = "durable_task.lease"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = BlockTaskParams.model_validate(params)
        task, authority = await self._task_and_authority(args.task_id)
        updated = await self._store.block(
            args.task_id,
            lease=self._lease(ctx, args.task_id),
            expected_revision=task.revision,
            authority=authority,
            executor_id=ctx.session.id,
            current_summary=args.current_summary,
            next_action=args.next_action,
        )
        self._sync_lease(ctx, updated)
        return _task_result(updated, "Durable task blocked")


class CompleteDurableTaskTool(_TaskTool):
    name = "complete_durable_task"
    description = "Complete a claimed task with an inspectable closure summary."
    Params = CompleteTaskParams
    Result = TaskResult
    state_guard_id = "durable_task.lease"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = CompleteTaskParams.model_validate(params)
        task, authority = await self._task_and_authority(args.task_id)
        updated = await self._store.complete(
            args.task_id,
            lease=self._lease(ctx, args.task_id),
            expected_revision=task.revision,
            completion_summary=args.completion_summary,
            authority=authority,
            executor_id=ctx.session.id,
        )
        self._sync_lease(ctx, updated)
        return _task_result(updated, "Durable task completed")


class CancelDurableTaskTool(_TaskTool):
    name = "cancel_durable_task"
    description = "Cancel a claimed durable task without deleting its history."
    Params = CancelTaskParams
    Result = TaskResult
    state_guard_id = "durable_task.lease"
    legacy_contract_never_unattended = True

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = CancelTaskParams.model_validate(params)
        task, authority = await self._task_and_authority(args.task_id)
        updated = await self._store.cancel(
            args.task_id,
            lease=self._lease(ctx, args.task_id),
            expected_revision=task.revision,
            reason=args.reason,
            authority=authority,
            executor_id=ctx.session.id,
        )
        self._sync_lease(ctx, updated)
        return _task_result(updated, "Durable task cancelled")


class ReopenDurableTaskTool(_TaskTool):
    name = "reopen_durable_task"
    description = "Reopen a completed or cancelled durable task with a reason."
    Params = ReopenTaskParams
    Result = TaskResult
    legacy_contract_never_unattended = True

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = ReopenTaskParams.model_validate(params)
        task, authority = await self._task_and_authority(args.task_id)
        del task
        updated = await self._store.reopen(
            args.task_id,
            reason=args.reason,
            authority=authority,
            executor_id=ctx.session.id,
            session_id=ctx.session.id,
        )
        return _task_result(updated, "Durable task reopened")


class ReleaseDurableTaskTool(_TaskTool):
    name = "release_durable_task"
    description = "Release this session's mutation lease without closing the task."
    Params = ReleaseTaskParams
    Result = TaskResult
    state_guard_id = "durable_task.lease"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = ReleaseTaskParams.model_validate(params)
        task, authority = await self._task_and_authority(args.task_id)
        updated = await self._store.release(
            args.task_id,
            lease=self._lease(ctx, args.task_id),
            expected_revision=task.revision,
            authority=authority,
            executor_id=ctx.session.id,
            summary=args.summary,
        )
        self._sync_lease(ctx, updated)
        return _task_result(updated, "Durable task lease released")


class ListTaskArtifactsTool(_TaskTool):
    name = "list_task_artifacts"
    description = (
        "List generic files in one durable task's artifact workspace. The result includes "
        "copy-ready logical attachment references; use them instead of guessing storage paths."
    )
    Params = TaskIdParams
    Result = ArtifactListResult
    risk = "read_only"
    capability_id = "builtin.task.read"
    effect_kind = "none"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = TaskIdParams.model_validate(params)
        task = await self._store.get_task(args.task_id)
        artifacts = await self._artifacts.list(args.task_id)
        return ToolResult(
            content=_render_artifacts_with_attachment_references(
                artifacts,
                task_id=args.task_id,
                profile=task.profile,
            ),
            data=ArtifactListResult(artifacts=artifacts).model_dump(mode="json"),
        )


def _render_artifacts_with_attachment_references(
    artifacts: list[TaskArtifactEntry],
    *,
    task_id: str,
    profile: str,
) -> str:
    rendered = render_artifacts(artifacts)
    if not artifacts:
        return rendered
    references = "\n".join(
        json.dumps(
            {
                "task_id": task_id,
                "task_artifact_path": artifact.path,
                "profile": profile,
            },
            separators=(",", ":"),
        )
        for artifact in artifacts
    )
    return (
        f"{rendered}\n\nAttachment references (copy one object into an outbound "
        f"attachments list; never construct a storage path):\n{references}"
    )


class ReadTaskArtifactTool(_TaskTool):
    name = "read_task_artifact"
    description = "Read bounded text from one task artifact; no lease is required."
    Params = ReadArtifactParams
    Result = ArtifactReadResult
    risk = "read_only"
    capability_id = "builtin.task.read"
    effect_kind = "none"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = ReadArtifactParams.model_validate(params)
        artifact = await self._artifacts.read(**args.model_dump())
        marker = "\n[truncated]" if artifact.truncated else ""
        return ToolResult(
            content=(
                f"{artifact.entry.path} lines {artifact.start_line}-{artifact.end_line} "
                f"sha256={artifact.entry.sha256}\n\n{artifact.content}{marker}"
            ),
            data=ArtifactReadResult(artifact=artifact).model_dump(mode="json"),
        )


class _ArtifactMutationTool(_TaskTool):
    state_guard_id = "durable_task.lease"

    def permission_scope(self, args: dict[str, object], ctx: ToolContext) -> GrantScope:
        del ctx
        task_id = str(args.get("task_id", ""))
        path = str(args.get("path", ""))
        return GrantScope(
            params_equal={"task_id": task_id, "path": path},
            label=f"write artifact {task_id}/{path}",
            allow_unconstrained=False,
        )


class WriteTaskArtifactTool(_ArtifactMutationTool):
    name = "write_task_artifact"
    description = "Create or digest-guarded replace a generic text artifact for a claimed task."
    Params = WriteArtifactParams
    Result = ArtifactWriteResult

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        del ctx
        parsed = WriteArtifactParams.model_validate(args)
        action = "create" if parsed.expected_sha256 is None else "replace"
        content = parsed.content[:_PREVIEW_LIMIT]
        marker = "\n[preview truncated]" if len(parsed.content) > _PREVIEW_LIMIT else ""
        return (
            f"{action} task artifact: {parsed.task_id}/{parsed.path}\n"
            f"expected sha256: {parsed.expected_sha256 or '(new file)'}\n\n"
            f"{content}{marker}"
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = WriteArtifactParams.model_validate(params)
        task, authority = await self._task_and_authority(args.task_id)
        result = await self._artifacts.write(
            args.task_id,
            args.path,
            args.content,
            lease=self._lease(ctx, args.task_id),
            expected_revision=task.revision,
            expected_sha256=args.expected_sha256,
            authority=authority,
            executor_id=ctx.session.id,
        )
        self._sync_lease(ctx, result.task)
        return ToolResult(
            content=f"Task artifact {'created' if result.created else 'updated'}: "
            f"{result.entry.path} sha256={result.entry.sha256}",
            data=ArtifactWriteResult(
                task=result.task, artifact=result.entry, created=result.created
            ).model_dump(mode="json"),
        )


class EditTaskArtifactTool(_ArtifactMutationTool):
    name = "edit_task_artifact"
    description = "Apply one digest-guarded exact single-occurrence edit to a task artifact."
    Params = EditArtifactParams
    Result = ArtifactWriteResult

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        del ctx
        parsed = EditArtifactParams.model_validate(args)
        diff = "\n".join(
            difflib.unified_diff(
                parsed.old.splitlines(),
                parsed.new.splitlines(),
                fromfile=f"{parsed.task_id}/{parsed.path} before fragment",
                tofile=f"{parsed.task_id}/{parsed.path} after fragment",
                lineterm="",
            )
        )
        return _truncate(
            f"edit task artifact: {parsed.task_id}/{parsed.path}\n"
            f"expected sha256: {parsed.expected_sha256}\n\n{diff}",
            _PREVIEW_LIMIT,
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = EditArtifactParams.model_validate(params)
        task, authority = await self._task_and_authority(args.task_id)
        result = await self._artifacts.exact_edit(
            args.task_id,
            args.path,
            old=args.old,
            new=args.new,
            expected_sha256=args.expected_sha256,
            lease=self._lease(ctx, args.task_id),
            expected_revision=task.revision,
            authority=authority,
            executor_id=ctx.session.id,
        )
        self._sync_lease(ctx, result.task)
        return ToolResult(
            content=f"Task artifact updated: {result.entry.path} sha256={result.entry.sha256}",
            data=ArtifactWriteResult(
                task=result.task, artifact=result.entry, created=False
            ).model_dump(mode="json"),
        )


def durable_task_tools(
    store: DurableTaskStore | ScopedDurableTaskStore,
    artifacts: TaskArtifactStore | ScopedTaskArtifactStore,
) -> list[Tool]:
    """Build the complete task toolpack for one immutable profile scope."""

    tools = cast(
        list[Tool],
        [
            SearchDurableTasksTool(store, artifacts),
            ReadDurableTaskTool(store, artifacts),
            CreateDurableTaskTool(store, artifacts),
            ParkForReviewTool(store, artifacts),
            ClaimDurableTaskTool(store, artifacts),
            RenewDurableTaskLeaseTool(store, artifacts),
            UpdateDurableTaskProgressTool(store, artifacts),
            UpdateDurableTaskTagsTool(store, artifacts),
            WaitDurableTaskTool(store, artifacts),
            BlockDurableTaskTool(store, artifacts),
            CompleteDurableTaskTool(store, artifacts),
            CancelDurableTaskTool(store, artifacts),
            ReopenDurableTaskTool(store, artifacts),
            ReleaseDurableTaskTool(store, artifacts),
            ListTaskArtifactsTool(store, artifacts),
            ReadTaskArtifactTool(store, artifacts),
            WriteTaskArtifactTool(store, artifacts),
            EditTaskArtifactTool(store, artifacts),
        ],
    )
    return tools


COORDINATION_TOOL_NAMES = frozenset(
    {
        "create_durable_task",
        "claim_durable_task",
        "renew_durable_task_lease",
        "update_durable_task_progress",
        "update_durable_task_tags",
        "wait_durable_task",
        "block_durable_task",
        "complete_durable_task",
        "cancel_durable_task",
        "reopen_durable_task",
        "release_durable_task",
    }
)


def durable_task_policy(base: Policy | None = None) -> Policy:
    """Add exact coordination-tool allows after any caller-supplied deny rules."""

    policy = base or Policy()
    allows = [
        PolicyRule(
            tool_name=name,
            decision="allow",
            reason="durable task coordination is approved internal bookkeeping",
        )
        for name in sorted(COORDINATION_TOOL_NAMES)
    ]
    return policy.model_copy(update={"rules": [*policy.rules, *allows]})


def _parked_result(parked: ParkedReview) -> ToolResult:
    action = "refreshed" if parked.reused else "created"
    return ToolResult(
        content=(
            f"Review task {action} and waiting on the user.\n"
            f"{render_task(parked.task)}\n\n"
            f"{render_artifacts(parked.artifacts)}"
        ),
        data=ParkForReviewResult(
            task=parked.task, artifacts=parked.artifacts, reused=parked.reused
        ).model_dump(mode="json"),
        effect_receipt=EffectReceipt(disposition="performed", provider_reference=parked.task.id),
    )


def _task_result(task: DurableTask, action: str) -> ToolResult:
    return ToolResult(
        content=f"{action}.\n{render_task(task)}",
        data=TaskResult(task=task).model_dump(mode="json"),
    )


def _parse_datetime(value: str | None, label: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone offset")
    return parsed


def _truncate(value: str, limit: int) -> str:
    marker = "\n[preview truncated]"
    if len(value) <= limit:
        return value
    return f"{value[: limit - len(marker)]}{marker}"
