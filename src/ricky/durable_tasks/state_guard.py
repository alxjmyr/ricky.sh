"""Subsystem-owned unattended safety guard for durable-task mutations."""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel

from ricky.durable_tasks.scoped import ScopedDurableTaskStore
from ricky.durable_tasks.store import DurableTaskStore
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
                return ToolResult(
                    content="unattended claims require the exact candidate expected_revision",
                    is_error=True,
                )
        return await self._tool.run(params, ctx)

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
