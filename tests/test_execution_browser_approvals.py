"""Durable parked-browser approval state, fencing, and replay tests."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ricky.browser.backend import (
    BackendCoordinatePreflight,
    BackendTargetDescriptor,
    BackendViewport,
)
from ricky.browser.service import BrowserPreparedCoordinateCommit
from ricky.browser.tools import PreparedBrowserCoordinateCommit
from ricky.browser.types import (
    BrowserCoordinateContext,
    BrowserCoordinateTarget,
    BrowserDialogPolicy,
    BrowserTransactionEnvelope,
    BrowserTransactionEvidence,
    CoordinateFallbackEvidence,
)
from ricky.config import ExecutionSettings, RickySettings
from ricky.executions.browser import (
    BrowserExecutionBudget,
    BrowserLiveBinding,
    BrowserProtectedUseEvidence,
    BrowserTransactionApprovalDraft,
    BrowserTransactionChallenge,
    CoordinateFallbackBinding,
    ProtectedDestinationApprovalDraft,
    envelope_digest,
    issue_parked_approval,
)
from ricky.executions.browser_runtime import (
    BackgroundBrowserApprovalContext,
    _coordinate_binding,
    _live_binding,
)
from ricky.executions.dispatcher import _browser_approval_body
from ricky.executions.store import BrowserApprovalError, ExecutionStore
from ricky.executions.types import ExecutionRequest
from ricky.profiles import ProfileResourceRef, ProfileScope
from ricky.tools import EffectIdentity

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


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings(
        user_data_dir=str(tmp_path / "user"),
        executions=ExecutionSettings(
            claim_seconds=10,
            heartbeat_seconds=2,
            concurrency=2,
        ),
    )


async def _running(
    store: ExecutionStore,
    now: datetime,
    *,
    conversation: str = CONVERSATION,
) -> tuple[ExecutionRequest, str]:
    request = await store.submit(
        ExecutionRequest(
            id=f"execution_{uuid4().hex}",
            kind="ad_hoc",
            status="queued",
            goal="Complete one exact browser transaction.",
            contract_id=f"contract_{uuid4().hex}",
            contract_digest="a" * 64,
            task_id=f"task_{uuid4().hex}",
            task_revision=1,
            profile_scope=SCOPE,
            source_conversation_id=conversation,
            source_message_id="message_proposal",
            notification_route=f"conversation:{conversation}",
            request_key=f"browser:{uuid4().hex}",
            created_at=now,
            expires_at=now + timedelta(minutes=10),
        ),
        scope=SCOPE,
    )
    [claimed] = await store.claim(
        worker_id="worker",
        scope=SCOPE,
        limit=1,
        now=now,
    )
    assert claimed.claim_token is not None
    running = await store.start(
        request.id,
        scope=SCOPE,
        token=claimed.claim_token,
        fence=claimed.claim_fence,
        run_id=f"jobrun_{uuid4().hex}",
    )
    return running, claimed.claim_token


def _draft(
    request: ExecutionRequest,
    now: datetime,
    *,
    logical_effect_key: str = "d" * 64,
    expires_at: datetime | None = None,
) -> BrowserTransactionApprovalDraft:
    assert request.run_id is not None
    envelope = BrowserTransactionEnvelope(
        kind="browser",
        intent="Submit the reviewed registration form",
        destination="Example registration service",
        consequences=("Creates one registration",),
        disclosures=("Shares the supplied contact information",),
        expected_result="A registration confirmation is displayed",
    )
    return BrowserTransactionApprovalDraft(
        id=f"browser_transaction_{uuid4().hex}",
        request_id=request.id,
        run_id=request.run_id,
        attempt_id=f"browser_attempt_{uuid4().hex}",
        claim_fence=request.claim_fence,
        prepared_effect_digest="b" * 64,
        logical_effect_key=logical_effect_key,
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
            page_generation=4,
            snapshot_digest="2" * 64,
            target_digest="3" * 64,
            target_description="Submit registration",
            top_level_origin="https://example.com",
            target_frame_origin="https://example.com",
            destination_projections=("https://example.com/register",),
        ),
        principal_id=PRINCIPAL,
        conversation_id=CONVERSATION,
        proposal_source_message_id="message_proposal",
        created_at=now,
        expires_at=expires_at or now + timedelta(minutes=2),
        target_mode="semantic",
        envelope=envelope,
        envelope_digest=envelope_digest(envelope),
    )


def test_transaction_approval_requires_complete_trusted_review_context() -> None:
    now = datetime(2026, 8, 31, 12, tzinfo=UTC)
    envelope = BrowserTransactionEnvelope(
        kind="browser",
        intent="Submit the reviewed registration form",
        destination="Example registration service",
        consequences=("Creates one registration",),
        disclosures=(),
        expected_result="A registration confirmation is displayed",
    )

    with pytest.raises(ValidationError, match="lacks trusted review context"):
        BrowserTransactionApprovalDraft(
            id=f"browser_transaction_{uuid4().hex}",
            request_id=f"execution_{uuid4().hex}",
            run_id=f"jobrun_{uuid4().hex}",
            attempt_id=f"browser_attempt_{uuid4().hex}",
            claim_fence=1,
            prepared_effect_digest="a" * 64,
            logical_effect_key="b" * 64,
            logical_transaction_id=f"browser_logical_{uuid4().hex}",
            review_digest="c" * 64,
            binding=BrowserLiveBinding(
                occurrence_digest="d" * 64,
                page_generation=1,
                snapshot_digest="e" * 64,
                target_digest="f" * 64,
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


def test_coordinate_projection_preserves_fractional_css_binding_and_reason() -> None:
    target = BrowserCoordinateTarget(
        session_id="browser_session_" + "1" * 32,
        page_id="browser_page_" + "2" * 32,
        screenshot_id="browser_snapshot_" + "3" * 32,
        x=24.75,
        y=17.25,
    )
    frozen = BrowserPreparedCoordinateCommit(
        target=target,
        dialog=BrowserDialogPolicy(),
        context=BrowserCoordinateContext(
            target=target,
            resource=ProfileResourceRef(profile="personal", name=target.session_id),
            navigation_generation=4,
            url="https://example.com/checkout",
            origin="https://example.com",
            image_width=400,
            image_height=300,
            css_x=12.375,
            css_y=8.625,
            masked_base_sha256="4" * 64,
        ),
        viewport=BackendViewport(
            width=200,
            height=150,
            scroll_x=1.25,
            scroll_y=22.75,
            device_scale_factor=2,
        ),
        preflight=BackendCoordinatePreflight(
            target=BackendTargetDescriptor(
                ref="d1",
                role="canvas",
                name="Position-sensitive checkout",
                frame_origin="https://example.com",
                consequential=True,
            )
        ),
        payment_sources=(),
        fallback=CoordinateFallbackEvidence(
            semantic_snapshot_id="browser_snapshot_" + "5" * 32,
            reason="custom_rendered_target",
        ),
    )

    envelope = BrowserTransactionEnvelope(
        kind="browser",
        intent="Submit the position-sensitive checkout",
        destination="Example checkout",
        consequences=("Creates one registration",),
        disclosures=(),
        expected_result="The checkout displays confirmation",
    )
    digest = envelope_digest(envelope)
    prepared = PreparedBrowserCoordinateCommit(
        tool_name="browser_coordinate_commit",
        identity=EffectIdentity(
            operation="browser_coordinate_commit",
            target="https://example.com",
            occurrence="coordinate-occurrence",
            summary="Commit exact coordinate",
            action_key="6" * 64,
        ),
        permission_summary="Commit exact coordinate",
        envelope=envelope,
        envelope_sha256=digest,
        prepared=frozen,
        transaction=BrowserTransactionEvidence(
            envelope_kind="browser",
            envelope_sha256=digest,
            top_level_origin="https://example.com",
            target_frame_origin="https://example.com",
        ),
    )
    context = BackgroundBrowserApprovalContext(
        request_id="execution_" + "7" * 32,
        task_id="task_" + "8" * 32,
        contract_digest="9" * 64,
        run_id="jobrun_review",
        attempt_id="browser_attempt_" + "a" * 32,
        claim_token="claim-token",
        claim_fence=1,
        principal_id=PRINCIPAL,
        conversation_id=CONVERSATION,
        source_message_id="message_proposal",
        approval_ttl_seconds=120,
        profile_scope=SCOPE,
        owner_token="browser-owner-token",
        provider="openrouter",
        resource=None,
        resource_kind="ephemeral",
        resource_configuration_digest=None,
        budget_ceiling=BUDGET,
    )

    binding = _coordinate_binding(prepared)
    live = _live_binding(prepared, "b" * 64, context)

    assert binding.reason == "position_sensitive_surface"
    assert binding.x == 12.375
    assert binding.y == 8.625
    assert binding.scroll_x == 1.25
    assert binding.scroll_y == 22.75
    assert binding.coordinate_scale == 2
    assert live.resource is None
    assert live.resource_kind == "ephemeral"
    assert live.resource_digest is not None
    assert target.session_id not in live.resource_digest
    assert live.provider == "openrouter"
    assert live.session_digest is not None
    assert target.session_id not in live.session_digest
    assert live.page_digest is not None
    assert target.page_id not in live.page_digest
    assert live.page_generation == 4
    assert live.budget_ceiling == BUDGET


def test_coordinate_approval_body_renders_exact_trusted_review() -> None:
    now = datetime(2026, 8, 31, 12, tzinfo=UTC)
    request = ExecutionRequest(
        id=f"execution_{uuid4().hex}",
        kind="ad_hoc",
        status="running",
        goal="Complete one exact browser transaction.",
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
        claimed_by="worker",
        claim_token="claim-token",
        claim_fence=1,
        claim_expires_at=now + timedelta(minutes=1),
        run_id=f"jobrun_{uuid4().hex}",
    )
    base = _draft(request, now)
    binding = BrowserLiveBinding.model_validate(
        {
            **base.binding.model_dump(mode="python"),
            "resource": {"profile": "personal", "name": "checkout"},
            "resource_kind": "persistent",
            "resource_configuration_digest": "7" * 64,
        }
    )
    coordinate = CoordinateFallbackBinding(
        reason="position_sensitive_surface",
        semantic_resolution_digest="8" * 64,
        masked_image_digest="9" * 64,
        visual_snapshot_digest="a" * 64,
        x=12.375,
        y=8.625,
        viewport_width=800,
        viewport_height=600,
        scroll_x=1.25,
        scroll_y=22.75,
        coordinate_scale=0.5,
        nested_hit_target_digest="b" * 64,
    )
    draft = BrowserTransactionApprovalDraft.model_validate(
        {
            **base.model_dump(mode="python"),
            "binding": binding,
            "target_mode": "coordinate",
            "coordinate": coordinate,
        }
    )
    code = "approval-code-with-20-chars"
    challenge = BrowserTransactionChallenge(
        approval=issue_parked_approval(draft, code=code),
        code=code,
    )

    body = _browser_approval_body(challenge)

    assert "Browser resource: `personal/checkout` (persistent" in body
    assert f"Browser resource configuration sha256: `{'7' * 64}`" in body
    assert "Pinned model provider: `openrouter`" in body
    assert f"Session occurrence sha256: `{'5' * 64}`" in body
    assert f"Page occurrence sha256: `{'6' * 64}`" in body
    assert "Page generation: 4" in body
    assert '"transaction_commits": 1' in body
    assert "Masked screenshot disclosed to: `openrouter`" in body
    assert "Coordinate fallback reason: `position_sensitive_surface`" in body
    assert "Exact CSS coordinate: (12.375, 8.625)" in body
    assert "Scroll position (CSS pixels): (1.25, 22.75)" in body
    assert f"Semantic resolution sha256: `{'8' * 64}`" in body
    assert f"Nested hit-target sha256: `{'b' * 64}`" in body


async def test_exact_approval_is_source_bound_consumed_once_and_not_replayed(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 31, 12, tzinfo=UTC)
    store = ExecutionStore(_settings(tmp_path))
    await store.initialize()
    running, token = await _running(store, now)
    draft = _draft(running, now)
    challenge = await store.park_browser_approval(
        draft,
        scope=SCOPE,
        token=token,
        fence=running.claim_fence,
        code="approval-code-with-20-chars",
    )

    assert (await store.get(running.id, scope=SCOPE)).status == ("awaiting_transaction_approval")
    assert challenge.approval.challenge_digest != challenge.code
    for changes in (
        {"principal_id": "telegram:owner:wrong"},
        {"conversation_id": "conversation_wrong"},
        {"code": "wrong-code-that-is-long-enough"},
    ):
        arguments = {
            "scope": SCOPE,
            "approve": True,
            "principal_id": PRINCIPAL,
            "conversation_id": CONVERSATION,
            "source_message_id": f"decision_{uuid4().hex}",
            "code": challenge.code,
            "now": now + timedelta(seconds=1),
            **changes,
        }
        with pytest.raises(BrowserApprovalError):
            await store.decide_browser_approval(challenge.approval.id, **arguments)

    approved = await store.decide_browser_approval(
        challenge.approval.id,
        scope=SCOPE,
        approve=True,
        principal_id=PRINCIPAL,
        conversation_id=CONVERSATION,
        source_message_id="message_approve",
        code=challenge.code,
        now=now + timedelta(seconds=5),
    )
    assert approved.state == "approved"
    consumed = await store.resume_browser_approval(
        approved.id,
        scope=SCOPE,
        token=token,
        fence=running.claim_fence,
        consume=True,
        now=now + timedelta(seconds=6),
    )
    assert consumed.state == "consumed"
    assert (await store.get(running.id, scope=SCOPE)).status == "running"

    with pytest.raises(BrowserApprovalError):
        await store.resume_browser_approval(
            approved.id,
            scope=SCOPE,
            token=token,
            fence=running.claim_fence,
            consume=True,
        )
    with pytest.raises(BrowserApprovalError):
        await store.park_browser_approval(
            _draft(running, now + timedelta(seconds=7)),
            scope=SCOPE,
            token=token,
            fence=running.claim_fence,
        )

    await store.finish(
        running.id,
        scope=SCOPE,
        token=token,
        fence=running.claim_fence,
        status="uncertain",
        error="commit response was ambiguous",
    )
    with pytest.raises(BrowserApprovalError):
        await store.attest_browser_transaction(
            consumed.id,
            scope=SCOPE,
            disposition="confirmed_not_completed",
            actor_principal_id="telegram:owner:wrong",
            source_conversation_id=CONVERSATION,
            source_message_id="message_reconcile_wrong",
            note="Verified no registration exists.",
        )
    resolved, attestation = await store.attest_browser_transaction(
        consumed.id,
        scope=SCOPE,
        disposition="confirmed_completed",
        actor_principal_id=PRINCIPAL,
        source_conversation_id=CONVERSATION,
        source_message_id="message_reconcile",
        note="Verified the registration confirmation in the account.",
    )
    assert resolved.status == "succeeded"
    assert attestation.actor_principal_id == PRINCIPAL
    assert await store.browser_transaction_attestations(consumed.id, scope=SCOPE) == [attestation]


async def test_denial_expiry_cancellation_and_scope_isolation_resume_without_commit(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 31, 12, tzinfo=UTC)
    store = ExecutionStore(_settings(tmp_path))
    await store.initialize()

    denied_request, denied_token = await _running(store, now)
    denied_challenge = await store.park_browser_approval(
        _draft(denied_request, now),
        scope=SCOPE,
        token=denied_token,
        fence=denied_request.claim_fence,
    )
    denied = await store.decide_browser_approval(
        denied_challenge.approval.id,
        scope=SCOPE,
        approve=False,
        principal_id=PRINCIPAL,
        conversation_id=CONVERSATION,
        source_message_id="message_deny",
        code=denied_challenge.code,
        now=now + timedelta(seconds=1),
    )
    resumed = await store.resume_browser_approval(
        denied.id,
        scope=SCOPE,
        token=denied_token,
        fence=denied_request.claim_fence,
        consume=False,
    )
    assert resumed.state == "denied"
    assert (await store.get(denied_request.id, scope=SCOPE)).status == "running"

    await store.finish(
        denied_request.id,
        scope=SCOPE,
        token=denied_token,
        fence=denied_request.claim_fence,
        status="failed",
    )
    expiring_request, expiring_token = await _running(store, now + timedelta(seconds=1))
    expiring = await store.park_browser_approval(
        _draft(
            expiring_request,
            now + timedelta(seconds=1),
            logical_effect_key="e" * 64,
            expires_at=now + timedelta(seconds=31),
        ),
        scope=SCOPE,
        token=expiring_token,
        fence=expiring_request.claim_fence,
    )
    assert (
        await store.expire_browser_approvals(
            scope=ProfileScope.create("work"),
            now=now + timedelta(seconds=32),
        )
        == []
    )
    assert (await store.get_browser_approval(expiring.approval.id, scope=SCOPE)).state == (
        "pending"
    )
    [expired] = await store.expire_browser_approvals(
        scope=SCOPE,
        now=now + timedelta(seconds=32),
    )
    assert expired.state == "expired"
    await store.resume_browser_approval(
        expired.id,
        scope=SCOPE,
        token=expiring_token,
        fence=expiring_request.claim_fence,
        consume=False,
    )

    await store.finish(
        expiring_request.id,
        scope=SCOPE,
        token=expiring_token,
        fence=expiring_request.claim_fence,
        status="failed",
    )
    cancelling_request, cancelling_token = await _running(store, now + timedelta(seconds=2))
    cancelling = await store.park_browser_approval(
        _draft(
            cancelling_request,
            now + timedelta(seconds=2),
            logical_effect_key="f" * 64,
        ),
        scope=SCOPE,
        token=cancelling_token,
        fence=cancelling_request.claim_fence,
    )
    cancelled = await store.cancel(cancelling_request.id, scope=SCOPE)
    assert cancelled.status == "cancel_requested"
    assert (await store.get_browser_approval(cancelling.approval.id, scope=SCOPE)).state == (
        "invalidated"
    )


async def test_approved_occurrence_expiring_before_consume_resumes_without_dispatch(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 31, 12, tzinfo=UTC)
    store = ExecutionStore(_settings(tmp_path))
    await store.initialize()
    running, token = await _running(store, now)
    challenge = await store.park_browser_approval(
        _draft(running, now, expires_at=now + timedelta(seconds=30)),
        scope=SCOPE,
        token=token,
        fence=running.claim_fence,
    )
    await store.decide_browser_approval(
        challenge.approval.id,
        scope=SCOPE,
        approve=True,
        principal_id=PRINCIPAL,
        conversation_id=CONVERSATION,
        source_message_id="message_approve",
        code=challenge.code,
        now=now + timedelta(seconds=1),
    )

    [expired] = await store.expire_browser_approvals(
        scope=SCOPE,
        now=now + timedelta(seconds=31),
    )
    assert expired.state == "expired"
    assert expired.decision_principal_id == PRINCIPAL
    resumed = await store.resume_browser_approval(
        expired.id,
        scope=SCOPE,
        token=token,
        fence=running.claim_fence,
        consume=True,
        now=now + timedelta(seconds=32),
    )
    assert resumed.state == "expired"
    assert (await store.get(running.id, scope=SCOPE)).status == "running"


async def test_protected_destination_uses_same_exact_one_occurrence_ceremony(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 31, 12, tzinfo=UTC)
    store = ExecutionStore(_settings(tmp_path))
    await store.initialize()
    running, token = await _running(store, now)
    assert running.run_id is not None
    challenge = await store.park_browser_approval(
        ProtectedDestinationApprovalDraft(
            id=f"browser_destination_{uuid4().hex}",
            request_id=running.id,
            run_id=running.run_id,
            attempt_id=f"browser_attempt_{uuid4().hex}",
            claim_fence=running.claim_fence,
            prepared_effect_digest="b" * 64,
            logical_effect_key="9" * 64,
            review_digest="c" * 64,
            binding=BrowserLiveBinding(
                occurrence_digest="1" * 64,
                page_generation=4,
                snapshot_digest="2" * 64,
                target_digest="3" * 64,
                target_description="Card security code field",
                top_level_origin="https://example.com",
                target_frame_origin="https://payments.example.com",
            ),
            principal_id=PRINCIPAL,
            conversation_id=CONVERSATION,
            proposal_source_message_id="message_proposal",
            created_at=now,
            expires_at=now + timedelta(minutes=2),
            protected_use=BrowserProtectedUseEvidence(
                resource=ProfileResourceRef(profile="personal", name="primary_card"),
                revision=2,
                field="security_code",
            ),
        ),
        scope=SCOPE,
        token=token,
        fence=running.claim_fence,
    )
    assert (await store.get(running.id, scope=SCOPE)).status == ("awaiting_protected_approval")
    approved = await store.decide_browser_approval(
        challenge.approval.id,
        scope=SCOPE,
        approve=True,
        principal_id=PRINCIPAL,
        conversation_id=CONVERSATION,
        source_message_id="message_approve",
        code=challenge.code,
        now=now + timedelta(seconds=1),
    )
    consumed = await store.resume_browser_approval(
        approved.id,
        scope=SCOPE,
        token=token,
        fence=running.claim_fence,
        consume=True,
        now=now + timedelta(seconds=2),
    )
    assert consumed.kind == "protected_destination"
    assert consumed.state == "consumed"
