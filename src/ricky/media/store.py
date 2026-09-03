"""Confined immutable storage and provider-bound egress for session media."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import stat
import struct
import tempfile
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

from ricky.agent.session import (
    AgentSession,
    MediaAdmissionEvidence,
    SessionMediaRecord,
)
from ricky.config import RickySettings, user_data_path
from ricky.llm import MediaArtifactRef, MediaResolver, ResolvedMedia
from ricky.profiles import ProfileLabel, ProfileName, ProfileScope

_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_MEDIA_ID = re.compile(r"^media_[0-9a-f]{32}$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class SessionMediaError(RuntimeError):
    """Session media could not be safely admitted, retained, or materialized."""


class SessionMediaLimitError(SessionMediaError):
    """A media operation exceeds a configured storage or request ceiling."""


async def _join_namespace_deletion(operation: asyncio.Task[None]) -> bool:
    """Join filesystem deletion while deferring cancellation of its owner."""

    interrupted = False
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            interrupted = True
        except Exception:
            break
    operation.result()
    return interrupted


class SessionMediaStore:
    """Filesystem namespace reopened by settings and one stable session id."""

    def __init__(self, settings: RickySettings, session_id: str) -> None:
        self._settings = settings
        self._media_settings = settings.context.media
        self._user_root = user_data_path(settings)
        self._sessions_root = self._user_root / "sessions"
        self._lock = asyncio.Lock()
        self._bind(session_id)

    def _bind(self, session_id: str) -> None:
        if _SESSION_ID.fullmatch(session_id) is None or session_id in {".", ".."}:
            raise ValueError("invalid session id for media storage")
        self._session_root = self._sessions_root / session_id
        self.root = self._session_root / "media"

    @classmethod
    def create(cls, settings: RickySettings, session_id: str) -> SessionMediaStore:
        """Construct a reopenable media store without eager directory creation."""
        return cls(settings, session_id)

    async def admit_png(
        self,
        session: AgentSession,
        *,
        content: bytes,
        source_label: ProfileLabel,
        source_owner: ProfileName,
        provenance: str,
        disclosure_class: str,
        admitted_provider: str,
        retention: Literal["runtime", "session", "conversation"] = "runtime",
    ) -> SessionMediaRecord:
        """Atomically admit one PNG after source, profile, policy, and limits pass."""
        self._require_session(session)
        if not session.profile_scope.permits(source_label) or not session.profile_scope.includes(
            source_owner
        ):
            raise SessionMediaError("media source is outside the active profile scope")
        if admitted_provider != session.provider:
            raise SessionMediaError("media admission provider does not match the session")
        if not _disclosure_allowed(
            self._settings,
            source_owner=source_owner,
            disclosure_class=disclosure_class,
            provider=admitted_provider,
        ):
            raise SessionMediaError("current media disclosure policy denies admission")
        width, height = _png_dimensions(content)
        digest = hashlib.sha256(content).hexdigest()

        async with self._lock:
            stored_bytes = sum(record.byte_count for record in session.media)
            if stored_bytes + len(content) > self._media_settings.session_byte_limit:
                raise SessionMediaLimitError("media exceeds the session byte limit")
            if len(session.media) >= 1_000:
                raise SessionMediaLimitError("session media manifest is full")

            media_id = f"media_{uuid4().hex}"
            relative_path = f"{media_id}.png"
            operation = asyncio.create_task(
                asyncio.to_thread(self._write_atomic, relative_path, content)
            )
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                with suppress(Exception):
                    await asyncio.shield(operation)
                with suppress(Exception):
                    await asyncio.to_thread(self._remove_file, relative_path)
                raise

            record = SessionMediaRecord(
                id=media_id,
                media_type="image/png",
                byte_count=len(content),
                sha256=digest,
                width=width,
                height=height,
                source_label=source_label,
                relative_path=relative_path,
                provenance=provenance,
                retention=retention,
                admission=MediaAdmissionEvidence(
                    disclosure_class=disclosure_class,
                    admitted_provider=admitted_provider,
                    source_owner=source_owner,
                ),
            )
            try:
                session.media.append(record)
            except BaseException:
                with suppress(Exception):
                    self._remove_file(relative_path)
                raise
            return record

    def resolver(
        self,
        session: AgentSession,
        *,
        provider: str,
        profile_scope: ProfileScope,
    ) -> BoundMediaResolver:
        """Bind materialization to one exact session, scope, and provider."""
        self._require_session(session)
        if profile_scope != session.profile_scope or provider != session.provider:
            raise SessionMediaError("media resolver binding does not match the session")
        return BoundMediaResolver(
            self,
            session=session,
            provider=provider,
            profile_scope=profile_scope,
        )

    async def materialize(
        self,
        session: AgentSession,
        reference: MediaArtifactRef,
        *,
        provider: str,
        profile_scope: ProfileScope,
    ) -> ResolvedMedia:
        """Read one exact digest-checked body after re-evaluating current policy."""
        self._require_session(session)
        if profile_scope != session.profile_scope or provider != session.provider:
            raise SessionMediaError("media resolver is bound to the wrong session authority")
        record = self._record(session, reference.id)
        if record.reference() != reference:
            raise SessionMediaError("media reference does not match its manifest record")
        if not profile_scope.permits(record.source_label):
            raise SessionMediaError("media source label is outside the bound profile scope")
        if record.admission.admitted_provider != provider:
            raise SessionMediaError("media was not admitted for the bound provider")
        if not _disclosure_allowed(
            self._settings,
            source_owner=record.admission.source_owner,
            disclosure_class=record.admission.disclosure_class,
            provider=provider,
        ):
            raise SessionMediaError("current media disclosure policy denies materialization")
        if record.byte_count > self._media_settings.request_image_byte_limit:
            raise SessionMediaLimitError("media exceeds the request byte limit")
        if record.width * record.height > self._media_settings.request_image_pixel_limit:
            raise SessionMediaLimitError("media exceeds the request pixel limit")
        content = await asyncio.to_thread(self._read_validated, record)
        return ResolvedMedia(
            media_type=record.media_type,
            content=content,
            sha256=record.sha256,
            width=record.width,
            height=record.height,
        )

    async def remove_retention(
        self,
        session: AgentSession,
        retention: Literal["runtime", "session", "conversation"],
    ) -> None:
        """Remove exactly one retention class and its private manifest records."""
        self._require_session(session)
        async with self._lock:
            removed = [record for record in session.media if record.retention == retention]
            for record in removed:
                await asyncio.to_thread(self._remove_file, record.relative_path)
            removed_ids = {record.id for record in removed}
            session.media = [record for record in session.media if record.id not in removed_ids]
            await asyncio.to_thread(self._remove_empty_namespace)

    async def remove_all(self, session: AgentSession) -> None:
        """Remove this validated session media namespace and clear its manifest."""
        self._require_session(session)
        async with self._lock:
            operation = asyncio.create_task(asyncio.to_thread(self._remove_all_sync))
            interrupted = await _join_namespace_deletion(operation)
            session.media = []
            if interrupted:
                raise asyncio.CancelledError

    async def reset_for_session(self, session: AgentSession, new_session_id: str) -> None:
        """Remove current media and rebind this resident store to a fresh session id."""
        self._require_session(session)
        if _SESSION_ID.fullmatch(new_session_id) is None or new_session_id in {".", ".."}:
            raise ValueError("invalid session id for media storage")
        async with self._lock:
            operation = asyncio.create_task(asyncio.to_thread(self._remove_all_sync))
            interrupted = await _join_namespace_deletion(operation)
            session.media = []
            self._bind(new_session_id)
            if interrupted:
                raise asyncio.CancelledError

    def _require_session(self, session: AgentSession) -> None:
        if session.id != self._session_root.name:
            raise SessionMediaError("media store does not belong to this session")

    def _record(self, session: AgentSession, media_id: str) -> SessionMediaRecord:
        if _MEDIA_ID.fullmatch(media_id) is None:
            raise SessionMediaError("unknown media artifact id")
        matches = [record for record in session.media if record.id == media_id]
        if len(matches) != 1:
            raise SessionMediaError("unknown media artifact id")
        return matches[0]

    def _write_atomic(self, relative_path: str, content: bytes) -> None:
        root = self._ensure_root()
        final = self._confined_path(relative_path, root=root)
        if final.exists() or final.is_symlink():
            raise SessionMediaError("media artifact id collision")
        descriptor = -1
        temporary: Path | None = None
        try:
            descriptor, raw_temporary = tempfile.mkstemp(prefix=".tmp-", dir=root)
            temporary = Path(raw_temporary)
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(content)
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

    def _read_validated(self, record: SessionMediaRecord) -> bytes:
        root = self._existing_root()
        path = self._confined_path(record.relative_path, root=root)
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            raise SessionMediaError("media artifact file is missing") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise SessionMediaError("media artifact is not a regular file")
        content = path.read_bytes()
        if len(content) != record.byte_count:
            raise SessionMediaError("media artifact byte count mismatch")
        if hashlib.sha256(content).hexdigest() != record.sha256:
            raise SessionMediaError("media artifact digest mismatch")
        if _png_dimensions(content) != (record.width, record.height):
            raise SessionMediaError("media artifact dimensions mismatch")
        return content

    def _ensure_root(self) -> Path:
        self._user_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in (self._sessions_root, self._session_root, self.root):
            if directory.is_symlink():
                raise SessionMediaError("media namespace cannot traverse symlinks")
            directory.mkdir(exist_ok=True, mode=0o700)
            if directory.is_symlink():
                raise SessionMediaError("media namespace cannot traverse symlinks")
            if os.name == "posix":
                directory.chmod(0o700)
        return self._existing_root()

    def _existing_root(self) -> Path:
        if self.root.is_symlink() or not self.root.is_dir():
            raise SessionMediaError("session media directory is missing or unsafe")
        resolved = self.root.resolve()
        if not resolved.is_relative_to(self._user_root):
            raise SessionMediaError("session media directory escapes user data")
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
            raise SessionMediaError("invalid media manifest locator")
        path = root / pure.parts[0]
        if path.is_symlink():
            raise SessionMediaError("media artifact cannot be a symlink")
        if not path.resolve(strict=False).is_relative_to(root):
            raise SessionMediaError("media artifact locator escapes its session")
        return path

    def _remove_file(self, relative_path: str) -> None:
        if not self.root.exists():
            return
        root = self._existing_root()
        self._confined_path(relative_path, root=root).unlink(missing_ok=True)

    def _remove_empty_namespace(self) -> None:
        if self.root.is_dir() and not any(self.root.iterdir()):
            self.root.rmdir()
        if self._session_root.is_dir() and not any(self._session_root.iterdir()):
            self._session_root.rmdir()

    def _remove_all_sync(self) -> None:
        if not self.root.exists():
            return
        if self.root.is_symlink():
            raise SessionMediaError("session media namespace is a symlink")
        resolved = self.root.resolve()
        if not resolved.is_relative_to(self._sessions_root.resolve()):
            raise SessionMediaError("session media namespace escapes user data")
        shutil.rmtree(resolved)
        self._remove_empty_namespace()


class BoundMediaResolver(MediaResolver):
    """In-process resolver that cannot choose a different session or provider."""

    def __init__(
        self,
        store: SessionMediaStore,
        *,
        session: AgentSession,
        provider: str,
        profile_scope: ProfileScope,
    ) -> None:
        self._store = store
        self._session = session
        self._provider = provider
        self._profile_scope = profile_scope

    async def resolve(self, reference: object) -> ResolvedMedia:
        """Materialize an exact canonical media reference."""
        parsed = MediaArtifactRef.model_validate(reference)
        return await self._store.materialize(
            self._session,
            parsed,
            provider=self._provider,
            profile_scope=self._profile_scope,
        )


def _png_dimensions(content: bytes) -> tuple[int, int]:
    if len(content) < 24 or content[:8] != _PNG_SIGNATURE:
        raise SessionMediaError("media is not a PNG")
    chunk_length = struct.unpack(">I", content[8:12])[0]
    if chunk_length != 13 or content[12:16] != b"IHDR":
        raise SessionMediaError("media PNG has an invalid header")
    width, height = struct.unpack(">II", content[16:24])
    if width < 1 or height < 1:
        raise SessionMediaError("media PNG dimensions must be positive")
    return width, height


def _disclosure_allowed(
    settings: RickySettings,
    *,
    source_owner: ProfileName,
    disclosure_class: str,
    provider: str,
) -> bool:
    if disclosure_class == "explicit_provider":
        return True
    if disclosure_class != "browser_screenshot":
        return False
    configured = settings.profile_configs.get(source_owner)
    if configured is None or configured.browser is None:
        return False
    return provider in configured.browser.screenshot_allowed_providers
