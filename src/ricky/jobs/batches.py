"""Private, bounded recurring batch payload persistence."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, JsonValue

from ricky.jobs.lock import FileLock
from ricky.jobs.sources import BatchKind, PersistedBatch
from ricky.jobs.store import JobRunStore
from ricky.profiles import ProfileScope


async def persist_batch(
    store: JobRunStore,
    *,
    run_id: str,
    job_name: str,
    source_name: str,
    kind: BatchKind,
    payload: BaseModel,
    item_ids: list[str],
    complete: bool,
    dry_run: bool,
    profile_scope: ProfileScope,
    upper_bound: datetime | None = None,
    input_cursor: JsonValue = None,
    next_cursor: JsonValue = None,
) -> PersistedBatch:
    """Write payload first, then atomically make its identities visible in SQLite."""

    batch_id = f"batch_{uuid4().hex}"
    directory = store.root / "batches" / run_id
    path = directory / f"{batch_id}.json"
    content = payload.model_dump_json(indent=2).encode("utf-8")
    await asyncio.to_thread(_exclusive_write, directory, path, content)
    batch = PersistedBatch(
        id=batch_id,
        run_id=run_id,
        job_name=job_name,
        source_name=source_name,
        kind=kind,
        payload_path=str(path),
        upper_bound=upper_bound,
        input_cursor=input_cursor,
        next_cursor=next_cursor,
        complete=complete,
        dry_run=dry_run,
        profile_label=profile_scope.label(),
        created_at=datetime.now(UTC),
    )
    try:
        await store.insert_batch(batch, item_ids, scope=profile_scope)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return batch


def _exclusive_write(directory: Path, path: Path, content: bytes) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        directory.chmod(0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


async def read_batch_payload(batch: PersistedBatch, model: type[BaseModel]) -> BaseModel:
    if not batch.payload_path:
        raise FileNotFoundError(f"batch payload was pruned: {batch.id}")
    return model.model_validate_json(await asyncio.to_thread(Path(batch.payload_path).read_bytes))


async def prune_batch_payloads(store: JobRunStore, *, scope: ProfileScope, keep: int) -> None:
    """Prune completed payload files while retaining batch/disposition metadata."""

    lock = FileLock(store.root / "locks" / "batch-retention.lock")
    if not lock.acquire():
        return
    try:
        completed = await store.completed_batch_payloads(scope=scope)
        for batch_id, raw_path in completed[keep:]:
            path = Path(raw_path)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            await store.clear_batch_payload_path(batch_id, raw_path, scope=scope)
    finally:
        lock.release()
