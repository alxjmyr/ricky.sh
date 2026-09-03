"""Prepared browser effects park before the shared external-effect reservation."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, cast
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict

from ricky.agent.session import AgentSession
from ricky.browser.types import BrowserTransactionEnvelope
from ricky.config import RickySettings
from ricky.executions.browser import (
    BrowserApprovalDraft,
    BrowserExecutionBudget,
    BrowserExecutionScope,
    BrowserLiveBinding,
    BrowserTransactionApprovalDraft,
    ParkedBrowserApproval,
    envelope_digest,
)
from ricky.executions.browser_runtime import (
    BackgroundBrowserApprovalContext,
    BackgroundBrowserApprovalCoordinator,
)
from ricky.executions.parked import ParkedPreparedEffectTool
from ricky.executions.store import ExecutionStore
from ricky.executions.types import ExecutionRequest
from ricky.jobs.browser_store import BrowserRunLedger
from ricky.jobs.effects import GuardedEffectTool
from ricky.jobs.store import JobRunStore
from ricky.jobs.types import JobRun
from ricky.profiles import ProfileResourceRef, ProfileScope
from ricky.protected_values import DestinationApprovalRequest, ProtectedOccurrenceBinding
from ricky.tools import EffectIdentity, EffectReceipt, Tool, ToolContext, ToolResult

SCOPE = ProfileScope.create("personal")
PRINCIPAL = "telegram:owner:42"
CONVERSATION = "conversation_browser_owner"
BUDGET = BrowserExecutionBudget(
    session_starts=1,
    navigations=5,
    scrolls=10,
    created_pages=2,
    controlled_pages=2,
    semantic_observations=10,
    visual_observations=2,
    interactions=10,
    protected_materializations=2,
    uploads=1,
    upload_bytes=1_000_000,
    downloads=1,
    download_bytes=1_000_000,
    transaction_commits=1,
    parked_browsers=1,
    approval_ttl_seconds=120,
)


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


@dataclass(frozen=True)
class _Prepared:
    tool_name: str
    identity: EffectIdentity
    permission_summary: str | None


class _PreparedTool:
    name: ClassVar[str] = "browser_commit"
    description: ClassVar[str] = "Commit one test browser transaction."
    Params: ClassVar[type[BaseModel]] = _Params
    risk: ClassVar[str] = "mutating"
    capability_id: ClassVar[str] = "browser"
    effect_kind: ClassVar[str] = "external"
    unattended: ClassVar[str] = "allowed"
    state_guard_id: ClassVar[None] = None
    review_mode: ClassVar[str] = "fresh"

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        del args, ctx
        return self._identity()

    async def prepare_effect(
        self,
        args: dict[str, object],
        ctx: ToolContext,
    ) -> _Prepared:
        del args, ctx
        self.events.append("inner_prepare")
        return _Prepared(self.name, self._identity(), "Commit exact registration")

    async def run_prepared(
        self,
        params: BaseModel,
        prepared: Any,
        ctx: ToolContext,
    ) -> ToolResult:
        del params, prepared, ctx
        self.events.append("inner_dispatch")
        return ToolResult(
            content="committed",
            effect_receipt=EffectReceipt(disposition="performed", provider_reference="test-1"),
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        return await self.run_prepared(params, await self.prepare_effect({}, ctx), ctx)

    @staticmethod
    def _identity() -> EffectIdentity:
        return EffectIdentity(
            operation="browser.commit",
            target="https://example.com",
            occurrence="prepared-browser-occurrence",
            summary="Commit registration",
            action_key="a" * 64,
        )


class _LoggingRunStore(JobRunStore):
    def __init__(self, settings: RickySettings, events: list[str]) -> None:
        super().__init__(settings)
        self.events = events

    async def reserve_action(self, **kwargs: Any):
        self.events.append("effect_reserve")
        return await super().reserve_action(**kwargs)


async def _running(
    settings: RickySettings,
) -> tuple[ExecutionStore, ExecutionRequest, str, JobRun]:
    now = datetime.now(UTC)
    store = ExecutionStore(settings)
    await store.initialize()
    request = await store.submit(
        ExecutionRequest(
            id=f"execution_{uuid4().hex}",
            kind="ad_hoc",
            status="queued",
            goal="Commit one reviewed browser transaction.",
            contract_id=f"contract_{uuid4().hex}",
            contract_digest="a" * 64,
            task_id=f"task_{uuid4().hex}",
            task_revision=1,
            profile_scope=SCOPE,
            source_conversation_id=CONVERSATION,
            source_message_id="message_proposal",
            notification_route=f"conversation:{CONVERSATION}",
            request_key=f"browser:{uuid4().hex}",
            created_at=now,
            expires_at=now + timedelta(minutes=10),
        ),
        scope=SCOPE,
    )
    [claimed] = await store.claim(worker_id="worker", scope=SCOPE, limit=1)
    assert claimed.claim_token is not None
    run = JobRun(
        id=f"jobrun_{uuid4().hex}",
        provider="openrouter",
        model="test",
        profile_scope=SCOPE,
        session_id=f"session_{uuid4().hex}",
        started_at=now,
        trigger="execution",
        trigger_id=request.id,
    )
    jobs = JobRunStore(settings)
    await jobs.initialize()
    await jobs.insert(run, scope=SCOPE)
    running = await store.start(
        request.id,
        scope=SCOPE,
        token=claimed.claim_token,
        fence=claimed.claim_fence,
        run_id=run.id,
    )
    return store, running, claimed.claim_token, run


def _projector(request: ExecutionRequest):
    def project(prepared: Any, args: dict[str, object], ctx: ToolContext):
        del prepared, args, ctx
        now = datetime.now(UTC)
        envelope = BrowserTransactionEnvelope(
            kind="browser",
            intent="Submit the reviewed registration form",
            destination="Example registration service",
            consequences=("Creates one registration",),
            disclosures=(),
            expected_result="A registration confirmation is displayed",
        )
        assert request.run_id is not None
        return BrowserTransactionApprovalDraft(
            id=f"browser_transaction_{uuid4().hex}",
            request_id=request.id,
            run_id=request.run_id,
            attempt_id=f"browser_attempt_{uuid4().hex}",
            claim_fence=request.claim_fence,
            prepared_effect_digest="b" * 64,
            logical_effect_key="d" * 64,
            logical_transaction_id=f"browser_logical_{uuid4().hex}",
            review_digest="c" * 64,
            binding=BrowserLiveBinding(
                occurrence_digest="1" * 64,
                resource_digest="4" * 64,
                resource_kind="ephemeral",
                provider="openrouter",
                session_digest="5" * 64,
                page_digest="6" * 64,
                budget_ceiling=BUDGET,
                page_generation=1,
                snapshot_digest="2" * 64,
                target_digest="3" * 64,
                target_description="Submit registration",
                top_level_origin="https://example.com",
                target_frame_origin="https://example.com",
            ),
            principal_id=PRINCIPAL,
            conversation_id=CONVERSATION,
            proposal_source_message_id="message_proposal",
            created_at=now,
            expires_at=now + timedelta(minutes=2),
            target_mode="semantic",
            envelope=envelope,
            envelope_digest=envelope_digest(envelope),
        )

    return project


def _context(settings: RickySettings, tmp_path: Path) -> ToolContext:
    return ToolContext(
        cwd=tmp_path,
        settings=settings,
        session=AgentSession.create(
            settings,
            profile_scope=SCOPE,
            provider="openrouter",
            model="test",
        ),
    )


async def test_approval_preparation_precedes_effect_reservation_and_dispatch(
    tmp_path: Path,
) -> None:
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    store, request, token, run = await _running(settings)
    events: list[str] = []
    release_count = 0

    async def notify(challenge) -> None:
        events.append("notify")
        await store.decide_browser_approval(
            challenge.approval.id,
            scope=SCOPE,
            approve=True,
            principal_id=PRINCIPAL,
            conversation_id=CONVERSATION,
            source_message_id="message_approve",
            code=challenge.code,
        )

    async def reserve(draft: BrowserApprovalDraft) -> None:
        del draft
        events.append("park_reserve")

    async def release(approval: BrowserApprovalDraft | ParkedBrowserApproval) -> None:
        nonlocal release_count
        del approval
        release_count += 1
        events.append("park_release")

    async def revalidate(approval, prepared, ctx) -> bool:
        del approval, prepared, ctx
        events.append("revalidate")
        return True

    parked = ParkedPreparedEffectTool(
        cast(Tool, _PreparedTool(events)),
        store=store,
        scope=SCOPE,
        claim_token=token,
        claim_fence=request.claim_fence,
        projector=_projector(request),
        notifier=notify,
        revalidator=revalidate,
        park_reserver=reserve,
        park_releaser=release,
    )
    guarded = GuardedEffectTool(
        cast(Tool, parked),
        store=_LoggingRunStore(settings, events),
        job_name="ad-hoc-browser",
        run_id=run.id,
        profile_scope=SCOPE,
        effect_budget=1,
    )
    result = await guarded.run(_Params(value="reviewed"), _context(settings, tmp_path))

    assert result.effect_receipt is not None
    assert result.effect_receipt.disposition == "performed"
    assert events == [
        "inner_prepare",
        "park_reserve",
        "notify",
        "revalidate",
        "effect_reserve",
        "park_release",
        "inner_dispatch",
    ]
    assert release_count == 1
    [approval] = await store.browser_approvals_for_request(request.id, scope=SCOPE)
    assert approval.state == "consumed"


async def test_approved_park_is_invalidated_when_effect_reservation_is_denied(
    tmp_path: Path,
) -> None:
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    store, request, token, run = await _running(settings)
    events: list[str] = []

    async def notify(challenge) -> None:
        events.append("notify")
        await store.decide_browser_approval(
            challenge.approval.id,
            scope=SCOPE,
            approve=True,
            principal_id=PRINCIPAL,
            conversation_id=CONVERSATION,
            source_message_id="message_approve",
            code=challenge.code,
        )

    async def reserve(draft: BrowserApprovalDraft) -> None:
        del draft
        events.append("park_reserve")

    async def release(approval: BrowserApprovalDraft | ParkedBrowserApproval) -> None:
        del approval
        events.append("park_release")

    async def revalidate(approval, prepared, ctx) -> bool:
        del approval, prepared, ctx
        events.append("revalidate")
        return True

    parked = ParkedPreparedEffectTool(
        cast(Tool, _PreparedTool(events)),
        store=store,
        scope=SCOPE,
        claim_token=token,
        claim_fence=request.claim_fence,
        projector=_projector(request),
        notifier=notify,
        revalidator=revalidate,
        park_reserver=reserve,
        park_releaser=release,
    )
    guarded = GuardedEffectTool(
        cast(Tool, parked),
        store=_LoggingRunStore(settings, events),
        job_name="ad-hoc-browser",
        run_id=run.id,
        profile_scope=SCOPE,
        effect_budget=0,
    )

    result = await guarded.run(_Params(value="reviewed"), _context(settings, tmp_path))

    assert result.is_error
    assert result.effect_receipt is not None
    assert result.effect_receipt.disposition == "not_performed"
    assert events == [
        "inner_prepare",
        "park_reserve",
        "notify",
        "revalidate",
        "effect_reserve",
        "park_release",
    ]
    [approval] = await store.browser_approvals_for_request(request.id, scope=SCOPE)
    assert approval.state == "invalidated"
    assert (await store.get(request.id, scope=SCOPE)).status == "running"


async def test_denial_releases_park_without_reserving_or_dispatching(tmp_path: Path) -> None:
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    store, request, token, run = await _running(settings)
    events: list[str] = []

    async def notify(challenge) -> None:
        events.append("notify")
        await store.decide_browser_approval(
            challenge.approval.id,
            scope=SCOPE,
            approve=False,
            principal_id=PRINCIPAL,
            conversation_id=CONVERSATION,
            source_message_id="message_deny",
            code=challenge.code,
        )

    async def reserve(draft: BrowserApprovalDraft) -> None:
        del draft
        events.append("park_reserve")

    async def release(approval: BrowserApprovalDraft | ParkedBrowserApproval) -> None:
        del approval
        events.append("park_release")

    async def revalidate(approval, prepared, ctx) -> bool:
        del approval, prepared, ctx
        raise AssertionError("denied approval cannot revalidate")

    parked = ParkedPreparedEffectTool(
        cast(Tool, _PreparedTool(events)),
        store=store,
        scope=SCOPE,
        claim_token=token,
        claim_fence=request.claim_fence,
        projector=_projector(request),
        notifier=notify,
        revalidator=revalidate,
        park_reserver=reserve,
        park_releaser=release,
    )
    guarded = GuardedEffectTool(
        cast(Tool, parked),
        store=_LoggingRunStore(settings, events),
        job_name="ad-hoc-browser",
        run_id=run.id,
        profile_scope=SCOPE,
        effect_budget=1,
    )
    result = await guarded.run(_Params(value="reviewed"), _context(settings, tmp_path))

    assert result.effect_receipt is not None
    assert result.effect_receipt.disposition == "not_performed"
    assert events == ["inner_prepare", "park_reserve", "notify", "park_release"]
    assert await JobRunStore(settings).actions_for_run(run.id, scope=SCOPE) == []
    [approval] = await store.browser_approvals_for_request(request.id, scope=SCOPE)
    assert approval.state == "denied"
    assert (await store.get(request.id, scope=SCOPE)).status == "running"


async def test_cancelled_wait_invalidates_and_releases_before_propagating(
    tmp_path: Path,
) -> None:
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    store, request, token, run = await _running(settings)
    events: list[str] = []
    notified = asyncio.Event()

    async def notify(challenge) -> None:
        del challenge
        events.append("notify")
        notified.set()

    async def reserve(draft: BrowserApprovalDraft) -> None:
        del draft
        events.append("park_reserve")

    async def release(approval: BrowserApprovalDraft | ParkedBrowserApproval) -> None:
        del approval
        events.append("park_release")

    async def revalidate(approval, prepared, ctx) -> bool:
        del approval, prepared, ctx
        return True

    parked = ParkedPreparedEffectTool(
        cast(Tool, _PreparedTool(events)),
        store=store,
        scope=SCOPE,
        claim_token=token,
        claim_fence=request.claim_fence,
        projector=_projector(request),
        notifier=notify,
        revalidator=revalidate,
        park_reserver=reserve,
        park_releaser=release,
    )
    guarded = GuardedEffectTool(
        cast(Tool, parked),
        store=_LoggingRunStore(settings, events),
        job_name="ad-hoc-browser",
        run_id=run.id,
        profile_scope=SCOPE,
        effect_budget=1,
    )
    operation = asyncio.create_task(
        guarded.run(_Params(value="reviewed"), _context(settings, tmp_path))
    )
    await asyncio.wait_for(notified.wait(), timeout=2)
    operation.cancel()
    try:
        await operation
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancelled approval wait did not propagate cancellation")

    assert events == ["inner_prepare", "park_reserve", "notify", "park_release"]
    assert await JobRunStore(settings).actions_for_run(run.id, scope=SCOPE) == []
    [approval] = await store.browser_approvals_for_request(request.id, scope=SCOPE)
    assert approval.state == "invalidated"
    assert (await store.get(request.id, scope=SCOPE)).status == "running"


async def test_cancelled_protected_destination_wait_invalidates_and_releases(
    tmp_path: Path,
) -> None:
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    store, request, token, run = await _running(settings)
    ledger = BrowserRunLedger(settings)
    scope = BrowserExecutionScope(
        mode="transaction",
        allow_ephemeral=True,
        allow_public_https_research=True,
        allowed_tools=("browser_fill_protected",),
        allowed_operations=(
            "controlled_pages",
            "created_pages",
            "parked_browsers",
            "protected_materializations",
        ),
        budget=BrowserExecutionBudget(
            session_starts=0,
            navigations=0,
            scrolls=0,
            created_pages=1,
            controlled_pages=1,
            semantic_observations=0,
            visual_observations=0,
            interactions=0,
            protected_materializations=1,
            uploads=0,
            upload_bytes=0,
            downloads=0,
            download_bytes=0,
            transaction_commits=0,
            parked_browsers=1,
            approval_ttl_seconds=120,
        ),
    )
    attempt = await ledger.start_attempt(
        run_id=run.id,
        scope=SCOPE,
        browser_scope=scope,
        claim_fence=request.claim_fence,
        worker_id="worker",
        execution_request_id=request.id,
        resource=None,
        resource_kind="ephemeral",
        resource_configuration_digest=None,
    )
    await ledger.transition(
        attempt.attempt.id,
        scope=SCOPE,
        owner_token=attempt.owner_token,
        claim_fence=request.claim_fence,
        status="running",
    )
    notified = asyncio.Event()

    async def notify(_challenge) -> None:
        notified.set()

    coordinator = BackgroundBrowserApprovalCoordinator(
        context=BackgroundBrowserApprovalContext(
            request_id=request.id,
            task_id=cast(str, request.task_id),
            contract_digest=cast(str, request.contract_digest),
            run_id=run.id,
            attempt_id=attempt.attempt.id,
            claim_token=token,
            claim_fence=request.claim_fence,
            principal_id=PRINCIPAL,
            conversation_id=CONVERSATION,
            source_message_id="message_proposal",
            approval_ttl_seconds=120,
            profile_scope=SCOPE,
            owner_token=attempt.owner_token,
            provider="openrouter",
            resource=None,
            resource_kind="ephemeral",
            resource_configuration_digest=None,
            budget_ceiling=scope.budget,
        ),
        store=store,
        ledger=ledger,
        notifier=notify,
        protected_values=None,
    )
    operation = asyncio.create_task(
        coordinator.approve_destination(
            DestinationApprovalRequest(
                ref=ProfileResourceRef(profile="personal", name="card"),
                revision=1,
                field="number",
                label="Card number",
                top_level_origin="https://example.com",
                frame_origin="https://example.com",
                occurrence="protected-use-occurrence",
                execution_mode="unattended",
                binding=ProtectedOccurrenceBinding(
                    generation=3,
                    observation_id="browser_snapshot_" + "a" * 32,
                    target_id="e1",
                ),
            )
        )
    )
    await asyncio.wait_for(notified.wait(), timeout=2)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation

    [approval] = await store.browser_approvals_for_request(request.id, scope=SCOPE)
    assert approval.state == "invalidated"
    assert (await store.get(request.id, scope=SCOPE)).status == "running"
    assert (await ledger.get_attempt(attempt.attempt.id, scope=SCOPE)).status == "running"
    usage = {
        item.operation: item.used
        for item in await ledger.budget_usage(
            attempt.attempt.id,
            scope=SCOPE,
        )
    }
    assert usage["parked_browsers"] == 0
