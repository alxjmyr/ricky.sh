"""Stable browser transaction identity and protected-commit policy tests."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ricky.browser.types import (
    BrowserFinancialTransactionEnvelope,
    BrowserMoney,
    BrowserProtectedValueFundingSource,
    BrowserSiteFundingSource,
)
from ricky.executions.browser import (
    BrowserExecutionBudget,
    BrowserLiveBinding,
    BrowserProtectedUseEvidence,
    ParkedBrowserTransaction,
    envelope_digest,
)
from ricky.executions.browser_runtime import (
    _logical_transaction_identity,
    _protected_commit_requests,
)
from ricky.profiles import ProfileResourceRef


def _financial_envelope(
    *,
    source: BrowserProtectedValueFundingSource | BrowserSiteFundingSource,
) -> BrowserFinancialTransactionEnvelope:
    return BrowserFinancialTransactionEnvelope(
        kind="financial",
        intent="Purchase one reviewed item",
        payee="Example Merchant",
        total=BrowserMoney(amount="13.75", currency="USD"),
        fees=(),
        timing="one_time",
        source=source,
        consequences=("Charges the approved funding source",),
        expected_result="The merchant displays an order confirmation",
    )


def _approval(
    envelope: BrowserFinancialTransactionEnvelope,
    protected_uses: tuple[BrowserProtectedUseEvidence, ...],
) -> ParkedBrowserTransaction:
    now = datetime.now(UTC)
    return ParkedBrowserTransaction(
        id=f"browser_transaction_{uuid4().hex}",
        request_id=f"execution_{uuid4().hex}",
        run_id=f"jobrun_{uuid4().hex}",
        attempt_id=f"browser_attempt_{uuid4().hex}",
        claim_fence=1,
        prepared_effect_digest="a" * 64,
        logical_effect_key="b" * 64,
        review_digest="c" * 64,
        binding=BrowserLiveBinding(
            occurrence_digest="d" * 64,
            resource_digest="2" * 64,
            resource_kind="ephemeral",
            provider="openrouter",
            session_digest="3" * 64,
            page_digest="4" * 64,
            budget_ceiling=BrowserExecutionBudget(
                session_starts=1,
                navigations=1,
                scrolls=1,
                created_pages=0,
                controlled_pages=1,
                semantic_observations=1,
                visual_observations=0,
                interactions=1,
                protected_materializations=3,
                uploads=0,
                upload_bytes=0,
                downloads=0,
                download_bytes=0,
                transaction_commits=1,
                parked_browsers=1,
                approval_ttl_seconds=120,
            ),
            page_generation=2,
            snapshot_digest="e" * 64,
            target_digest="f" * 64,
            target_description="Place order",
            top_level_origin="https://example.com",
            target_frame_origin="https://example.com",
        ),
        principal_id="telegram:owner:42",
        conversation_id="conversation_browser_owner",
        proposal_source_message_id="message_proposal",
        challenge_digest="1" * 64,
        state="pending",
        revision=1,
        created_at=now,
        expires_at=now + timedelta(minutes=2),
        logical_transaction_id=f"browser_logical_{uuid4().hex}",
        target_mode="semantic",
        envelope=envelope,
        envelope_digest=envelope_digest(envelope),
        protected_uses=protected_uses,
    )


def test_logical_transaction_identity_ignores_transient_browser_ownership() -> None:
    inputs = {
        "task_id": f"task_{uuid4().hex}",
        "contract_digest": "a" * 64,
        "operation": "browser.commit",
        "envelope_digest": "b" * 64,
    }

    first = _logical_transaction_identity(sequence=1, **inputs)
    after_restart = _logical_transaction_identity(sequence=1, **inputs)
    repeated_live_commit = _logical_transaction_identity(sequence=2, **inputs)

    assert after_restart == first
    assert repeated_live_commit != first
    assert (
        _logical_transaction_identity(
            sequence=1,
            **{**inputs, "envelope_digest": "c" * 64},
        )[1]
        != first[1]
    )


def test_financial_commit_authorizes_every_protected_use_and_exact_total() -> None:
    card = ProfileResourceRef(profile="personal", name="card")
    address = ProfileResourceRef(profile="personal", name="billing-address")
    envelope = _financial_envelope(
        source=BrowserProtectedValueFundingSource(
            kind="protected_value",
            protected_value=card,
        )
    )
    approval = _approval(
        envelope,
        (
            BrowserProtectedUseEvidence(resource=card, revision=3, field="number"),
            BrowserProtectedUseEvidence(resource=card, revision=3, field="security_code"),
            BrowserProtectedUseEvidence(resource=address, revision=7, field="postal_code"),
        ),
    )

    requests = _protected_commit_requests(
        execution_id=approval.request_id,
        approval=approval,
        envelope=envelope,
    )

    assert {request.ref.qualified for request in requests} == {
        card.qualified,
        address.qualified,
    }
    assert all(request.amount_minor == 1375 for request in requests)
    assert all(request.currency == "USD" for request in requests)
    assert next(request for request in requests if request.ref == card).fields == (
        "number",
        "security_code",
    )


def test_financial_commit_requires_protected_funding_source_fill_evidence() -> None:
    card = ProfileResourceRef(profile="personal", name="card")
    address = ProfileResourceRef(profile="personal", name="billing-address")
    envelope = _financial_envelope(
        source=BrowserProtectedValueFundingSource(
            kind="protected_value",
            protected_value=card,
        )
    )
    approval = _approval(
        envelope,
        (BrowserProtectedUseEvidence(resource=address, revision=1, field="postal_code"),),
    )

    with pytest.raises(ValueError, match="funding source lacks live fill evidence"):
        _protected_commit_requests(
            execution_id=approval.request_id,
            approval=approval,
            envelope=envelope,
        )


def test_site_funding_still_authorizes_other_protected_financial_uses() -> None:
    address = ProfileResourceRef(profile="personal", name="billing-address")
    envelope = _financial_envelope(
        source=BrowserSiteFundingSource(kind="site", label="Saved card ending in 42")
    )
    approval = _approval(
        envelope,
        (BrowserProtectedUseEvidence(resource=address, revision=2, field="postal_code"),),
    )

    [request] = _protected_commit_requests(
        execution_id=approval.request_id,
        approval=approval,
        envelope=envelope,
    )

    assert request.ref == address
    assert request.amount_minor == 1375
    assert request.currency == "USD"
