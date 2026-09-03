"""Plain-text durable-task rendering shared by tools and interfaces."""

from __future__ import annotations

from ricky.durable_tasks.types import DurableTask, TaskActivity, TaskArtifactEntry


def render_task(task: DurableTask) -> str:
    """Render every user-relevant coordination field."""

    lease = (
        f"{task.lease.holder_session_id} until {task.lease.expires_at.isoformat()} "
        f"(epoch {task.lease.epoch})"
        if task.lease is not None
        else "(none)"
    )
    return "\n".join(
        [
            f"# {task.title}",
            f"id: {task.id}",
            f"profile: {task.profile}",
            f"mode: {task.execution_mode}",
            f"status: {task.status}",
            f"priority: {task.priority}",
            f"tags: {', '.join(task.tags) if task.tags else '(none)'}",
            f"objective: {task.objective}",
            f"closure criteria: {task.closure_criteria}",
            f"current summary: {task.current_summary or '(none)'}",
            f"next action: {task.next_action or '(none)'}",
            f"waiting on: {task.waiting_on or '(none)'}",
            f"due at: {task.due_at.isoformat() if task.due_at else '(none)'}",
            f"completion summary: {task.completion_summary or '(none)'}",
            f"revision: {task.revision}",
            f"created at: {task.created_at.isoformat()}",
            f"updated at: {task.updated_at.isoformat()}",
            f"completed at: {task.completed_at.isoformat() if task.completed_at else '(none)'}",
            f"cancelled at: {task.cancelled_at.isoformat() if task.cancelled_at else '(none)'}",
            f"lease: {lease}",
        ]
    )


def render_task_list(tasks: list[DurableTask]) -> str:
    if not tasks:
        return "[no matching durable tasks]"
    return "\n".join(
        f"{task.id}  [{task.profile}:{task.status}/{task.execution_mode}]  p={task.priority}  "
        f"{task.title}  next={task.next_action or '(none)'}"
        for task in tasks
    )


def render_activity(activity: list[TaskActivity]) -> str:
    if not activity:
        return "[no task activity]"
    return "\n".join(
        f"{item.id}  [{item.profile}] {item.created_at.isoformat()}  {item.kind}  "
        f"{item.from_status or '-'}->{item.to_status or '-'}  "
        f"authority={item.authority} executor={item.executor_id}  {item.summary}"
        for item in activity
    )


def render_artifacts(entries: list[TaskArtifactEntry]) -> str:
    if not entries:
        return "[no task artifacts]"
    return "\n".join(
        f"{entry.path}  {entry.size} bytes  sha256={entry.sha256}  "
        f"modified={entry.modified_at.isoformat()}"
        for entry in entries
    )
