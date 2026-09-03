"""Fenced browser-attempt lifecycle and durable budget tests."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

import pytest

from ricky.config import RickySettings
from ricky.executions.browser import (
    BrowserActionEvidence,
    BrowserExecutionBudget,
    BrowserExecutionScope,
    BrowserNavigationCheckpoint,
    BrowserProtectedResourcePin,
)
from ricky.executions.store import ExecutionStore
from ricky.executions.types import ExecutionRequest
from ricky.jobs.browser_store import (
    BrowserAttemptFenceError,
    BrowserBudgetExceededError,
    BrowserRunLedger,
)
from ricky.jobs.runner import JobRunner
from ricky.jobs.store import JobRunStore, JobStoreError
from ricky.jobs.types import JobRun
from ricky.profiles import ProfileResourceRef, ProfileScope

SCOPE = ProfileScope.create("personal")


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings(user_data_dir=str(tmp_path / "user"))


def _budget() -> BrowserExecutionBudget:
    return BrowserExecutionBudget(
        session_starts=1,
        navigations=4,
        scrolls=4,
        created_pages=4,
        controlled_pages=2,
        semantic_observations=4,
        visual_observations=0,
        interactions=2,
        protected_materializations=0,
        uploads=0,
        upload_bytes=0,
        downloads=0,
        download_bytes=0,
        transaction_commits=1,
        parked_browsers=1,
        approval_ttl_seconds=120,
    )


def _scope(*, mode: str = "transaction") -> BrowserExecutionScope:
    tools = (
        "browser_session_open",
        "browser_navigate",
        "browser_snapshot",
        "browser_scroll",
        "browser_click",
        "browser_commit",
    )
    operations = (
        "session_starts",
        "navigations",
        "scrolls",
        "created_pages",
        "controlled_pages",
        "semantic_observations",
        "interactions",
        "transaction_commits",
        "parked_browsers",
    )
    if mode == "read_only":
        tools = tools[:4]
        operations = operations[:6]
    return BrowserExecutionScope.model_validate(
        {
            "mode": mode,
            "allow_ephemeral": True,
            "allow_public_https_research": True,
            "allowed_tools": tools,
            "allowed_operations": operations,
            "budget": _budget().model_dump(mode="python"),
        }
    )


async def _run(
    settings: RickySettings,
    *,
    trigger: Literal["manual", "execution"] = "execution",
) -> JobRun:
    request_id = f"execution_{uuid4().hex}"
    run = JobRun(
        id=f"jobrun_{uuid4().hex}",
        job_name=None if trigger == "execution" else "personal/research",
        provider="openrouter",
        model="test",
        profile_scope=SCOPE,
        session_id=f"session_{uuid4().hex}",
        started_at=datetime.now(UTC),
        trigger=trigger,
        trigger_id=request_id if trigger == "execution" else None,
    )
    store = JobRunStore(settings)
    await store.initialize()
    await store.insert(run, scope=SCOPE)
    return run


async def _attempt(
    ledger: BrowserRunLedger,
    run: JobRun,
    scope: BrowserExecutionScope,
):
    lease = await ledger.start_attempt(
        run_id=run.id,
        scope=SCOPE,
        browser_scope=scope,
        claim_fence=1,
        worker_id="worker",
        execution_request_id=run.trigger_id,
        resource=None,
        resource_kind="ephemeral",
        resource_configuration_digest=None,
    )
    await ledger.transition(
        lease.attempt.id,
        scope=SCOPE,
        owner_token=lease.owner_token,
        claim_fence=1,
        status="running",
    )
    return lease


async def test_browser_attempt_budgets_are_atomic_releasable_and_fenced(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    run = await _run(settings)
    ledger = BrowserRunLedger(settings)
    lease = await _attempt(ledger, run, _scope())

    results = await asyncio.gather(
        *(
            ledger.reserve_budget(
                lease.attempt.id,
                "interactions",
                scope=SCOPE,
                owner_token=lease.owner_token,
                claim_fence=1,
            )
            for _ in range(3)
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, BaseException) for result in results) == 2
    assert sum(isinstance(result, BrowserBudgetExceededError) for result in results) == 1

    controlled = await ledger.reserve_budget(
        lease.attempt.id,
        "controlled_pages",
        amount=2,
        scope=SCOPE,
        owner_token=lease.owner_token,
        claim_fence=1,
    )
    assert controlled.used == 2
    released = await ledger.release_live_budget(
        lease.attempt.id,
        "controlled_pages",
        amount=1,
        scope=SCOPE,
        owner_token=lease.owner_token,
        claim_fence=1,
    )
    assert released.used == 1
    with pytest.raises(JobStoreError, match="release exceeds"):
        await ledger.release_live_budget(
            lease.attempt.id,
            "controlled_pages",
            amount=2,
            scope=SCOPE,
            owner_token=lease.owner_token,
            claim_fence=1,
        )
    with pytest.raises(BrowserAttemptFenceError):
        await ledger.reserve_budget(
            lease.attempt.id,
            "navigations",
            scope=SCOPE,
            owner_token="browser_owner_00000000000000000000000000000000",
            claim_fence=1,
        )


async def test_popup_capacity_settles_actual_use_and_keeps_ambiguous_maximum(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    run = await _run(settings)
    ledger = BrowserRunLedger(settings)
    lease = await _attempt(ledger, run, _scope())

    first = await ledger.reserve_possible_pages(
        lease.attempt.id,
        maximum=3,
        reservation_key="1" * 64,
        scope=SCOPE,
        owner_token=lease.owner_token,
        claim_fence=1,
    )
    first = await ledger.settle_possible_pages(
        first.id,
        consumed=1,
        in_doubt=False,
        scope=SCOPE,
        owner_token=lease.owner_token,
        claim_fence=1,
    )
    assert first.state == "settled" and first.consumed == 1

    second = await ledger.reserve_possible_pages(
        lease.attempt.id,
        maximum=3,
        reservation_key="2" * 64,
        scope=SCOPE,
        owner_token=lease.owner_token,
        claim_fence=1,
    )
    second = await ledger.settle_possible_pages(
        second.id,
        consumed=0,
        in_doubt=True,
        scope=SCOPE,
        owner_token=lease.owner_token,
        claim_fence=1,
    )
    assert second.state == "in_doubt" and second.consumed == 3
    usage = {
        item.operation: item for item in await ledger.budget_usage(lease.attempt.id, scope=SCOPE)
    }
    assert usage["created_pages"].used == 4

    same = await ledger.settle_possible_pages(
        second.id,
        consumed=0,
        in_doubt=True,
        scope=SCOPE,
        owner_token=lease.owner_token,
        claim_fence=1,
    )
    assert same == second


async def test_attempt_records_safe_checkpoint_and_recovers_lost_owner(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    run = await _run(settings)
    ledger = BrowserRunLedger(settings)
    lease = await _attempt(ledger, run, _scope())
    checkpoint = await ledger.checkpoint_navigation(
        BrowserNavigationCheckpoint(
            attempt_id=lease.attempt.id,
            page_generation=2,
            top_level_origin="https://example.com",
            url_projection="https://example.com",
            created_at=datetime.now(UTC),
        ),
        scope=SCOPE,
        owner_token=lease.owner_token,
        claim_fence=1,
    )
    evidence = await ledger.record_action_evidence(
        BrowserActionEvidence(
            attempt_id=lease.attempt.id,
            operation="browser_snapshot",
            live_occurrence_digest="a" * 64,
            disposition="observed",
            postcondition="semantic snapshot observed",
            created_at=datetime.now(UTC),
        ),
        scope=SCOPE,
        owner_token=lease.owner_token,
        claim_fence=1,
    )
    assert checkpoint.id is not None and evidence.id is not None
    assert await ledger.action_evidence(lease.attempt.id, scope=SCOPE) == [evidence]

    finished = run.model_copy(update={"outcome": "interrupted", "finished_at": datetime.now(UTC)})
    await ledger.runs.finish(finished, scope=SCOPE)
    [recovered] = await ledger.recover_orphaned(scope=SCOPE)
    assert recovered.status == "failed"
    assert recovered.cleanup == "failed"


async def test_lost_attempt_keeps_effectful_evidence_in_doubt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    run = await _run(settings)
    ledger = BrowserRunLedger(settings)
    lease = await _attempt(ledger, run, _scope())
    await ledger.record_action_evidence(
        BrowserActionEvidence(
            attempt_id=lease.attempt.id,
            operation="browser_click",
            logical_effect_key="f" * 64,
            live_occurrence_digest="e" * 64,
            disposition="performed",
            postcondition="browser dispatch completed before owner loss",
            created_at=datetime.now(UTC),
        ),
        scope=SCOPE,
        owner_token=lease.owner_token,
        claim_fence=1,
    )

    assert await ledger.has_ambiguous_effect_evidence(lease.attempt.id, scope=SCOPE)
    recovered = await ledger.recover_lost_attempt(
        lease.attempt.id,
        scope=SCOPE,
        reason="synthetic worker loss",
    )

    assert recovered.status == "in_doubt"
    assert recovered.cleanup == "failed"
    assert await ledger.active_attempts(scope=SCOPE) == []


async def test_named_jobs_cannot_start_transaction_browser_attempts(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    run = await _run(settings, trigger="manual")
    ledger = BrowserRunLedger(settings)
    with pytest.raises(JobStoreError, match="ad hoc execution"):
        await ledger.start_attempt(
            run_id=run.id,
            scope=SCOPE,
            browser_scope=_scope(),
            claim_fence=1,
            worker_id="worker",
            execution_request_id=None,
            resource=None,
            resource_kind="ephemeral",
            resource_configuration_digest=None,
        )
    read_only = await ledger.start_attempt(
        run_id=run.id,
        scope=SCOPE,
        browser_scope=_scope(mode="read_only"),
        claim_fence=1,
        worker_id="worker",
        execution_request_id=None,
        resource=None,
        resource_kind="ephemeral",
        resource_configuration_digest=None,
    )
    assert read_only.attempt.mode == "read_only"


async def test_browser_setup_failure_terminalizes_seeded_run_and_attempt(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    scope = _scope().model_copy(
        update={
            "allowed_tools": ("browser_fill_protected",),
            "allowed_operations": (
                "controlled_pages",
                "created_pages",
                "navigations",
                "protected_materializations",
            ),
            "protected_resources": (
                BrowserProtectedResourcePin(
                    resource=ProfileResourceRef(profile="personal", name="card"),
                    revision=1,
                    fields=("security_code",),
                    materialization_limit=1,
                    commit_limit=1,
                ),
            ),
        }
    )
    now = datetime.now(UTC)
    request_id = f"execution_{uuid4().hex}"
    run = JobRun(
        id=f"jobrun_{uuid4().hex}",
        provider="openrouter",
        model="test",
        profile_scope=SCOPE,
        session_id=f"session_{uuid4().hex}",
        started_at=now,
        trigger="execution",
        trigger_id=request_id,
    )
    request = ExecutionRequest(
        id=request_id,
        kind="ad_hoc",
        status="running",
        goal="Use the protected value in the browser.",
        contract_id="contract_" + "a" * 32,
        contract_digest="b" * 64,
        task_id="task_" + "c" * 32,
        task_revision=1,
        profile_scope=SCOPE,
        source_conversation_id="conversation-owner",
        source_message_id="message-owner",
        notification_route="owner",
        request_key="setup-failure",
        created_at=now,
        claimed_by="gateway-worker",
        claim_token="claim-token",
        claim_fence=1,
        claim_expires_at=now.replace(year=now.year + 1),
        run_id=run.id,
    )

    class FailingResidentRegistry:
        unlocked_profiles = ("personal",)

        def lease(self, **_kwargs):
            raise RuntimeError("synthetic broker lease failure")

    async def notify(*_args) -> None:
        pass

    store = JobRunStore(settings)
    await store.initialize()
    runner = JobRunner(
        settings,
        store=store,
        execution_store=ExecutionStore(settings),
        protected_value_registry=cast(Any, FailingResidentRegistry()),
        browser_approval_notifier=notify,
    )

    with pytest.raises(RuntimeError, match="synthetic broker lease failure"):
        await runner._setup_background_browser(  # noqa: SLF001
            run=run,
            browser_scope=scope,
            profile_scope=SCOPE,
            provider="openrouter",
            contract_execution=True,
            loaded=None,
            execution_request=request,
            browser_principal_id="telegram:owner:1",
            trigger_id=request_id,
        )

    stored = await store.get(run.id, scope=SCOPE)
    assert stored.outcome == "failed"
    [attempt] = await BrowserRunLedger(settings).attempts_for_run(run.id, scope=SCOPE)
    assert attempt.status == "failed"
    assert attempt.cleanup == "confirmed"
    assert await BrowserRunLedger(settings).active_attempts(scope=SCOPE) == []


async def test_browser_setup_cancellation_joins_run_and_attempt_terminalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    now = datetime.now(UTC)
    request_id = f"execution_{uuid4().hex}"
    run = JobRun(
        id=f"jobrun_{uuid4().hex}",
        provider="openrouter",
        model="test",
        profile_scope=SCOPE,
        session_id=f"session_{uuid4().hex}",
        started_at=now,
        trigger="execution",
        trigger_id=request_id,
    )
    request = ExecutionRequest(
        id=request_id,
        kind="ad_hoc",
        status="running",
        goal="Browse the public site.",
        contract_id="contract_" + "d" * 32,
        contract_digest="e" * 64,
        task_id="task_" + "f" * 32,
        task_revision=1,
        profile_scope=SCOPE,
        notification_route="owner",
        request_key="setup-cancellation",
        created_at=now,
        claimed_by="gateway-worker",
        claim_token="claim-token",
        claim_fence=1,
        claim_expires_at=now.replace(year=now.year + 1),
        run_id=run.id,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    original_transition = BrowserRunLedger.transition

    async def blocking_transition(self, attempt_id, **kwargs):
        if kwargs["status"] == "running":
            entered.set()
            await release.wait()
        return await original_transition(self, attempt_id, **kwargs)

    monkeypatch.setattr(BrowserRunLedger, "transition", blocking_transition)

    async def notify(*_args) -> None:
        pass

    store = JobRunStore(settings)
    await store.initialize()
    runner = JobRunner(
        settings,
        store=store,
        execution_store=ExecutionStore(settings),
        browser_approval_notifier=notify,
    )
    setup = asyncio.create_task(
        runner._setup_background_browser(  # noqa: SLF001
            run=run,
            browser_scope=_scope(),
            profile_scope=SCOPE,
            provider="openrouter",
            contract_execution=True,
            loaded=None,
            execution_request=request,
            browser_principal_id="telegram:owner:1",
            trigger_id=request_id,
        )
    )
    await entered.wait()
    setup.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await setup

    stored = await store.get(run.id, scope=SCOPE)
    assert stored.outcome == "interrupted"
    [attempt] = await BrowserRunLedger(settings).attempts_for_run(run.id, scope=SCOPE)
    assert attempt.status == "cancelled"
    assert attempt.cleanup == "confirmed"
    assert await BrowserRunLedger(settings).active_attempts(scope=SCOPE) == []
