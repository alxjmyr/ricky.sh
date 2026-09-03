"""Flushed, permission-confined JSONL event transcripts and retention."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TextIO

from ricky.agent.artifacts import SessionArtifactError, SessionArtifactStore
from ricky.agent.events import AgentEvent
from ricky.config import RickySettings
from ricky.jobs.lock import FileLock
from ricky.jobs.store import JobRunStore
from ricky.profiles import ProfileScope


class JobTranscript:
    """One sensitive event transcript flushed after every event."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: TextIO | None = None

    async def open(self) -> JobTranscript:
        self._open_sync()
        return self

    async def append(self, event: AgentEvent) -> None:
        line = event.model_dump_json() + "\n"
        self._append_sync(line)

    async def close(self) -> None:
        self._close_sync()

    def _open_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._handle = os.fdopen(descriptor, "w", encoding="utf-8")

    def _append_sync(self, line: str) -> None:
        if self._handle is None:
            raise RuntimeError("job transcript is not open")
        self._handle.write(line)
        self._handle.flush()

    def _close_sync(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            handle.flush()
            handle.close()


async def prune_transcripts(
    store: JobRunStore,
    *,
    scope: ProfileScope,
    keep: int,
    settings: RickySettings | None = None,
) -> None:
    """Prune only completed transcripts while serializing concurrent pruners."""

    lock = FileLock(store.root / "locks" / "transcript-retention.lock")
    if not lock.acquire():
        return
    try:
        completed = await store.completed_transcript_records(scope=scope)
        for run_id, raw_path, session_id in completed[keep:]:
            path = Path(raw_path)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            if settings is not None and not await store.has_other_transcript_reference(
                session_id, run_id, scope=scope
            ):
                try:
                    await SessionArtifactStore.create(settings, session_id).remove_all()
                except (OSError, SessionArtifactError, ValueError):
                    # Keep the logical transcript reference so a later retention
                    # pass retries this conservative leak.
                    continue
            await store.clear_transcript_path(run_id, raw_path, scope=scope)
    finally:
        lock.release()
