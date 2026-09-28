"""Private, bounded recurring batch payload persistence."""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
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

    async def persist() -> None:
        await asyncio.to_thread(_exclusive_write, directory, path, content)
        try:
            await store.insert_batch(batch, item_ids, scope=profile_scope)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    # The payload and ledger row form one owned operation. Cancelling a raw
    # to_thread await can leave a committed row referring to a deleted payload.
    operation = asyncio.create_task(persist())
    try:
        await asyncio.shield(operation)
    except asyncio.CancelledError:
        while not operation.done():
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.shield(operation)
        with suppress(Exception):
            operation.result()
        raise
    return batch


def _exclusive_write(directory: Path, path: Path, content: bytes) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        directory.chmod(0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


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
