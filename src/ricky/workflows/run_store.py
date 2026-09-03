"""Atomic JSON checkpoint storage for Workflow runs."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from ricky.config import RickySettings, user_data_path
from ricky.installation import fsync_directory
from ricky.profiles import ProfileScope
from ricky.workflows.run import WorkflowRun, utc_now

ReplaceOperation = Callable[[str | Path, str | Path], None]


class WorkflowRunStore:
    """Store validated runs below the configured user-global data root."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        project_root: Path | None = None,
        replace: ReplaceOperation = os.replace,
    ) -> None:
        self.settings = settings
        # ``project_root`` remains accepted for constructor compatibility. Run
        # provenance still distinguishes project and user workflow sources, but
        # checkpoints are user data regardless of where the bundle is authored.
        self.project_root = project_root
        self._replace = replace

    def root(self, scope: Literal["project", "user"]) -> Path:
        """Return the configured storage root for one run scope."""

        del scope
        return user_data_path(self.settings) / self.settings.workflow.run_dir

    def path(self, run_id: str, scope: Literal["project", "user"]) -> Path:
        """Return a validated path for one opaque run id."""

        if not run_id.startswith("workflow_") or not run_id.removeprefix("workflow_").isalnum():
            raise ValueError(f"invalid workflow run id: {run_id!r}")
        return self.root(scope) / f"{run_id}.json"

    async def save(self, run: WorkflowRun) -> None:
        """Write one atomic checkpoint without replacing a valid file on failure."""

        snapshot = run.model_copy(deep=True)
        await asyncio.to_thread(self._save_sync, snapshot)

    async def load(
        self,
        run_id: str,
        *,
        profile_scope: ProfileScope,
        scope: Literal["project", "user"] = "project",
    ) -> WorkflowRun:
        """Load and validate one checkpoint."""

        return await asyncio.to_thread(self._load_sync, run_id, scope, profile_scope)

    async def list_runs(
        self,
        *,
        profile_scope: ProfileScope,
        scope: Literal["project", "user"] = "project",
    ) -> list[WorkflowRun]:
        """Load valid checkpoints in newest-first order."""

        return await asyncio.to_thread(self._list_sync, scope, profile_scope)

    async def abandon(
        self,
        run_id: str,
        *,
        profile_scope: ProfileScope,
        scope: Literal["project", "user"] = "project",
    ) -> WorkflowRun:
        """Mark one non-completed run abandoned."""

        run = await self.load(run_id, scope=scope, profile_scope=profile_scope)
        if run.status in {"completed", "completed_with_errors"}:
            raise ValueError(f"cannot abandon completed workflow run {run_id!r}")
        if run.status == "abandoned":
            return run
        run.status = "abandoned"
        run.updated_at = run.finished_at = utc_now()
        await self.save(run)
        return run

    async def reconcile(
        self,
        run_id: str,
        execution_address: str,
        *,
        completed: bool,
        profile_scope: ProfileScope,
        scope: Literal["project", "user"] = "project",
    ) -> WorkflowRun:
        """Resolve one in-doubt effect from explicit external knowledge."""

        run = await self.load(run_id, scope=scope, profile_scope=profile_scope)
        entry = next(
            (
                value
                for value in run.effect_journal
                if value.execution_address == execution_address and value.status == "in_doubt"
            ),
            None,
        )
        if entry is None:
            raise ValueError(f"run has no in-doubt effect at {execution_address!r}")
        record = _record_for_address(run, execution_address)
        entry.status = "reconciled" if completed else "prepared"
        entry.result_summary = (
            "user confirmed the effect completed"
            if completed
            else "user confirmed the effect did not complete"
        )
        entry.finished_at = utc_now() if completed else None
        if completed:
            record.status = "completed"
            record.output = {"reconciled": True}
            record.error = None
            record.finished_at = utc_now()
        else:
            record.status = "pending"
            record.output = None
            record.error = None
            record.finished_at = None
        run.status = "pending"
        run.updated_at = utc_now()
        run.finished_at = None
        await self.save(run)
        return run

    def _save_sync(self, run: WorkflowRun) -> None:
        root = self.root(run.storage_scope)
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        target = self.path(run.id, run.storage_scope)
        temporary = root / f".{run.id}.{os.getpid()}.tmp"
        lock_path = root / f".{run.id}.lock"
        payload = run.model_dump_json(indent=2).encode("utf-8")
        try:
            with _advisory_lock(lock_path):
                with temporary.open("wb") as stream:
                    os.chmod(temporary, 0o600)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._replace(temporary, target)
                fsync_directory(root)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_sync(
        self,
        run_id: str,
        scope: Literal["project", "user"],
        profile_scope: ProfileScope,
    ) -> WorkflowRun:
        run = self._read_sync(run_id)
        if run.storage_scope != scope:
            raise ValueError(f"workflow run {run_id!r} has mismatched storage scope")
        if not profile_scope.permits(run.profile_scope.label()):
            raise ValueError(f"workflow run {run_id!r} is outside the active profile scope")
        return run

    def _read_sync(self, run_id: str) -> WorkflowRun:
        path = self.path(run_id, "project")
        try:
            payload = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"cannot read workflow run {run_id!r}: {exc}") from exc
        try:
            run = WorkflowRun.model_validate_json(payload)
        except ValueError as exc:
            raise ValueError(f"workflow run {run_id!r} is corrupt: {exc}") from exc
        if run.id != run_id:
            raise ValueError(f"workflow run {run_id!r} has mismatched identity")
        return run

    def _list_sync(
        self, scope: Literal["project", "user"], profile_scope: ProfileScope
    ) -> list[WorkflowRun]:
        root = self.root(scope)
        if not root.is_dir():
            return []
        runs: list[WorkflowRun] = []
        for path in root.glob("workflow_*.json"):
            run = self._read_sync(path.stem)
            if run.storage_scope == scope and profile_scope.permits(run.profile_scope.label()):
                runs.append(run)
        return sorted(runs, key=lambda run: run.updated_at, reverse=True)


@contextmanager
def _advisory_lock(path: Path):
    """Hold a process-level exclusive advisory lock for one run mutation."""

    import fcntl

    with path.open("a+b") as stream:
        os.fchmod(stream.fileno(), 0o600)
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _record_for_address(run: WorkflowRun, address: str):
    for record in run.steps.values():
        if record.execution_address == address:
            return record
    for items in run.item_runs.values():
        for item in items:
            for record in item.steps.values():
                if record.execution_address == address:
                    return record
    raise ValueError(f"run has no step record at {address!r}")
