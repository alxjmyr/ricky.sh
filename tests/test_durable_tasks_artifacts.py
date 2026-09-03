"""Task artifact confinement, optimistic writes, and human-edit tests."""

from __future__ import annotations

import asyncio
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ricky.config import DurableTaskSettings, RickySettings
from ricky.durable_tasks.artifacts import ArtifactConflictError, TaskArtifactStore
from ricky.durable_tasks.store import DurableTaskStore, TaskLeaseError
from ricky.durable_tasks.types import DurableTask, TaskLease


def _lease(task: DurableTask) -> TaskLease:
    assert task.lease is not None
    return task.lease


async def _claimed(tmp_path: Path):
    store = await DurableTaskStore.create(
        RickySettings(user_data_dir=str(tmp_path / "user")), profile="personal"
    )
    task = await store.create_task(
        title="Draft response",
        objective="Prepare a response for review",
        closure_criteria="The reviewed response is finalized",
        execution_mode="joint",
        authority="joint_work",
        executor_id="session_a",
    )
    claimed = await store.claim(
        task.id,
        holder_session_id="session_a",
        authority="joint_work",
        executor_id="session_a",
    )
    assert claimed.lease is not None
    return store, TaskArtifactStore(store), claimed


async def test_artifacts_are_generic_readable_and_digest_guarded(tmp_path: Path) -> None:
    store, artifacts, task = await _claimed(tmp_path)
    written = await artifacts.write(
        task.id,
        "drafts/reply.md",
        "Hello\nDraft body\n",
        lease=_lease(task),
        expected_revision=task.revision,
        expected_sha256=None,
        authority="joint_work",
        executor_id="session_a",
    )
    assert written.created
    assert written.entry.path == "drafts/reply.md"
    assert (store.artifact_root / task.id / "drafts" / "reply.md").read_text() == (
        "Hello\nDraft body\n"
    )
    read = await artifacts.read(task.id, "drafts/reply.md", start_line=2)
    assert read.content == "Draft body\n"
    assert [entry.path for entry in await artifacts.list(task.id)] == ["drafts/reply.md"]

    edited = await artifacts.exact_edit(
        task.id,
        "drafts/reply.md",
        old="Draft body",
        new="Final body",
        expected_sha256=written.entry.sha256,
        lease=_lease(written.task),
        expected_revision=written.task.revision,
        authority="joint_work",
        executor_id="session_a",
    )
    assert (await artifacts.read(task.id, "drafts/reply.md")).content.endswith("Final body\n")
    assert edited.entry.sha256 != written.entry.sha256


async def test_human_edit_conflicts_instead_of_overwrite(tmp_path: Path) -> None:
    store, artifacts, task = await _claimed(tmp_path)
    written = await artifacts.write(
        task.id,
        "draft.md",
        "agent draft",
        lease=_lease(task),
        expected_revision=task.revision,
        expected_sha256=None,
        authority="joint_work",
        executor_id="session_a",
    )
    path = store.artifact_root / task.id / "draft.md"
    path.write_text("human revision", encoding="utf-8")

    with pytest.raises(ArtifactConflictError, match="artifact changed"):
        await artifacts.write(
            task.id,
            "draft.md",
            "agent overwrite",
            lease=_lease(written.task),
            expected_revision=written.task.revision,
            expected_sha256=written.entry.sha256,
            authority="joint_work",
            executor_id="session_a",
        )
    assert path.read_text() == "human revision"


@pytest.mark.parametrize("path", ["../escape.md", "/tmp/escape.md", ".ricky-task.lock"])
async def test_artifact_paths_are_confined(tmp_path: Path, path: str) -> None:
    _, artifacts, task = await _claimed(tmp_path)
    with pytest.raises(ValueError):
        await artifacts.write(
            task.id,
            path,
            "nope",
            lease=_lease(task),
            expected_revision=task.revision,
            expected_sha256=None,
            authority="joint_work",
            executor_id="session_a",
        )


