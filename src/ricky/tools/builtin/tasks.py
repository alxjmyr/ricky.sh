"""Task tracking tool."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ricky.agent.session import TaskItem, TaskStatus
from ricky.tools.base import Risk, ToolContext, ToolResult


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(
        default=None, description="Stable task id, if updating an existing task."
    )
    title: str = Field(description="Short task title.")
    status: TaskStatus = "pending"


class UpdateTasksParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tasks: list[TaskInput] = Field(description="Complete replacement task list.")


class UpdateTasksTool:
    name: ClassVar[str] = "update_tasks"
    description: ClassVar[str] = "Replace the session task list with current task statuses."
    Params: ClassVar[type[BaseModel]] = UpdateTasksParams
    risk: ClassVar[Risk] = "read_only"
    capability_id = "builtin.session.mutate"
    effect_kind = "ricky_state"
    unattended = "allowed"
    state_guard_id = None

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = UpdateTasksParams.model_validate(params)
        ctx.session.tasks = [
            TaskItem(id=task.id, title=task.title, status=task.status)
            if task.id is not None
            else TaskItem(title=task.title, status=task.status)
            for task in args.tasks
        ]
        return ToolResult(content=f"Updated {len(ctx.session.tasks)} task(s)")
