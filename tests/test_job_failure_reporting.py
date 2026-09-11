"""Failure reporting preserves durable performed receipts without replay."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from ricky.config import RickySettings
from ricky.jobs.reporting import failure_summary
from ricky.jobs.store import JobRunStore, JobStoreError
from ricky.jobs.types import JobRun
from ricky.profiles import ProfileScope
from ricky.tools.base import EffectIdentity

SCOPE = ProfileScope.create("personal")


async def _run(tmp_path: Path) -> tuple[RickySettings, JobRunStore, JobRun]:
    settings = RickySettings(
        user_data_dir=str(tmp_path / "user"), project_data_dir=str(tmp_path / "project")
    )
    store = JobRunStore(settings)
    await store.initialize()
    run = JobRun(
        id="jobrun_receipts",
        job_name="personal/example",
        provider="openrouter",
        model="test",
        session_id="session",
        profile_scope=SCOPE,
        started_at=datetime.now(UTC),
        outcome="failed",
        error="Task revision conflict",
        final_message="Everything succeeded.",
    )
    await store.insert(run.model_copy(update={"outcome": None}), scope=SCOPE)
    return settings, store, run


async def _effect(
    store: JobRunStore,
    run: JobRun,
    occurrence: str,
    status: Literal["reserved", "performed", "not_performed", "in_doubt"],
):
    identity = EffectIdentity(
        action_key=hashlib.sha256(occurrence.encode()).hexdigest(),
        operation="gmail.trash",
        target="message",
        occurrence=occurrence,
        summary="Move message to Trash",
    )
    action = await store.reserve_action(
        job_name="personal/example", run_id=run.id, identity=identity, effect_budget=20, scope=SCOPE
    )
    if status != "reserved":
        await store.resolve_action(action.id, status, scope=SCOPE, provider_reference="private-ref")
    return action, identity


async def test_failed_run_summary_retains_performed_receipt_and_no_replay(tmp_path: Path) -> None:
    settings, store, run = await _run(tmp_path)
    action, identity = await _effect(store, run, "first", "performed")
    await store.finish(run.model_copy(update={"finished_at": datetime.now(UTC)}), scope=SCOPE)
    before = await store.actions_for_run(run.id, scope=SCOPE)
    summary = await failure_summary(run, run.error or "", store=store, scope=SCOPE, limit=1000)
    assert "Run failed" in summary
    assert "Task revision conflict" in summary
    assert "gmail.trash" in summary and action.id in summary
    assert "Everything succeeded" not in summary and "private-ref" not in summary
    assert await store.actions_for_run(run.id, scope=SCOPE) == before
    with pytest.raises(JobStoreError):
        await store.reserve_action(
            job_name="personal/example",
            run_id=run.id,
            identity=identity,
            effect_budget=20,
            scope=SCOPE,
        )
    assert store.root.is_relative_to(Path(settings.user_data_dir))
    assert not Path(settings.project_data_dir).exists()


@pytest.mark.parametrize("limit", [100, 300, 1000])
async def test_mixed_receipts_preserve_uncertainty_and_bound_text(
    tmp_path: Path, limit: int
) -> None:
    _, store, run = await _run(tmp_path)
    for index in range(8):
        await _effect(store, run, str(index), "performed")
    await _effect(store, run, "unknown", "in_doubt")
    run = run.model_copy(update={"outcome": "uncertain", "finished_at": datetime.now(UTC)})
    await store.finish(run, scope=SCOPE)
    summary = await failure_summary(
        run, "ambiguous operation " * 100, store=store, scope=SCOPE, limit=limit
    )
    assert len(summary) <= limit
    assert "Run uncertain" in summary
    assert "8 performed" in summary
    assert "Unresolved: 1; review required" in summary
    if limit >= 300:
        assert "omitted" in summary


async def test_no_performed_receipts_preserves_error_and_scope(tmp_path: Path) -> None:
    _, store, run = await _run(tmp_path)
    assert (
        await failure_summary(run, "original error", store=store, scope=SCOPE, limit=100)
        == "original error"
    )
    await _effect(store, run, "unperformed", "not_performed")
    assert (
        await failure_summary(run, "original error", store=store, scope=SCOPE, limit=100)
        == "original error"
    )
    with pytest.raises(JobStoreError):
        await failure_summary(
            run, "error", store=store, scope=ProfileScope.create("work"), limit=100
        )


async def test_failure_reason_is_not_truncated_when_receipt_and_error_fit(tmp_path: Path) -> None:
    _, store, run = await _run(tmp_path)
    action, _ = await _effect(store, run, "first", "performed")
    reason = "a" * 2000
    summary = await failure_summary(run, reason, store=store, scope=SCOPE, limit=4000)
    assert reason in summary
    assert action.id in summary
