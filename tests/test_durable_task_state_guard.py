"""Revision recovery advice follows live leases without weakening task fencing."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ricky.agent.session import AgentSession
from ricky.config import RickySettings
from ricky.durable_tasks.artifacts import TaskArtifactStore
from ricky.durable_tasks.state_guard import DurableTaskStateGuard
from ricky.durable_tasks.store import DurableTaskStore
from ricky.durable_tasks.tools import durable_task_tools
from ricky.tools import ToolContext, ToolRegistry


async def _context(tmp_path: Path, *, expired: bool = False):
    settings = RickySettings(
        user_data_dir=str(tmp_path / "user"), project_data_dir=str(tmp_path / "project")
    )
    now = datetime.now(UTC) - timedelta(days=1) if expired else datetime.now(UTC)
    store = await DurableTaskStore.create(settings, profile="personal", clock=lambda: now)
    guard = DurableTaskStateGuard(store)
    registry = ToolRegistry(
        [guard.wrap(tool) for tool in durable_task_tools(store, TaskArtifactStore(store))]
    )
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="synthetic",
    )
    ctx = ToolContext(cwd=tmp_path, settings=settings, session=session)
    task = await store.create_task(
        title="Finish responsibility",
        objective="Perform and record work",
        closure_criteria="Work completed",
        execution_mode="agent",
        authority="agent_autonomy",
        executor_id=session.id,
    )
    return store, registry, ctx, task


async def test_stale_claim_guides_held_lease_to_progress_and_completion(tmp_path: Path):
    store, registry, ctx, task = await _context(tmp_path)
    claim = {"task_id": task.id, "expected_revision": task.revision}
    assert not (await registry.dispatch("claim_durable_task", claim, ctx)).is_error
    claimed = await store.get_task(task.id)
    rejected = await registry.dispatch("claim_durable_task", claim, ctx)
    assert rejected.is_error
    assert "expected revision 1; current revision is 2" in rejected.content
    assert "This session already holds the active task lease" in rejected.content
    assert "update_durable_task_progress or complete_durable_task" in rejected.content
    assert rejected.runtime_failure is not None
    assert rejected.runtime_failure.kind == "state_conflict"
    assert (await store.get_task(task.id)) == claimed
    repeated = await registry.dispatch("claim_durable_task", claim, ctx)
    assert repeated.runtime_failure == rejected.runtime_failure

    progress = await registry.dispatch(
        "update_durable_task_progress",
        {"task_id": task.id, "current_summary": "Work performed"},
        ctx,
    )
    assert not progress.is_error
    changed = await registry.dispatch("claim_durable_task", claim, ctx)
    assert changed.runtime_failure is not None
    assert changed.runtime_failure.state_fingerprint != rejected.runtime_failure.state_fingerprint
    completed = await registry.dispatch(
        "complete_durable_task",
        {"task_id": task.id, "completion_summary": "Work performed and verified"},
        ctx,
    )
    assert not completed.is_error
    assert (await store.get_task(task.id)).status == "completed"
    assert task.id not in ctx.session.active_task_leases
    assert (tmp_path / "user").exists()
    assert not (tmp_path / "project").exists()


@pytest.mark.parametrize("credential", ["other_session", "missing", "stale_id", "stale_epoch"])
async def test_stale_claim_does_not_mistake_a_lease_for_session_ownership(
    tmp_path: Path, credential: str
):
    store, registry, ctx, task = await _context(tmp_path)
    holder = "other-session" if credential == "other_session" else ctx.session.id
    claimed = await store.claim(
        task.id, holder_session_id=holder, authority="agent_autonomy", executor_id=holder
    )
    assert claimed.lease is not None
    if credential == "other_session":
        ctx.session.active_task_leases[task.id] = claimed.lease
    elif credential == "stale_id":
        ctx.session.active_task_leases[task.id] = claimed.lease.model_copy(
            update={"id": "stale-lease"}
        )
    elif credential == "stale_epoch":
        ctx.session.active_task_leases[task.id] = claimed.lease.model_copy(
            update={"epoch": claimed.lease.epoch + 1}
        )
    result = await registry.dispatch(
        "claim_durable_task", {"task_id": task.id, "expected_revision": 1}, ctx
    )
    assert result.is_error
    assert "active lease that this session does not hold" in result.content
    assert "already holds" not in result.content
    assert result.runtime_failure is not None
    assert (await store.get_task(task.id)) == claimed


async def test_expired_cached_lease_never_advises_completion(tmp_path: Path):
    store, registry, ctx, task = await _context(tmp_path, expired=True)
    claimed = await store.claim(
        task.id,
        holder_session_id=ctx.session.id,
        authority="agent_autonomy",
        executor_id=ctx.session.id,
    )
    assert claimed.lease is not None
    ctx.session.active_task_leases[task.id] = claimed.lease
    result = await registry.dispatch(
        "claim_durable_task", {"task_id": task.id, "expected_revision": 1}, ctx
    )
    assert result.is_error
    assert "already holds" not in result.content
    assert "expected_revision 2" in result.content
    assert (await store.get_task(task.id)) == claimed


async def test_missing_revision_is_actionable_and_exact_revision_still_required(tmp_path: Path):
    store, registry, ctx, task = await _context(tmp_path)
    result = await registry.dispatch("claim_durable_task", {"task_id": task.id}, ctx)
    assert result.is_error
    assert "expected revision None; current revision is 1" in result.content
    assert "expected_revision 1" in result.content
    assert (await store.get_task(task.id)) == task
    valid = await registry.dispatch(
        "claim_durable_task", {"task_id": task.id, "expected_revision": 1}, ctx
    )
    assert not valid.is_error
    assert valid.runtime_failure is None