async def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    store, artifacts, task = await _claimed(tmp_path)
    task_dir = store.artifact_root / task.id
    task_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (task_dir / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(ValueError, match="symlink"):
        await artifacts.write(
            task.id,
            "link/escape.md",
            "nope",
            lease=_lease(task),
            expected_revision=task.revision,
            expected_sha256=None,
            authority="joint_work",
            executor_id="session_a",
        )
    assert not (outside / "escape.md").exists()
    if os.name == "posix":
        assert task_dir.stat().st_mode & 0o777 == 0o700


async def test_artifact_write_requires_a_current_database_lease(tmp_path: Path) -> None:
    store, artifacts, task = await _claimed(tmp_path)
    released = await store.release(
        task.id,
        lease=_lease(task),
        expected_revision=task.revision,
        authority="joint_work",
        executor_id="session_a",
    )
    with pytest.raises(TaskLeaseError, match="no active lease"):
        await artifacts.write(
            task.id,
            "draft.md",
            "not authorized",
            lease=_lease(task),
            expected_revision=released.revision,
            expected_sha256=None,
            authority="joint_work",
            executor_id="session_a",
        )


async def test_cancelled_artifact_write_settles_its_audit_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, artifacts, task = await _claimed(tmp_path)
    started = threading.Event()
    release = threading.Event()
    original = artifacts._write

    def blocked_write(
        task_id: str,
        path: str,
        content: str,
        expected_sha256: str | None,
    ):
        started.set()
        assert release.wait(timeout=2)
        return original(task_id, path, content, expected_sha256)

    monkeypatch.setattr(artifacts, "_write", blocked_write)
    request = asyncio.create_task(
        artifacts.write(
            task.id,
            "cancelled.md",
            "write must settle",
            lease=_lease(task),
            expected_revision=task.revision,
            expected_sha256=None,
            authority="joint_work",
            executor_id="session_a",
        )
    )
    assert await asyncio.to_thread(started.wait, 2)
    request.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await request

    assert (store.artifact_root / task.id / "cancelled.md").read_text() == "write must settle"
    assert (await store.activities(task.id))[0].kind == "artifact_created"


async def test_artifact_mutation_blocks_lease_rollover_until_audit_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Clock:
        value = datetime(2026, 7, 28, tzinfo=UTC)

        def __call__(self) -> datetime:
            return self.value

    clock = Clock()
    settings = RickySettings(
        user_data_dir=str(tmp_path / "user"),
        durable_tasks=DurableTaskSettings(lease_seconds=10),
    )
    store = await DurableTaskStore.create(settings, profile="personal", clock=clock)
    task = await store.create_task(
        title="Fenced artifact",
        objective="Keep artifact mutation under the current lease",
        closure_criteria="A stale lease never writes after rollover",
        execution_mode="agent",
        authority="agent_autonomy",
        executor_id="session_a",
    )
    claimed = await store.claim(
        task.id,
        holder_session_id="session_a",
        authority="agent_autonomy",
        executor_id="session_a",
    )
    artifacts = TaskArtifactStore(store)
    write_started = threading.Event()
    release_write = threading.Event()
    claim_started = threading.Event()
    claim_finished = threading.Event()
    original_write = artifacts._write
    original_claim = store._claim

    def blocked_write(
        task_id: str,
        path: str,
        content: str,
        expected_sha256: str | None,
    ):
        write_started.set()
        assert release_write.wait(timeout=2)
        return original_write(task_id, path, content, expected_sha256)

    def observed_claim(*args, **kwargs):
        claim_started.set()
        try:
            return original_claim(*args, **kwargs)
        finally:
            claim_finished.set()

    monkeypatch.setattr(artifacts, "_write", blocked_write)
    monkeypatch.setattr(store, "_claim", observed_claim)
    write_request = asyncio.create_task(
        artifacts.write(
            task.id,
            "fenced.md",
            "session A",
            lease=_lease(claimed),
            expected_revision=claimed.revision,
            expected_sha256=None,
            authority="agent_autonomy",
            executor_id="session_a",
        )
    )
    assert await asyncio.to_thread(write_started.wait, 2)
    clock.value += timedelta(seconds=11)
    claim_request = asyncio.create_task(
        store.claim(
            task.id,
            holder_session_id="session_b",
            authority="agent_autonomy",
            executor_id="session_b",
        )
    )
    assert await asyncio.to_thread(claim_started.wait, 2)
    try:
        assert not await asyncio.to_thread(claim_finished.wait, 0.1)
    finally:
        release_write.set()

    written = await write_request
    reclaimed = await claim_request
    assert reclaimed.lease is not None
    assert reclaimed.lease.epoch == _lease(claimed).epoch + 1
    assert reclaimed.revision == written.task.revision + 1
    assert (store.artifact_root / task.id / "fenced.md").read_text() == "session A"


async def test_oversized_human_artifacts_fail_before_read_or_hash(tmp_path: Path) -> None:
    settings = RickySettings(
        user_data_dir=str(tmp_path / "user"),
        durable_tasks=DurableTaskSettings(
            artifact_read_char_limit=1_000,
            artifact_write_char_limit=1_000,
            artifact_file_byte_limit=4_000,
            artifact_list_byte_limit=5_000,
        ),
    )
    store = await DurableTaskStore.create(settings, profile="personal")
    task = await store.create_task(
        title="Bounded artifacts",
        objective="Bound artifact resource use",
        closure_criteria="Oversized files fail closed",
        execution_mode="joint",
        authority="joint_work",
        executor_id="fixture",
    )
    task_dir = store.artifact_root / task.id
    task_dir.mkdir(parents=True)
    oversized = task_dir / "oversized.md"
    oversized.write_bytes(b"x" * 4_001)
    artifacts = TaskArtifactStore(store)

    with pytest.raises(ValueError, match="file byte limit"):
        await artifacts.read(task.id, "oversized.md")
    with pytest.raises(ValueError, match="file byte limit"):
        await artifacts.list(task.id)


async def test_artifact_listing_has_a_total_hash_budget(tmp_path: Path) -> None:
    store, artifacts, task = await _claimed(tmp_path)
    artifacts._settings = artifacts._settings.model_copy(
        update={"artifact_file_byte_limit": 4_000, "artifact_list_byte_limit": 5_000}
    )
    task_dir = store.artifact_root / task.id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "one.md").write_bytes(b"a" * 3_000)
    (task_dir / "two.md").write_bytes(b"b" * 3_000)

    with pytest.raises(ValueError, match="total byte limit"):
        await artifacts.list(task.id)
