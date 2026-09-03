"""Confined immutable storage for full session tool-result text."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import stat
import tempfile
from contextlib import suppress
from pathlib import Path, PurePosixPath
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ricky.agent.session import AgentSession, SessionArtifactRecord
from ricky.config import RickySettings, ToolResultContextSettings, user_data_path

_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_ARTIFACT_ID = re.compile(r"^artifact_[0-9a-f]{32}$")


class SessionArtifactError(RuntimeError):
    """A session artifact could not be safely stored or read."""


class SessionArtifactLimitError(SessionArtifactError):
    """An artifact would exceed a configured immutable-storage limit."""


class ToolArtifactChunk(BaseModel):
    """One validated character range read from a full stored result."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(pattern=r"^artifact_[0-9a-f]{32}$")
    content: str
    offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)
    total_chars: int = Field(ge=0)
    next_offset: int | None = Field(default=None, ge=0)


class SessionArtifactStore:
    """Filesystem-backed namespace reopened by settings and stable session id."""

    def __init__(self, settings: RickySettings, session_id: str) -> None:
        self._settings: ToolResultContextSettings = settings.context.tool_results
        self._user_root = user_data_path(settings)
        self._sessions_root = self._user_root / "sessions"
        self._lock = asyncio.Lock()
        self._bind(session_id)

    def _bind(self, session_id: str) -> None:
        if _SESSION_ID.fullmatch(session_id) is None or session_id in {".", ".."}:
            raise ValueError("invalid session id for artifact storage")
        self._session_root = self._sessions_root / session_id
        self.root = self._session_root / "artifacts"

    @classmethod
    def create(cls, settings: RickySettings, session_id: str) -> SessionArtifactStore:
        """Construct a reopenable store without creating directories eagerly."""
        return cls(settings, session_id)

    async def offload(
        self,
        session: AgentSession,
        *,
        call_id: str,
        tool_name: str,
        content: str,
        excerpt_chars: int,
    ) -> SessionArtifactRecord:
        """Atomically persist full text, then append its immutable manifest record."""
        if session.id != self._session_root.name:
            raise SessionArtifactError("artifact store does not belong to this session")
        full_chars = len(content)
        if full_chars > self._settings.artifact_max_chars:
            raise SessionArtifactLimitError("tool result exceeds the per-artifact limit")
        payload = content.encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()

        async with self._lock:
            stored_chars = sum(record.full_chars for record in session.artifacts)
            if stored_chars + full_chars > self._settings.session_artifact_max_chars:
                raise SessionArtifactLimitError("tool result exceeds the session artifact limit")
            if len(session.artifacts) >= 10_000:
                raise SessionArtifactLimitError("session artifact manifest is full")

            artifact_id = f"artifact_{uuid4().hex}"
            relative_path = f"{artifact_id}.txt"
            operation = asyncio.create_task(
                asyncio.to_thread(self._write_atomic, relative_path, payload)
            )
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                with suppress(Exception):
                    await asyncio.shield(operation)
                with suppress(Exception):
                    await asyncio.to_thread(self._remove_file, relative_path)
                raise

            record = SessionArtifactRecord(
                id=artifact_id,
                call_id=call_id,
                tool_name=tool_name,
                full_chars=full_chars,
                sha256=digest,
                excerpt_chars=excerpt_chars,
                relative_path=relative_path,
            )
            try:
                session.artifacts.append(record)
            except BaseException:
                with suppress(Exception):
                    self._remove_file(relative_path)
                raise
            return record

    async def read(
        self,
        session: AgentSession,
        artifact_id: str,
        *,
        offset: int = 0,
        max_chars: int | None = None,
    ) -> ToolArtifactChunk:
        """Read a digest-verified bounded range resolved only through the manifest."""
        if offset < 0:
            raise SessionArtifactError("artifact offset must not be negative")
        limit = self._settings.read_chunk_chars if max_chars is None else max_chars
        if limit < 1 or limit > self._settings.read_chunk_chars:
            raise SessionArtifactError("artifact read size exceeds the configured limit")
        record = self._record(session, artifact_id)
        content = await asyncio.to_thread(self._read_validated, record)
        end = min(record.full_chars, offset + limit)
        if offset > record.full_chars:
            raise SessionArtifactError("artifact offset exceeds the full result size")
        return ToolArtifactChunk(
            artifact_id=artifact_id,
            content=content[offset:end],
            offset=offset,
            end_offset=end,
            total_chars=record.full_chars,
            next_offset=end if end < record.full_chars else None,
        )

    async def remove_all(self) -> None:
        """Remove this validated session namespace for retention cleanup."""
        await asyncio.to_thread(self._remove_all_sync)

    async def reset_for_session(self, session_id: str) -> None:
        """Discard the current namespace and bind this idle owner to a fresh session."""

        if _SESSION_ID.fullmatch(session_id) is None or session_id in {".", ".."}:
            raise ValueError("invalid session id for artifact storage")
        async with self._lock:
            await asyncio.to_thread(self._remove_all_sync)
            self._bind(session_id)

    def _record(self, session: AgentSession, artifact_id: str) -> SessionArtifactRecord:
        if session.id != self._session_root.name:
            raise SessionArtifactError("artifact store does not belong to this session")
        if _ARTIFACT_ID.fullmatch(artifact_id) is None:
            raise SessionArtifactError("unknown tool artifact id")
        matches = [record for record in session.artifacts if record.id == artifact_id]
        if len(matches) != 1:
            raise SessionArtifactError("unknown tool artifact id")
        return matches[0]

    def _write_atomic(self, relative_path: str, payload: bytes) -> None:
        root = self._ensure_root()
        final = self._confined_path(relative_path, root=root)
        if final.exists() or final.is_symlink():
            raise SessionArtifactError("artifact id collision")
        descriptor = -1
        temporary: Path | None = None
        try:
            descriptor, raw_temporary = tempfile.mkstemp(prefix=".tmp-", dir=root)
            temporary = Path(raw_temporary)
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, final)
            temporary = None
            if os.name == "posix":
                final.chmod(0o600)
                directory_fd = os.open(root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            final.unlink(missing_ok=True)
            raise

    def _read_validated(self, record: SessionArtifactRecord) -> str:
        root = self._existing_root()
        path = self._confined_path(record.relative_path, root=root)
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            raise SessionArtifactError("tool artifact file is missing") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise SessionArtifactError("tool artifact is not a regular file")
        try:
            payload = path.read_bytes()
            content = payload.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise SessionArtifactError("tool artifact could not be read as UTF-8 text") from exc
        if len(content) != record.full_chars:
            raise SessionArtifactError("tool artifact character count mismatch")
        if hashlib.sha256(payload).hexdigest() != record.sha256:
            raise SessionArtifactError("tool artifact digest mismatch")
        return content

    def _ensure_root(self) -> Path:
        self._user_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in (self._sessions_root, self._session_root, self.root):
            if directory.is_symlink():
                raise SessionArtifactError("artifact namespace cannot traverse symlinks")
            directory.mkdir(exist_ok=True, mode=0o700)
            if directory.is_symlink():
                raise SessionArtifactError("artifact namespace cannot traverse symlinks")
            if os.name == "posix":
                directory.chmod(0o700)
        return self._existing_root()

    def _existing_root(self) -> Path:
        if self.root.is_symlink() or not self.root.is_dir():
            raise SessionArtifactError("session artifact directory is missing or unsafe")
        resolved = self.root.resolve()
        if not resolved.is_relative_to(self._user_root):
            raise SessionArtifactError("session artifact directory escapes user data")
        return resolved

    @staticmethod
    def _confined_path(relative_path: str, *, root: Path) -> Path:
        pure = PurePosixPath(relative_path)
        if (
            not relative_path
            or pure.is_absolute()
            or len(pure.parts) != 1
            or pure.parts[0] in {".", ".."}
        ):
            raise SessionArtifactError("invalid artifact manifest locator")
        path = root / pure.parts[0]
        if path.is_symlink():
            raise SessionArtifactError("tool artifact cannot be a symlink")
        if not path.resolve(strict=False).is_relative_to(root):
            raise SessionArtifactError("tool artifact locator escapes its session")
        return path

    def _remove_file(self, relative_path: str) -> None:
        if not self.root.exists():
            return
        root = self._existing_root()
        self._confined_path(relative_path, root=root).unlink(missing_ok=True)

    def _remove_all_sync(self) -> None:
        if not self._session_root.exists():
            return
        if self._session_root.is_symlink():
            raise SessionArtifactError("session artifact namespace is a symlink")
        resolved = self._session_root.resolve()
        if not resolved.is_relative_to(self._sessions_root.resolve()):
            raise SessionArtifactError("session artifact namespace escapes user data")
        shutil.rmtree(resolved)
