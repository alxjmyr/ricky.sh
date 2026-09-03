"""Confined human-readable artifact workspaces for durable tasks."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict

from ricky.config import RickySettings, profile_data_path
from ricky.durable_tasks.store import DurableTaskStore, TaskStoreError
from ricky.durable_tasks.types import (
    DurableTask,
    TaskArtifactEntry,
    TaskAuthority,
    TaskLease,
    validate_task_id,
)
from ricky.profiles import ProfileName


class ArtifactConflictError(TaskStoreError):
    """An artifact changed after the caller read it."""


class ArtifactAuditError(TaskStoreError):
    """A file write succeeded but its activity record failed."""


class TaskArtifactRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry: TaskArtifactEntry
    content: str
    start_line: int
    end_line: int
    truncated: bool = False


class TaskArtifactWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: DurableTask
    entry: TaskArtifactEntry
    created: bool


class TaskArtifactStore:
    """Generic text file operations rooted beneath one profile task store."""

    def __init__(self, task_store: DurableTaskStore) -> None:
        self._tasks = task_store
        self.root = task_store.artifact_root
        self._settings = task_store._settings  # package-private shared limits
        self._lock_root = task_store.root / ".artifact-locks"

    async def list(self, task_id: str) -> list[TaskArtifactEntry]:
        await self._tasks.get_task(validate_task_id(task_id))
        return await asyncio.to_thread(self._list, task_id)

    async def inspect(self, task_id: str, path: str) -> TaskArtifactEntry:
        await self._tasks.get_task(validate_task_id(task_id))
        return await asyncio.to_thread(self._entry, task_id, path)

    async def read(
        self,
        task_id: str,
        path: str,
        *,
        start_line: int = 1,
        line_count: int | None = None,
    ) -> TaskArtifactRead:
        await self._tasks.get_task(validate_task_id(task_id))
        if start_line < 1:
            raise ValueError("start_line must be at least 1")
        if line_count is not None and line_count < 1:
            raise ValueError("line_count must be positive")
        return await asyncio.to_thread(self._read, task_id, path, start_line, line_count)

    async def write(
        self,
        task_id: str,
        path: str,
        content: str,
        *,
        lease: TaskLease,
        expected_revision: int,
        expected_sha256: str | None,
        authority: TaskAuthority,
        executor_id: str,
    ) -> TaskArtifactWrite:
        operation = asyncio.create_task(
            self._write_impl(
                task_id,
                path,
                content,
                lease=lease,
                expected_revision=expected_revision,
                expected_sha256=expected_sha256,
                authority=authority,
                executor_id=executor_id,
            )
        )
        return await self._settle_mutation(operation)

    async def _write_impl(
        self,
        task_id: str,
        path: str,
        content: str,
        *,
        lease: TaskLease,
        expected_revision: int,
        expected_sha256: str | None,
        authority: TaskAuthority,
        executor_id: str,
    ) -> TaskArtifactWrite:
        if len(content) > self._settings.artifact_write_char_limit:
            raise ValueError(
                "artifact content exceeds configured limit: "
                f"{len(content)} > {self._settings.artifact_write_char_limit}"
            )
        mutation: tuple[TaskArtifactEntry, bool] | None = None

        def operation() -> tuple[TaskArtifactEntry, bool]:
            nonlocal mutation
            mutation = self._write(task_id, path, content, expected_sha256)
            return mutation

        try:
            task, entry, created = await self._tasks.mutate_artifact(
                task_id,
                lease=lease,
                expected_revision=expected_revision,
                authority=authority,
                executor_id=executor_id,
                operation=operation,
            )
        except Exception as exc:
            if mutation is None:
                raise
            entry, _ = mutation
            raise ArtifactAuditError(
                f"artifact write succeeded for {entry.path}, but activity recording failed; "
                "re-read the task and artifact before retrying"
            ) from exc
        return TaskArtifactWrite(task=task, entry=entry, created=created)

    async def exact_edit(
        self,
        task_id: str,
        path: str,
        *,
        old: str,
        new: str,
        expected_sha256: str,
        lease: TaskLease,
        expected_revision: int,
        authority: TaskAuthority,
        executor_id: str,
    ) -> TaskArtifactWrite:
        operation = asyncio.create_task(
            self._exact_edit_impl(
                task_id,
                path,
                old=old,
                new=new,
                expected_sha256=expected_sha256,
                lease=lease,
                expected_revision=expected_revision,
                authority=authority,
                executor_id=executor_id,
            )
        )
        return await self._settle_mutation(operation)

    async def _exact_edit_impl(
        self,
        task_id: str,
        path: str,
        *,
        old: str,
        new: str,
        expected_sha256: str,
        lease: TaskLease,
        expected_revision: int,
        authority: TaskAuthority,
        executor_id: str,
    ) -> TaskArtifactWrite:
        if not old:
            raise ValueError("old text must be non-empty")
        mutation: tuple[TaskArtifactEntry, bool] | None = None

        def operation() -> tuple[TaskArtifactEntry, bool]:
            nonlocal mutation
            content = self._edited_content(task_id, path, old, new, expected_sha256)
            if len(content) > self._settings.artifact_write_char_limit:
                raise ValueError("edited artifact exceeds configured write limit")
            mutation = self._write(task_id, path, content, expected_sha256)
            return mutation

        try:
            task, entry, created = await self._tasks.mutate_artifact(
                task_id,
                lease=lease,
                expected_revision=expected_revision,
                authority=authority,
                executor_id=executor_id,
                operation=operation,
            )
        except Exception as exc:
            if mutation is None:
                raise
            entry, _ = mutation
            raise ArtifactAuditError(
                f"artifact edit succeeded for {entry.path}, but activity recording failed; "
                "re-read the task and artifact before retrying"
            ) from exc
        assert not created
        return TaskArtifactWrite(task=task, entry=entry, created=False)

    @staticmethod
    async def _settle_mutation[T](operation: asyncio.Task[T]) -> T:
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.shield(operation)
            raise

    def _task_dir(self, task_id: str, *, create: bool = False) -> Path:
        validate_task_id(task_id)
        root = self.root.resolve()
        task_dir = self.root / task_id
        if create:
            task_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name == "posix":
                task_dir.chmod(0o700)
        resolved = task_dir.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError("artifact task directory escapes its profile root")
        return task_dir

    def _path(self, task_id: str, relative: str, *, create_parent: bool = False) -> Path:
        pure = PurePosixPath(relative)
        if not relative or pure.is_absolute() or ".." in pure.parts:
            raise ValueError("artifact path must be a non-empty relative path")
        if any(part in {"", "."} or part.startswith(".ricky-task") for part in pure.parts):
            raise ValueError("artifact path contains a reserved component")
        task_dir = self._task_dir(task_id, create=create_parent)
        if create_parent:
            parent = task_dir
            for part in pure.parts[:-1]:
                parent /= part
                if parent.is_symlink():
                    raise ValueError("artifact paths cannot traverse symlinks")
                parent.mkdir(exist_ok=True, mode=0o700)
                if parent.is_symlink():
                    raise ValueError("artifact paths cannot traverse symlinks")
                if os.name == "posix":
                    parent.chmod(0o700)
        current = task_dir
        for part in pure.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("artifact paths cannot traverse symlinks")
        root = task_dir.resolve()
        resolved = current.resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise ValueError("artifact path escapes its task workspace")
        return current

    def _list(self, task_id: str) -> list[TaskArtifactEntry]:
        task_dir = self._task_dir(task_id)
        if not task_dir.exists():
            return []
        entries: list[TaskArtifactEntry] = []
        total_size = 0
        for path in sorted(task_dir.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"artifact workspace contains a symlink: {path.name}")
            if not path.is_file():
                continue
            total_size += self._checked_file_size(path)
            if total_size > self._settings.artifact_list_byte_limit:
                raise ValueError("artifact listing exceeds the configured total byte limit")
            entries.append(self._entry_for_path(task_dir, path))
            if len(entries) >= self._settings.artifact_list_limit:
                break
        return entries

    def _entry(self, task_id: str, relative: str) -> TaskArtifactEntry:
        path = self._path(task_id, relative)
        if not path.is_file():
            raise FileNotFoundError(f"task artifact not found: {relative}")
        return self._entry_for_path(self._task_dir(task_id), path)

    def _read(
        self, task_id: str, relative: str, start_line: int, line_count: int | None
    ) -> TaskArtifactRead:
        path = self._path(task_id, relative)
        if not path.is_file():
            raise FileNotFoundError(f"task artifact not found: {relative}")
        self._checked_file_size(path)
        rendered: list[str] = []
        rendered_chars = 0
        selected_lines = 0
        end_line = start_line - 1
        truncated = False
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if line_number < start_line:
                    continue
                if line_count is not None and selected_lines >= line_count:
                    break
                selected_lines += 1
                end_line = line_number
                available = self._settings.artifact_read_char_limit - rendered_chars
                if len(line) > available:
                    rendered.append(line[:available])
                    truncated = True
                    break
                rendered.append(line)
                rendered_chars += len(line)
        return TaskArtifactRead(
            entry=self._entry_for_path(self._task_dir(task_id), path),
            content="".join(rendered),
            start_line=start_line,
            end_line=end_line,
            truncated=truncated,
        )

    def _write(
        self,
        task_id: str,
        relative: str,
        content: str,
        expected_sha256: str | None,
    ) -> tuple[TaskArtifactEntry, bool]:
        path = self._path(task_id, relative, create_parent=True)
        encoded_size = len(content.encode("utf-8"))
        if encoded_size > self._settings.artifact_file_byte_limit:
            raise ValueError("artifact content exceeds the configured file byte limit")
        with self._task_lock(task_id):
            exists = path.exists()
            if exists and not path.is_file():
                raise ValueError("artifact target is not a regular file")
            if exists:
                self._checked_file_size(path)
                current_sha = _sha256_file(path, self._settings.artifact_file_byte_limit)
                if expected_sha256 is None:
                    raise ArtifactConflictError(
                        "replacing an existing artifact requires expected_sha256"
                    )
                if current_sha != expected_sha256:
                    raise ArtifactConflictError(
                        f"artifact changed: expected {expected_sha256}, current {current_sha}"
                    )
            elif expected_sha256 is not None:
                raise ArtifactConflictError("artifact no longer exists")
            self._atomic_write(path, content)
            return self._entry_for_path(self._task_dir(task_id), path), not exists

    def _edited_content(
        self,
        task_id: str,
        relative: str,
        old: str,
        new: str,
        expected_sha256: str,
    ) -> str:
        path = self._path(task_id, relative)
        if not path.is_file():
            raise FileNotFoundError(f"task artifact not found: {relative}")
        self._checked_file_size(path)
        content = self._bounded_text(path)
        current_sha = _sha256_file(path, self._settings.artifact_file_byte_limit)
        if current_sha != expected_sha256:
            raise ArtifactConflictError(
                f"artifact changed: expected {expected_sha256}, current {current_sha}"
            )
        if content.count(old) != 1:
            raise ArtifactConflictError("exact edit requires old text to occur exactly once")
        return content.replace(old, new, 1)

    def _entry_for_path(self, task_dir: Path, path: Path) -> TaskArtifactEntry:
        stat = path.stat()
        self._check_file_size(stat.st_size)
        return TaskArtifactEntry(
            path=path.relative_to(task_dir).as_posix(),
            size=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            sha256=_sha256_file(path, self._settings.artifact_file_byte_limit),
        )

    def _checked_file_size(self, path: Path) -> int:
        size = path.stat().st_size
        self._check_file_size(size)
        return size

    def _check_file_size(self, size: int) -> None:
        if size > self._settings.artifact_file_byte_limit:
            raise ValueError(
                f"artifact exceeds configured file byte limit: "
                f"{size} > {self._settings.artifact_file_byte_limit}"
            )

    def _bounded_text(self, path: Path) -> str:
        limit = self._settings.artifact_file_byte_limit
        with path.open("rb") as handle:
            content = handle.read(limit + 1)
        if len(content) > limit:
            self._check_file_size(len(content))
        return content.decode("utf-8")

    def _atomic_write(self, path: Path, content: str) -> None:
        encoded = content.encode("utf-8")
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temp_path = Path(temp_name)
        try:
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            if os.name == "posix":
                path.chmod(0o600)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    @contextmanager
    def _task_lock(self, task_id: str) -> Iterator[None]:
        self._lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self._lock_root / f"{validate_task_id(task_id)}.lock"
        with lock_path.open("a+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                yield
            finally:
                if os.name == "posix":
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def resolve_task_artifact_source(
    settings: RickySettings,
    *,
    profile: ProfileName,
    task_id: str,
    path: str,
) -> Path:
    """Resolve an existing task artifact for a downstream read-only consumer."""

    validate_task_id(task_id)
    pure = PurePosixPath(path)
    if not path or pure.is_absolute() or ".." in pure.parts:
        raise ValueError("artifact path must be a non-empty relative path")
    if any(part in {"", "."} or part.startswith(".ricky-task") for part in pure.parts):
        raise ValueError("artifact path contains a reserved component")
    root = (
        profile_data_path(settings, profile) / settings.durable_tasks.dir / "artifacts" / task_id
    ).resolve()
    candidate = root.joinpath(*pure.parts)
    current = root
    for part in pure.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("artifact paths cannot traverse symlinks")
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError("artifact path escapes its task workspace")
    return resolved


def _sha256_file(path: Path, byte_limit: int) -> str:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            total += len(chunk)
            if total > byte_limit:
                raise ValueError("artifact grew beyond the configured file byte limit")
            digest.update(chunk)
    return digest.hexdigest()
