"""Profile-owned SQLite store for durable task coordination."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

from pydantic import JsonValue, ValidationError

from ricky.config import RickySettings, profile_data_path, user_data_path
from ricky.durable_tasks.types import (
    DurableTask,
    TaskActivity,
    TaskActivityKind,
    TaskArtifactEntry,
    TaskAuthority,
    TaskExecutionMode,
    TaskLease,
    TaskSearchQuery,
    TaskStatus,
    TaskTag,
    TaskWaitingOn,
    canonicalize_task_tags,
    validate_task_id,
)
from ricky.profiles import ProfileName

SCHEMA_VERSION = 2
_ACTIVE_STATUSES = {"open", "in_progress", "waiting", "blocked"}
_METADATA_CHAR_LIMIT = 16_000
_INITIALIZATION_LOCK = threading.Lock()


class TaskStoreError(RuntimeError):
    """Base error safe to surface without SQLite details."""


class TaskNotFoundError(TaskStoreError):
    """The task does not exist in this profile store."""


class TaskConflictError(TaskStoreError):
    """The task revision changed concurrently."""


class TaskLeaseError(TaskStoreError):
    """A lease is absent, expired, foreign, or stale."""


class TaskTransitionError(TaskStoreError):
    """The requested lifecycle transition is invalid."""


class TaskSchemaError(TaskStoreError):
    """The database schema is corrupt or unsupported."""


class DurableTaskStore:
    """Async-first application surface over one profile's SQLite database."""

    def __init__(
        self,
        *,
        user_root: Path,
        root: Path,
        profile: ProfileName,
        settings: RickySettings,
        clock: Callable[[], datetime],
    ) -> None:
        self.user_root = user_root
        self.root = root
        self.profile = profile
        self.db_path = root / "tasks.sqlite3"
        self.artifact_root = root / "artifacts"
        self._settings = settings.durable_tasks
        self._clock = clock

    @classmethod
    async def create(
        cls,
        settings: RickySettings,
        *,
        profile: ProfileName,
        clock: Callable[[], datetime] | None = None,
    ) -> DurableTaskStore:
        """Create a profile-owned store and initialize its schema."""

        user_root = user_data_path(settings)
        root = profile_data_path(settings, profile) / settings.durable_tasks.dir
        store = cls(
            user_root=user_root,
            root=root,
            profile=profile,
            settings=settings,
            clock=clock or (lambda: datetime.now(UTC)),
        )
        await store._run_blocking(store._initialize)
        return store

    async def create_task(
        self,
        *,
        title: str,
        objective: str,
        closure_criteria: str,
        execution_mode: TaskExecutionMode,
        authority: TaskAuthority,
        executor_id: str,
        session_id: str | None = None,
        priority: int = 0,
        due_at: datetime | None = None,
        tags: list[TaskTag] | None = None,
    ) -> DurableTask:
        return await self._run_blocking(
            self._create_task,
            title,
            objective,
            closure_criteria,
            execution_mode,
            authority,
            executor_id,
            session_id,
            priority,
            due_at,
            canonicalize_task_tags(tags or []),
        )

    async def update_tags(
        self,
        task_id: str,
        *,
        tags: list[TaskTag],
        lease: TaskLease,
        expected_revision: int,
        authority: TaskAuthority,
        executor_id: str,
    ) -> DurableTask:
        """Replace the canonical tag set under revision and lease fencing."""

        return await self._run_blocking(
            self._update_tags,
            validate_task_id(task_id),
            canonicalize_task_tags(tags),
            lease,
            expected_revision,
            authority,
            executor_id,
        )

    async def get_task(self, task_id: str) -> DurableTask:
        return await self._run_blocking(self._get_task, validate_task_id(task_id))

    @property
    def search_limit(self) -> int:
        """Maximum bounded page size accepted by structured task search."""

        return self._settings.search_limit

    async def search(self, query: TaskSearchQuery | None = None) -> list[DurableTask]:
        query = query or TaskSearchQuery(limit=self._settings.search_limit)
        if query.limit > self._settings.search_limit:
            raise ValueError(
                f"task search limit exceeds configured maximum: "
                f"{query.limit} > {self._settings.search_limit}"
            )
        return await self._run_blocking(self._search, query)

    async def activities(self, task_id: str, *, limit: int | None = None) -> list[TaskActivity]:
        actual_limit = limit or self._settings.activity_limit
        if actual_limit < 1 or actual_limit > self._settings.activity_limit:
            raise ValueError("activity limit is outside the configured range")
        return await self._run_blocking(self._activities, validate_task_id(task_id), actual_limit)

    async def claim(
        self,
        task_id: str,
        *,
        holder_session_id: str,
        authority: TaskAuthority,
        executor_id: str,
        expected_revision: int | None = None,
    ) -> DurableTask:
        return await self._run_blocking(
            self._claim,
            validate_task_id(task_id),
            holder_session_id,
            authority,
            executor_id,
            expected_revision,
        )

    async def renew(
        self,
        task_id: str,
        *,
        lease: TaskLease,
        expected_revision: int,
        authority: TaskAuthority,
        executor_id: str,
    ) -> DurableTask:
        return await self._run_blocking(
            self._renew,
            validate_task_id(task_id),
            lease,
            expected_revision,
            authority,
            executor_id,
        )

    async def progress(
        self,
        task_id: str,
        *,
        lease: TaskLease,
        expected_revision: int,
        current_summary: str,
        next_action: str | None,
        authority: TaskAuthority,
        executor_id: str,
        priority: int | None = None,
        due_at: datetime | None = None,
        clear_due_at: bool = False,
    ) -> DurableTask:
        return await self._run_blocking(
            self._transition,
            validate_task_id(task_id),
            lease,
            expected_revision,
            "progressed",
            "in_progress",
            current_summary,
            next_action,
            None,
            authority,
            executor_id,
            priority,
            due_at,
            clear_due_at,
        )

    async def wait(
        self,
        task_id: str,
        *,
        lease: TaskLease,
        expected_revision: int,
        waiting_on: TaskWaitingOn,
        current_summary: str,
        next_action: str,
        authority: TaskAuthority,
        executor_id: str,
        due_at: datetime | None = None,
        clear_due_at: bool = False,
    ) -> DurableTask:
        return await self._run_blocking(
            self._transition,
            validate_task_id(task_id),
            lease,
            expected_revision,
            "waiting",
            "waiting",
            current_summary,
            next_action,
            waiting_on,
            authority,
            executor_id,
            None,
            due_at,
            clear_due_at,
        )

    async def block(
        self,
        task_id: str,
        *,
        lease: TaskLease,
        expected_revision: int,
        current_summary: str,
        next_action: str,
        authority: TaskAuthority,
        executor_id: str,
    ) -> DurableTask:
        return await self._run_blocking(
            self._transition,
            validate_task_id(task_id),
            lease,
            expected_revision,
            "blocked",
            "blocked",
            current_summary,
            next_action,
            None,
            authority,
            executor_id,
            None,
            None,
            False,
        )

    async def complete(
        self,
        task_id: str,
        *,
        lease: TaskLease,
        expected_revision: int,
        completion_summary: str,
        authority: TaskAuthority,
        executor_id: str,
    ) -> DurableTask:
        return await self._run_blocking(
            self._terminal,
            validate_task_id(task_id),
            lease,
            expected_revision,
            "completed",
            completion_summary,
            authority,
            executor_id,
        )

    async def cancel(
        self,
        task_id: str,
        *,
        lease: TaskLease,
        expected_revision: int,
        reason: str,
        authority: TaskAuthority,
        executor_id: str,
    ) -> DurableTask:
        return await self._run_blocking(
            self._terminal,
            validate_task_id(task_id),
            lease,
            expected_revision,
            "cancelled",
            reason,
            authority,
            executor_id,
        )

    async def reopen(
        self,
        task_id: str,
        *,
        reason: str,
        authority: TaskAuthority,
        executor_id: str,
        session_id: str | None = None,
    ) -> DurableTask:
        return await self._run_blocking(
            self._reopen,
            validate_task_id(task_id),
            reason,
            authority,
            executor_id,
            session_id,
        )

    async def release(
        self,
        task_id: str,
        *,
        lease: TaskLease,
        expected_revision: int,
        authority: TaskAuthority,
        executor_id: str,
        summary: str = "Lease released",
    ) -> DurableTask:
        return await self._run_blocking(
            self._release,
            validate_task_id(task_id),
            lease,
            expected_revision,
            authority,
            executor_id,
            summary,
        )

    async def record_artifact_activity(
        self,
        task_id: str,
        *,
        lease: TaskLease,
        expected_revision: int,
        created: bool,
        path: str,
        sha256: str,
        size: int,
        authority: TaskAuthority,
        executor_id: str,
    ) -> DurableTask:
        return await self._run_blocking(
            self._record_artifact_activity,
            validate_task_id(task_id),
            lease,
            expected_revision,
            created,
            path,
            sha256,
            size,
            authority,
            executor_id,
        )

    async def mutate_artifact(
        self,
        task_id: str,
        *,
        lease: TaskLease,
        expected_revision: int,
        authority: TaskAuthority,
        executor_id: str,
        operation: Callable[[], tuple[TaskArtifactEntry, bool]],
    ) -> tuple[DurableTask, TaskArtifactEntry, bool]:
        """Fence a file mutation and its activity update in one write transaction."""

        return await self._run_blocking(
            self._mutate_artifact,
            validate_task_id(task_id),
            lease,
            expected_revision,
            authority,
            executor_id,
            operation,
        )

    async def release_session_leases(self, session_id: str) -> list[str]:
        """Best-effort graceful cleanup for every live lease held by a session."""

        return await self._run_blocking(self._release_session_leases, session_id)

    async def verify_lease(
        self, task_id: str, *, lease: TaskLease, expected_revision: int
    ) -> DurableTask:
        """Validate a lease/revision without mutating task state."""

        return await self._run_blocking(
            self._verify_lease, validate_task_id(task_id), lease, expected_revision
        )

    def _initialize(self) -> None:
        from ricky.durable_tasks.upgrade import (
            create_current_durable_tasks_database,
            inspect_durable_tasks_database,
        )

        # Re-assert the private data roots before either branch so a creation
        # never widens an intermediate directory and an existing store heals a
        # mode that was loosened outside Ricky.
        _private_dir(self.user_root)
        current = self.user_root
        for part in self.root.relative_to(self.user_root).parts:
            current /= part
            _private_dir(current)
        # Runtime composition can initialize the same profile from concurrent
        # conversations. Keep the absent -> current transition atomic inside
        # this process so no observer mistakes the exclusively-created empty
        # SQLite file for an unsupported version-0 database.
        with _INITIALIZATION_LOCK:
            if self.db_path.exists():
                inspection = inspect_durable_tasks_database(self.db_path)
                if inspection.state != "current":
                    raise TaskSchemaError(inspection.detail)
            else:
                create_current_durable_tasks_database(self.db_path)
        _private_dir(self.artifact_root)
        if os.name == "posix":
            for path in self.root.glob("tasks.sqlite3*"):
                if path.is_file():
                    path.chmod(0o600)

    async def _run_blocking[T](self, operation: Callable[..., T], *args: object) -> T:
        """Run one SQLite operation to completion, even when its waiter is cancelled."""

        worker = asyncio.create_task(asyncio.to_thread(operation, *args))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.shield(worker)
            raise
        except sqlite3.Error as exc:
            raise TaskStoreError("durable task store operation failed") from exc

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self.db_path,
                timeout=self._settings.sqlite_busy_timeout_ms / 1000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {self._settings.sqlite_busy_timeout_ms}")
            return connection
        except sqlite3.Error as exc:
            raise TaskStoreError("unable to open durable task store") from exc

    def _create_task(
        self,
        title: str,
        objective: str,
        closure_criteria: str,
        execution_mode: TaskExecutionMode,
        authority: TaskAuthority,
        executor_id: str,
        session_id: str | None,
        priority: int,
        due_at: datetime | None,
        tags: list[TaskTag],
    ) -> DurableTask:
        now = self._now()
        task = DurableTask(
            id=f"task_{uuid4().hex}",
            profile=self.profile,
            title=title,
            objective=objective,
            closure_criteria=closure_criteria,
            execution_mode=execution_mode,
            status="open",
            priority=priority,
            due_at=due_at,
            tags=tags,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO tasks (
                    id, title, objective, closure_criteria, execution_mode, status,
                    waiting_on, current_summary, next_action, priority, due_at,
                    completion_summary, revision, created_at, updated_at, completed_at,
                    cancelled_at, lease_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, NULL, 1, ?, ?, NULL,
                          NULL, 0)""",
                (
                    task.id,
                    task.title,
                    task.objective,
                    task.closure_criteria,
                    task.execution_mode,
                    task.status,
                    task.priority,
                    _dump_dt(task.due_at),
                    _dump_dt(now),
                    _dump_dt(now),
                ),
            )
            self._insert_activity(
                connection,
                task_id=task.id,
                kind="created",
                authority=authority,
                executor_id=executor_id,
                session_id=session_id,
                from_status=None,
                to_status="open",
                summary="Durable task created",
                metadata={},
                revision=1,
                now=now,
            )
            connection.executemany(
                "INSERT INTO task_tags (task_id, tag) VALUES (?, ?)",
                [(task.id, tag) for tag in task.tags],
            )
        return task

    def _get_task(self, task_id: str) -> DurableTask:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFoundError(f"durable task not found: {task_id}")
        return self._task_from_row(row)

    def _search(self, query: TaskSearchQuery) -> list[DurableTask]:
        clauses: list[str] = []
        params: list[object] = []
        if not query.include_closed and not query.statuses:
            clauses.append("status NOT IN ('completed', 'cancelled')")
        if query.statuses:
            clauses.append(f"status IN ({_placeholders(len(query.statuses))})")
            params.extend(query.statuses)
        if query.execution_modes:
            clauses.append(f"execution_mode IN ({_placeholders(len(query.execution_modes))})")
            params.extend(query.execution_modes)
        if query.waiting_on:
            clauses.append(f"waiting_on IN ({_placeholders(len(query.waiting_on))})")
            params.extend(query.waiting_on)
        if query.tags_any:
            clauses.append(
                f"EXISTS (SELECT 1 FROM task_tags ta WHERE ta.task_id = tasks.id "
                f"AND ta.tag IN ({_placeholders(len(query.tags_any))}))"
            )
            params.extend(query.tags_any)
        if query.tags_all:
            clauses.append(
                f"(SELECT COUNT(DISTINCT ta.tag) FROM task_tags ta WHERE ta.task_id = tasks.id "
                f"AND ta.tag IN ({_placeholders(len(query.tags_all))})) = ?"
            )
            params.extend(query.tags_all)
            params.append(len(query.tags_all))
        if query.tags_none:
            clauses.append(
                f"NOT EXISTS (SELECT 1 FROM task_tags ta WHERE ta.task_id = tasks.id "
                f"AND ta.tag IN ({_placeholders(len(query.tags_none))}))"
            )
            params.extend(query.tags_none)
        if query.due_before is not None:
            clauses.append("due_at IS NOT NULL AND due_at <= ?")
            params.append(_dump_dt(query.due_before))
        if query.text:
            clauses.append(
                """lower(title || ' ' || objective || ' ' || closure_criteria || ' ' ||
                   coalesce(current_summary, '') || ' ' || coalesce(next_action, ''))
                   LIKE ? ESCAPE '\\'"""
            )
            escaped = query.text.casefold().replace("\\", "\\\\").replace("%", "\\%")
            escaped = escaped.replace("_", "\\_")
            params.append(f"%{escaped}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend((query.limit, query.offset))
        sql = f"""SELECT * FROM tasks {where}
            ORDER BY priority DESC, due_at IS NULL ASC, due_at ASC,
                     updated_at DESC, id ASC LIMIT ? OFFSET ?"""
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._task_from_row(row) for row in rows]

    def _update_tags(
        self,
        task_id: str,
        tags: list[TaskTag],
        lease: TaskLease,
        expected_revision: int,
        authority: TaskAuthority,
        executor_id: str,
    ) -> DurableTask:
        now = self._now()
        with self._transaction() as connection:
            row = self._required_leased_row(connection, task_id, lease, expected_revision, now)
            before = [
                str(item[0])
                for item in connection.execute(
                    "SELECT tag FROM task_tags WHERE task_id = ? ORDER BY tag", (task_id,)
                )
            ]
            after = list(tags)
            if before == after:
                return self._task_from_row(row, connection=connection)
            revision = expected_revision + 1
            cursor = connection.execute(
                "UPDATE tasks SET revision = ?, updated_at = ? WHERE id = ? AND revision = ?",
                (revision, _dump_dt(now), task_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise TaskConflictError(f"durable task revision changed: {task_id}")
            connection.execute("DELETE FROM task_tags WHERE task_id = ?", (task_id,))
            connection.executemany(
                "INSERT INTO task_tags (task_id, tag) VALUES (?, ?)",
                [(task_id, tag) for tag in after],
            )
            self._insert_activity(
                connection,
                task_id=task_id,
                kind="tags_updated",
                authority=authority,
                executor_id=executor_id,
                session_id=lease.holder_session_id,
                from_status=row["status"],
                to_status=row["status"],
                summary="Durable task tags updated",
                metadata={
                    "before": cast(JsonValue, before),
                    "after": cast(JsonValue, after),
                },
                revision=revision,
                now=now,
            )
            updated = self._required_row(connection, task_id)
            return self._task_from_row(updated, connection=connection)

    def _activities(self, task_id: str, limit: int) -> list[TaskActivity]:
        with self._connect() as connection:
            exists = connection.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if exists is None:
                raise TaskNotFoundError(f"durable task not found: {task_id}")
            rows = connection.execute(
                """SELECT * FROM task_activity WHERE task_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (task_id, limit),
            ).fetchall()
        return [self._activity_from_row(row) for row in rows]

    def _claim(
        self,
        task_id: str,
        holder_session_id: str,
        authority: TaskAuthority,
        executor_id: str,
        expected_revision: int | None,
    ) -> DurableTask:
        now = self._now()
        expires_at = now + timedelta(seconds=self._settings.lease_seconds)
        with self._transaction() as connection:
            row = self._required_row(connection, task_id)
            if expected_revision is not None and row["revision"] != expected_revision:
                raise TaskConflictError(f"durable task revision changed: {task_id}")
            if row["status"] not in _ACTIVE_STATUSES:
                raise TaskTransitionError(f"cannot claim {row['status']} durable task: {task_id}")
            live = self._live_lease(row, now)
            if live is not None:
                raise TaskLeaseError(
                    f"task is leased by {live.holder_session_id} until "
                    f"{live.expires_at.isoformat()}"
                )
            revision = int(row["revision"]) + 1
            epoch = int(row["lease_epoch"]) + 1
            lease_id = f"lease_{uuid4().hex}"
            old_status = cast(TaskStatus, row["status"])
            new_status: TaskStatus = "in_progress" if old_status == "open" else old_status
            if row["lease_id"] is not None:
                self._insert_activity(
                    connection,
                    task_id=task_id,
                    kind="lease_expired",
                    authority="system_recovery",
                    executor_id="durable_task_store",
                    session_id=None,
                    from_status=old_status,
                    to_status=old_status,
                    summary="Expired task lease recovered",
                    metadata={"previous_holder": row["lease_holder_session_id"]},
                    revision=revision,
                    now=now,
                )
            connection.execute(
                """UPDATE tasks SET status = ?, revision = ?, updated_at = ?,
                   lease_id = ?, lease_holder_session_id = ?, lease_epoch = ?,
                   lease_acquired_at = ?, lease_expires_at = ? WHERE id = ?""",
                (
                    new_status,
                    revision,
                    _dump_dt(now),
                    lease_id,
                    holder_session_id,
                    epoch,
                    _dump_dt(now),
                    _dump_dt(expires_at),
                    task_id,
                ),
            )
            self._insert_activity(
                connection,
                task_id=task_id,
                kind="claimed",
                authority=authority,
                executor_id=executor_id,
                session_id=holder_session_id,
                from_status=old_status,
                to_status=new_status,
                summary="Durable task claimed",
                metadata={"lease_epoch": epoch},
                revision=revision,
                now=now,
            )
            updated = self._required_row(connection, task_id)
        return self._task_from_row(updated)

    def _renew(
        self,
        task_id: str,
        lease: TaskLease,
        expected_revision: int,
        authority: TaskAuthority,
        executor_id: str,
    ) -> DurableTask:
        now = self._now()
        expires_at = now + timedelta(seconds=self._settings.lease_seconds)
        with self._transaction() as connection:
            row = self._required_leased_row(connection, task_id, lease, expected_revision, now)
            revision = expected_revision + 1
            connection.execute(
                "UPDATE tasks SET revision = ?, updated_at = ?, lease_expires_at = ? WHERE id = ?",
                (revision, _dump_dt(now), _dump_dt(expires_at), task_id),
            )
            status = cast(TaskStatus, row["status"])
            self._insert_activity(
                connection,
                task_id=task_id,
                kind="lease_renewed",
                authority=authority,
                executor_id=executor_id,
                session_id=lease.holder_session_id,
                from_status=status,
                to_status=status,
                summary="Durable task lease renewed",
                metadata={"lease_epoch": lease.epoch},
                revision=revision,
                now=now,
            )
            updated = self._required_row(connection, task_id)
        return self._task_from_row(updated)

    def _transition(
        self,
        task_id: str,
        lease: TaskLease,
        expected_revision: int,
        kind: TaskActivityKind,
        new_status: TaskStatus,
        current_summary: str,
        next_action: str | None,
        waiting_on: TaskWaitingOn | None,
        authority: TaskAuthority,
        executor_id: str,
        priority: int | None,
        due_at: datetime | None,
        clear_due_at: bool,
    ) -> DurableTask:
        now = self._now()
        current_summary = _nonempty(current_summary, "current_summary")
        if new_status in {"waiting", "blocked"}:
            next_action = _nonempty(next_action, "next_action")
        with self._transaction() as connection:
            row = self._required_leased_row(connection, task_id, lease, expected_revision, now)
            old_status = cast(TaskStatus, row["status"])
            if old_status not in _ACTIVE_STATUSES:
                raise TaskTransitionError(f"cannot advance {old_status} durable task")
            revision = expected_revision + 1
            new_priority = int(row["priority"]) if priority is None else priority
            new_due = None if clear_due_at else (due_at or _parse_dt(row["due_at"]))
            candidate = self._candidate_from_row(
                row,
                status=new_status,
                waiting_on=waiting_on,
                current_summary=current_summary,
                next_action=next_action,
                priority=new_priority,
                due_at=new_due,
                revision=revision,
                updated_at=now,
            )
            connection.execute(
                """UPDATE tasks SET status = ?, waiting_on = ?, current_summary = ?,
                   next_action = ?, priority = ?, due_at = ?, revision = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    candidate.status,
                    candidate.waiting_on,
                    candidate.current_summary,
                    candidate.next_action,
                    candidate.priority,
                    _dump_dt(candidate.due_at),
                    revision,
                    _dump_dt(now),
                    task_id,
                ),
            )
            self._insert_activity(
                connection,
                task_id=task_id,
                kind=kind,
                authority=authority,
                executor_id=executor_id,
                session_id=lease.holder_session_id,
                from_status=old_status,
                to_status=new_status,
                summary=current_summary,
                metadata={"next_action": next_action, "waiting_on": waiting_on},
                revision=revision,
                now=now,
            )
            updated = self._required_row(connection, task_id)
        return self._task_from_row(updated)

    def _terminal(
        self,
        task_id: str,
        lease: TaskLease,
        expected_revision: int,
        new_status: TaskStatus,
        summary: str,
        authority: TaskAuthority,
        executor_id: str,
    ) -> DurableTask:
        now = self._now()
        summary = _nonempty(summary, "summary")
        with self._transaction() as connection:
            row = self._required_leased_row(connection, task_id, lease, expected_revision, now)
            old_status = cast(TaskStatus, row["status"])
            if old_status not in _ACTIVE_STATUSES:
                raise TaskTransitionError(f"cannot close {old_status} durable task")
            revision = expected_revision + 1
            completion_summary = summary if new_status == "completed" else None
            completed_at = now if new_status == "completed" else None
            cancelled_at = now if new_status == "cancelled" else None
            connection.execute(
                """UPDATE tasks SET status = ?, waiting_on = NULL, next_action = NULL,
                   completion_summary = ?, completed_at = ?, cancelled_at = ?,
                   revision = ?, updated_at = ?, lease_id = NULL,
                   lease_holder_session_id = NULL, lease_acquired_at = NULL,
                   lease_expires_at = NULL WHERE id = ?""",
                (
                    new_status,
                    completion_summary,
                    _dump_dt(completed_at),
                    _dump_dt(cancelled_at),
                    revision,
                    _dump_dt(now),
                    task_id,
                ),
            )
            kind: TaskActivityKind = "completed" if new_status == "completed" else "cancelled"
            self._insert_activity(
                connection,
                task_id=task_id,
                kind=kind,
                authority=authority,
                executor_id=executor_id,
                session_id=lease.holder_session_id,
                from_status=old_status,
                to_status=new_status,
                summary=summary,
                metadata={},
                revision=revision,
                now=now,
            )
            updated = self._required_row(connection, task_id)
        return self._task_from_row(updated)

    def _reopen(
        self,
        task_id: str,
        reason: str,
        authority: TaskAuthority,
        executor_id: str,
        session_id: str | None,
    ) -> DurableTask:
        now = self._now()
        reason = _nonempty(reason, "reason")
        with self._transaction() as connection:
            row = self._required_row(connection, task_id)
            old_status = cast(TaskStatus, row["status"])
            if old_status not in {"completed", "cancelled"}:
                raise TaskTransitionError("only completed or cancelled tasks can be reopened")
            live = self._live_lease(row, now)
            if live is not None:
                raise TaskLeaseError(
                    f"task is leased by {live.holder_session_id} until "
                    f"{live.expires_at.isoformat()}"
                )
            revision = int(row["revision"]) + 1
            connection.execute(
                """UPDATE tasks SET status = 'open', waiting_on = NULL,
                   completion_summary = NULL, completed_at = NULL, cancelled_at = NULL,
                   revision = ?, updated_at = ?, lease_id = NULL,
                   lease_holder_session_id = NULL, lease_acquired_at = NULL,
                   lease_expires_at = NULL WHERE id = ?""",
                (revision, _dump_dt(now), task_id),
            )
            self._insert_activity(
                connection,
                task_id=task_id,
                kind="reopened",
                authority=authority,
                executor_id=executor_id,
                session_id=session_id,
                from_status=old_status,
                to_status="open",
                summary=reason,
                metadata={},
                revision=revision,
                now=now,
            )
            updated = self._required_row(connection, task_id)
        return self._task_from_row(updated)

    def _release(
        self,
        task_id: str,
        lease: TaskLease,
        expected_revision: int,
        authority: TaskAuthority,
        executor_id: str,
        summary: str,
    ) -> DurableTask:
        now = self._now()
        summary = _nonempty(summary, "summary")
        with self._transaction() as connection:
            row = self._required_leased_row(connection, task_id, lease, expected_revision, now)
            revision = expected_revision + 1
            connection.execute(
                """UPDATE tasks SET revision = ?, updated_at = ?, lease_id = NULL,
                   lease_holder_session_id = NULL, lease_acquired_at = NULL,
                   lease_expires_at = NULL WHERE id = ?""",
                (revision, _dump_dt(now), task_id),
            )
            status = cast(TaskStatus, row["status"])
            self._insert_activity(
                connection,
                task_id=task_id,
                kind="released",
                authority=authority,
                executor_id=executor_id,
                session_id=lease.holder_session_id,
                from_status=status,
                to_status=status,
                summary=summary,
                metadata={"lease_epoch": lease.epoch},
                revision=revision,
                now=now,
            )
            updated = self._required_row(connection, task_id)
        return self._task_from_row(updated)

    def _record_artifact_activity(
        self,
        task_id: str,
        lease: TaskLease,
        expected_revision: int,
        created: bool,
        path: str,
        sha256: str,
        size: int,
        authority: TaskAuthority,
        executor_id: str,
    ) -> DurableTask:
        now = self._now()
        with self._transaction() as connection:
            row = self._required_leased_row(connection, task_id, lease, expected_revision, now)
            revision = expected_revision + 1
            connection.execute(
                "UPDATE tasks SET revision = ?, updated_at = ? WHERE id = ?",
                (revision, _dump_dt(now), task_id),
            )
            status = cast(TaskStatus, row["status"])
            kind: TaskActivityKind = "artifact_created" if created else "artifact_updated"
            self._insert_activity(
                connection,
                task_id=task_id,
                kind=kind,
                authority=authority,
                executor_id=executor_id,
                session_id=lease.holder_session_id,
                from_status=status,
                to_status=status,
                summary=f"Artifact {'created' if created else 'updated'}: {path}",
                metadata={"path": path, "sha256": sha256, "size": size},
                revision=revision,
                now=now,
            )
            updated = self._required_row(connection, task_id)
        return self._task_from_row(updated)

    def _mutate_artifact(
        self,
        task_id: str,
        lease: TaskLease,
        expected_revision: int,
        authority: TaskAuthority,
        executor_id: str,
        operation: Callable[[], tuple[TaskArtifactEntry, bool]],
    ) -> tuple[DurableTask, TaskArtifactEntry, bool]:
        """Keep lease/revision fencing live until the file and audit both settle."""

        now = self._now()
        with self._transaction() as connection:
            row = self._required_leased_row(connection, task_id, lease, expected_revision, now)
            entry, created = operation()
            revision = expected_revision + 1
            connection.execute(
                "UPDATE tasks SET revision = ?, updated_at = ? WHERE id = ?",
                (revision, _dump_dt(now), task_id),
            )
            status = cast(TaskStatus, row["status"])
            kind: TaskActivityKind = "artifact_created" if created else "artifact_updated"
            self._insert_activity(
                connection,
                task_id=task_id,
                kind=kind,
                authority=authority,
                executor_id=executor_id,
                session_id=lease.holder_session_id,
                from_status=status,
                to_status=status,
                summary=f"Artifact {'created' if created else 'updated'}: {entry.path}",
                metadata={"path": entry.path, "sha256": entry.sha256, "size": entry.size},
                revision=revision,
                now=now,
            )
            updated = self._required_row(connection, task_id)
        return self._task_from_row(updated), entry, created

    def _release_session_leases(self, session_id: str) -> list[str]:
        now = self._now()
        released: list[str] = []
        with self._transaction() as connection:
            rows = connection.execute(
                """SELECT * FROM tasks WHERE lease_holder_session_id = ?
                   AND lease_expires_at > ?""",
                (session_id, _dump_dt(now)),
            ).fetchall()
            for row in rows:
                revision = int(row["revision"]) + 1
                task_id = cast(str, row["id"])
                connection.execute(
                    """UPDATE tasks SET revision = ?, updated_at = ?, lease_id = NULL,
                       lease_holder_session_id = NULL, lease_acquired_at = NULL,
                       lease_expires_at = NULL WHERE id = ?""",
                    (revision, _dump_dt(now), task_id),
                )
                status = cast(TaskStatus, row["status"])
                self._insert_activity(
                    connection,
                    task_id=task_id,
                    kind="released",
                    authority="system_recovery",
                    executor_id="session_cleanup",
                    session_id=session_id,
                    from_status=status,
                    to_status=status,
                    summary="Lease released during session cleanup",
                    metadata={"lease_epoch": int(row["lease_epoch"])},
                    revision=revision,
                    now=now,
                )
                released.append(task_id)
        return released

    def _verify_lease(self, task_id: str, lease: TaskLease, expected_revision: int) -> DurableTask:
        now = self._now()
        with self._connect() as connection:
            row = self._required_leased_row(connection, task_id, lease, expected_revision, now)
            return self._task_from_row(row)

    def _required_row(self, connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFoundError(f"durable task not found: {task_id}")
        return row

    def _required_leased_row(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        lease: TaskLease,
        expected_revision: int,
        now: datetime,
    ) -> sqlite3.Row:
        row = self._required_row(connection, task_id)
        actual_revision = int(row["revision"])
        if actual_revision != expected_revision:
            raise TaskConflictError(
                f"task revision conflict: expected {expected_revision}, current {actual_revision}"
            )
        if row["lease_id"] is None:
            raise TaskLeaseError("task has no active lease")
        if row["lease_holder_session_id"] != lease.holder_session_id:
            raise TaskLeaseError(f"task lease belongs to session {row['lease_holder_session_id']}")
        if row["lease_id"] != lease.id:
            raise TaskLeaseError("task lease id is stale")
        if int(row["lease_epoch"]) != lease.epoch:
            raise TaskLeaseError("task lease epoch is stale")
        expires_at = _parse_dt(row["lease_expires_at"])
        if expires_at is None or expires_at <= now:
            raise TaskLeaseError("task lease has expired")
        return row

    def _live_lease(self, row: sqlite3.Row, now: datetime) -> TaskLease | None:
        if row["lease_id"] is None:
            return None
        expires_at = _parse_dt(row["lease_expires_at"])
        if expires_at is None or expires_at <= now:
            return None
        return TaskLease(
            id=row["lease_id"],
            holder_session_id=row["lease_holder_session_id"],
            epoch=row["lease_epoch"],
            acquired_at=_required_dt(row["lease_acquired_at"]),
            expires_at=expires_at,
        )

    def _task_from_row(
        self, row: sqlite3.Row, *, connection: sqlite3.Connection | None = None
    ) -> DurableTask:
        lease = None
        if row["lease_id"] is not None:
            lease = TaskLease(
                id=row["lease_id"],
                holder_session_id=row["lease_holder_session_id"],
                epoch=row["lease_epoch"],
                acquired_at=_required_dt(row["lease_acquired_at"]),
                expires_at=_required_dt(row["lease_expires_at"]),
            )
        owned = connection is None
        connection = connection or self._connect()
        try:
            tags = [
                str(item[0])
                for item in connection.execute(
                    "SELECT tag FROM task_tags WHERE task_id = ? ORDER BY tag", (row["id"],)
                )
            ]
            return DurableTask(
                id=row["id"],
                profile=self.profile,
                title=row["title"],
                objective=row["objective"],
                closure_criteria=row["closure_criteria"],
                execution_mode=row["execution_mode"],
                status=row["status"],
                waiting_on=row["waiting_on"],
                current_summary=row["current_summary"],
                next_action=row["next_action"],
                priority=row["priority"],
                tags=tags,
                due_at=_parse_dt(row["due_at"]),
                completion_summary=row["completion_summary"],
                revision=row["revision"],
                created_at=_required_dt(row["created_at"]),
                updated_at=_required_dt(row["updated_at"]),
                completed_at=_parse_dt(row["completed_at"]),
                cancelled_at=_parse_dt(row["cancelled_at"]),
                lease=lease,
            )
        except (ValidationError, ValueError) as exc:
            raise TaskSchemaError("durable task store contains an invalid task row") from exc
        finally:
            if owned:
                connection.close()

    def _candidate_from_row(self, row: sqlite3.Row, **updates: object) -> DurableTask:
        values = self._task_from_row(row).model_dump()
        values.update(updates)
        return DurableTask.model_validate(values)

    def _activity_from_row(self, row: sqlite3.Row) -> TaskActivity:
        try:
            metadata = json.loads(row["metadata_json"])
            return TaskActivity(
                id=row["id"],
                task_id=row["task_id"],
                profile=self.profile,
                kind=row["kind"],
                authority=row["authority"],
                executor_id=row["executor_id"],
                session_id=row["session_id"],
                from_status=row["from_status"],
                to_status=row["to_status"],
                summary=row["summary"],
                metadata=metadata,
                task_revision=row["task_revision"],
                created_at=_required_dt(row["created_at"]),
            )
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise TaskSchemaError("durable task store contains invalid activity") from exc

    def _insert_activity(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        kind: TaskActivityKind,
        authority: TaskAuthority,
        executor_id: str,
        session_id: str | None,
        from_status: TaskStatus | None,
        to_status: TaskStatus | None,
        summary: str,
        metadata: dict[str, JsonValue],
        revision: int,
        now: datetime,
    ) -> None:
        if len(summary) > 4_000:
            raise TaskStoreError("durable task activity summary exceeds its limit")
        metadata_json = json.dumps(metadata, allow_nan=False, separators=(",", ":"))
        if len(metadata_json) > _METADATA_CHAR_LIMIT:
            raise TaskStoreError("durable task activity metadata exceeds its limit")
        connection.execute(
            """INSERT INTO task_activity (
                task_id, kind, authority, executor_id, session_id, from_status,
                to_status, summary, metadata_json, task_revision, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                kind,
                authority,
                _nonempty(executor_id, "executor_id"),
                session_id,
                from_status,
                to_status,
                _nonempty(summary, "summary"),
                metadata_json,
                revision,
                _dump_dt(now),
            ),
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise TaskStoreError("durable task clock must return aware UTC time")
        return now.astimezone(UTC)

    def _transaction(self) -> _Transaction:
        return _Transaction(self)


class _Transaction:
    def __init__(self, store: DurableTaskStore) -> None:
        self._store = store
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        connection = self._store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            connection.close()
            raise TaskStoreError("unable to begin durable task transaction") from exc
        self._connection = connection
        return connection

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, traceback
        assert self._connection is not None
        try:
            if exc is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        except sqlite3.Error as sqlite_error:
            raise TaskStoreError("durable task transaction failed") from sqlite_error
        finally:
            self._connection.close()


def _private_dir(path: Path) -> None:
    if path.is_symlink():
        raise TaskStoreError("durable task data roots cannot be symlinks")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise TaskStoreError("durable task data roots cannot be symlinks")
    if os.name == "posix":
        path.chmod(0o700)


def _nonempty(value: str | None, name: str) -> str:
    if value is None or not value.strip():
        raise ValueError(f"{name} must be non-empty")
    return value.strip()


def _dump_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored timestamp is not timezone-aware")
    return parsed.astimezone(UTC)


def _required_dt(value: str | None) -> datetime:
    parsed = _parse_dt(value)
    if parsed is None:
        raise ValueError("required stored timestamp is missing")
    return parsed


def _placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL CHECK(length(trim(title)) > 0),
    objective TEXT NOT NULL CHECK(length(trim(objective)) > 0),
    closure_criteria TEXT NOT NULL CHECK(length(trim(closure_criteria)) > 0),
    execution_mode TEXT NOT NULL CHECK(execution_mode IN ('agent','joint','user')),
    status TEXT NOT NULL CHECK(status IN
        ('open','in_progress','waiting','blocked','completed','cancelled')),
    waiting_on TEXT CHECK(waiting_on IN ('agent','user','external','time')),
    current_summary TEXT,
    next_action TEXT,
    priority INTEGER NOT NULL DEFAULT 0 CHECK(priority BETWEEN -100 AND 100),
    due_at TEXT,
    completion_summary TEXT,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    cancelled_at TEXT,
    lease_id TEXT,
    lease_holder_session_id TEXT,
    lease_epoch INTEGER NOT NULL DEFAULT 0 CHECK(lease_epoch >= 0),
    lease_acquired_at TEXT,
    lease_expires_at TEXT,
    CHECK((status = 'waiting' AND waiting_on IS NOT NULL AND next_action IS NOT NULL)
       OR (status <> 'waiting' AND waiting_on IS NULL)),
    CHECK((status = 'completed' AND completion_summary IS NOT NULL AND completed_at IS NOT NULL)
       OR (status <> 'completed' AND completion_summary IS NULL AND completed_at IS NULL)),
    CHECK((status = 'cancelled' AND cancelled_at IS NOT NULL)
       OR (status <> 'cancelled' AND cancelled_at IS NULL)),
    CHECK((lease_id IS NULL AND lease_holder_session_id IS NULL
           AND lease_acquired_at IS NULL AND lease_expires_at IS NULL)
       OR (lease_id IS NOT NULL AND lease_holder_session_id IS NOT NULL
           AND lease_epoch >= 1 AND lease_acquired_at IS NOT NULL
           AND lease_expires_at IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS task_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    kind TEXT NOT NULL CHECK(kind IN
        ('created','claimed','lease_renewed','progressed','waiting','blocked',
         'completed','cancelled','reopened','released','lease_expired',
         'artifact_created','artifact_updated','tags_updated')),
    authority TEXT NOT NULL CHECK(authority IN
        ('agent_autonomy','joint_work','direct_user_instruction',
         'deterministic_user_command','system_recovery')),
    executor_id TEXT NOT NULL,
    session_id TEXT,
    from_status TEXT,
    to_status TEXT,
    summary TEXT NOT NULL CHECK(length(trim(summary)) > 0),
    metadata_json TEXT NOT NULL,
    task_revision INTEGER NOT NULL CHECK(task_revision >= 1),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_status_updated_idx ON tasks(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS tasks_mode_status_idx ON tasks(execution_mode, status);
CREATE INDEX IF NOT EXISTS tasks_due_idx ON tasks(due_at);
CREATE INDEX IF NOT EXISTS tasks_lease_expiry_idx ON tasks(lease_expires_at);
CREATE INDEX IF NOT EXISTS activity_task_id_idx ON task_activity(task_id, id DESC);
CREATE TABLE IF NOT EXISTS task_tags (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (task_id, tag)
);
CREATE INDEX IF NOT EXISTS task_tags_tag_task_idx ON task_tags(tag, task_id);
"""
