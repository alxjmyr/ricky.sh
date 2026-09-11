"""Subsystem-owned unattended safety guard for durable-task mutations."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import BaseModel

from ricky.durable_tasks.scoped import ScopedDurableTaskStore
from ricky.durable_tasks.store import DurableTaskStore
from ricky.durable_tasks.types import DurableTask
from ricky.tool_contracts import ToolRuntimeFailure
from ricky.tools.base import Tool, ToolContext, ToolResult


class GuardedDurableTaskTool:
    """Keep user-owned and user-baton tasks read-only in unattended runs."""

    def __init__(self, tool: Tool, store: DurableTaskStore | ScopedDurableTaskStore) -> None:
        self._tool = tool
        self._store = store
        self.name = tool.name
        self.description = tool.description
        self.Params = tool.Params
        self.risk = tool.risk
        declared = cast(Any, tool)
        self.capability_id = declared.capability_id
        self.effect_kind = declared.effect_kind
        self.unattended = declared.unattended
        self.state_guard_id = declared.state_guard_id
        self.review_mode = getattr(declared, "review_mode", "policy")

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = params.model_dump(mode="python")
        task_id = args.get("task_id")
        if not isinstance(task_id, str):
            return ToolResult(
                content="unattended task mutation requires task_id",
                is_error=True,
            )
        task = await self._store.get_task(task_id)
        if task.execution_mode == "user":
            return ToolResult(
                content="user-owned tasks are read-only in unattended runs",
                is_error=True,
            )
        if task.execution_mode == "joint" and task.waiting_on == "user":
            return ToolResult(
                content="joint task baton currently belongs to the user",
                is_error=True,
            )
        if self.name == "claim_durable_task":
            expected = args.get("expected_revision")
            if expected is None or expected != task.revision:
                recovery, fingerprint = self._claim_recovery(task, ctx)
                return ToolResult(
                    content=(
                        f"Claim rejected: expected revision {expected}; current revision "
                        f"is {task.revision}. {recovery}"
                    ),
                    is_error=True,
                    runtime_failure=ToolRuntimeFailure(
                        kind="state_conflict",
                        state_fingerprint=fingerprint,
                        recovery=recovery,
                    ),
                )
        return await self._tool.run(params, ctx)

    @staticmethod
    def _claim_recovery(task: DurableTask, ctx: ToolContext) -> tuple[str, str]:
        lease = task.lease
        held = ctx.session.active_task_leases.get(task.id)
        live = lease is not None and lease.expires_at > datetime.now(UTC)
        owns_lease = (
            live
            and lease is not None
            and held is not None
            and lease.holder_session_id == ctx.session.id
            and held.holder_session_id == ctx.session.id
            and held.id == lease.id
            and held.epoch == lease.epoch
        )
        if owns_lease:
            recovery = (
                "This session already holds the active task lease. Use "
                "update_durable_task_progress or complete_durable_task directly; "
                "do not claim again. Later task tool results supersede the dispatch snapshot."
            )
        elif live:
            recovery = (
                "The task has an active lease that this session does not hold. "
                "Do not mutate or repeatedly claim it; wait for the lease to be released "
                "or expire, then read the task again before attempting a claim."
            )
        elif task.status in {"completed", "cancelled"}:
            recovery = "The task is closed and cannot be claimed. Read its current state."
        else:
            recovery = (
                f"Read the task's current state before claiming with expected_revision "
                f"{task.revision}. An unattended claim requires the exact current revision."
            )
        # Include observed lease and local credential changes without exposing lease IDs.
        state = {
            "task_id": task.id,
            "revision": task.revision,
            "status": task.status,
            "lease": lease.model_dump(mode="json") if lease else None,
            "held": held.model_dump(mode="json") if held else None,
            "live": live,
            "owns_lease": owns_lease,
        }
        fingerprint = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
        return recovery, fingerprint

    def normalize_permission_args(
        self, args: dict[str, object], ctx: ToolContext
    ) -> dict[str, object]:
        hook = getattr(self._tool, "normalize_permission_args", None)
        return hook(args, ctx) if hook is not None else args

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        hook = getattr(self._tool, "summarize_permission", None)
        return hook(args, ctx) if hook is not None else f"coordinate durable task via {self.name}"

    def __getattr__(self, name: str) -> Any:
        """Preserve optional callable-contract declarations from the wrapped tool."""

        return getattr(self._tool, name)


class DurableTaskStateGuard:
    """Apply the durable-task ownership, baton, and revision contract."""

    id = "durable_task.lease"

    def __init__(self, store: DurableTaskStore | ScopedDurableTaskStore) -> None:
        self._store = store

    def wrap(self, tool: Tool) -> Tool:
        return GuardedDurableTaskTool(tool, self._store)  # type: ignore[return-value]
