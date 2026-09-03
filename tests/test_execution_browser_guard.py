"""Exact upload and protected-revision checks in the execution guard adapter."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from ricky.browser.runtime_guard import BrowserGuardFacts, BrowserRuntimeEvidence
from ricky.config import RickySettings
from ricky.executions.browser import (
    BrowserAttachmentPin,
    BrowserExecutionBudget,
    BrowserExecutionScope,
    BrowserProtectedResourcePin,
)
from ricky.executions.browser_guard import (
    BrowserExecutionGuardError,
    DurableBrowserExecutionGuard,
)
from ricky.executions.store import ExecutionStore
from ricky.jobs.browser_store import BrowserRunLedger
from ricky.jobs.store import JobRunStore
from ricky.jobs.types import JobRun
from ricky.profiles import ProfileResourceRef, ProfileScope
from ricky.tools import EffectIdentity

SCOPE = ProfileScope.create("personal")
SESSION_ID = "browser_session_" + "1" * 32
SESSION_RESOURCE = ProfileResourceRef(profile="personal", name=SESSION_ID)
PROTECTED_RESOURCE = ProfileResourceRef(profile="personal", name="primary_card")
TASK_ID = "task_" + "9" * 32
ATTACHMENT_ONE = f"task/personal/{TASK_ID}/one.txt"
ATTACHMENT_TWO = f"task/personal/{TASK_ID}/two.txt"


class _Executions:
    async def get(self, request_id: str, *, scope: ProfileScope):
        del request_id, scope
        return SimpleNamespace(
            run_id="jobrun_browser",
            claim_token="claim_browser",
            claim_fence=1,
            status="running",
        )


def _scope() -> BrowserExecutionScope:
    return BrowserExecutionScope(
        mode="transaction",
        allow_ephemeral=True,
        allow_public_https_research=True,
        allowed_tools=("browser_fill_protected", "browser_upload"),
        allowed_operations=(
            "session_starts",
            "controlled_pages",
            "interactions",
            "protected_materializations",
            "uploads",
            "upload_bytes",
            "parked_browsers",
        ),
        attachments=(
            BrowserAttachmentPin(
                id=ATTACHMENT_ONE,
                profile="personal",
                task_id=TASK_ID,
                artifact_path="one.txt",
                sha256="a" * 64,
                byte_count=11,
            ),
            BrowserAttachmentPin(
                id=ATTACHMENT_TWO,
                profile="personal",
                task_id=TASK_ID,
                artifact_path="two.txt",
                sha256="b" * 64,
                byte_count=13,
            ),
        ),
        protected_resources=(
            BrowserProtectedResourcePin(
                resource=PROTECTED_RESOURCE,
                revision=3,
                fields=("security_code",),
                materialization_limit=1,
                commit_limit=1,
            ),
        ),
        budget=BrowserExecutionBudget(
            session_starts=1,
            navigations=1,
            scrolls=1,
            created_pages=1,
            controlled_pages=1,
            semantic_observations=1,
            visual_observations=0,
            interactions=2,
            protected_materializations=1,
            uploads=1,
            upload_bytes=24,
            downloads=0,
            download_bytes=0,
            transaction_commits=1,
            parked_browsers=1,
            approval_ttl_seconds=120,
        ),
    )


def _guard() -> DurableBrowserExecutionGuard:
    return DurableBrowserExecutionGuard(
        browser_scope=_scope(),
        profile_scope=SCOPE,
        provider="openrouter",
        request_id="execution_" + "1" * 32,
        attempt_id="browser_attempt_" + "1" * 32,
        run_id="jobrun_browser",
        owner_token="browser_owner_" + "1" * 32,
        claim_token="claim_browser",
        claim_fence=1,
        resource=None,
        resource_configuration_digest=None,
        executions=cast(ExecutionStore, _Executions()),
        ledger=cast(BrowserRunLedger, None),
    )


def _upload(**updates: object) -> BrowserGuardFacts:
    values: dict[str, object] = {
        "tool_name": "browser_upload",
        "resource": SESSION_RESOURCE,
        "session_id": SESSION_ID,
        "session_mode": "owned_ephemeral",
        "headless": True,
        "controlled_page_count": 1,
        "top_level_origin": "https://example.com",
        "target_frame_origin": "https://example.com",
        "attachment_count": 2,
        "attachment_ids": (ATTACHMENT_ONE, ATTACHMENT_TWO),
        "attachment_sha256": ("a" * 64, "b" * 64),
        "byte_count": 24,
    }
    values.update(updates)
    return BrowserGuardFacts.model_validate(values)


async def test_upload_requires_exact_pinned_ids_digests_and_aggregate_bytes(
    tmp_path: Path,
) -> None:
    del tmp_path
    guard = _guard()
    await guard.check(_upload())

    invalid = (
        _upload(attachment_ids=()),
        _upload(attachment_ids=(ATTACHMENT_ONE, "missing")),
        _upload(attachment_sha256=("a" * 64, "c" * 64)),
        _upload(byte_count=23),
        _upload(attachment_ids=(ATTACHMENT_ONE, ATTACHMENT_ONE)),
    )
    for facts in invalid:
        with pytest.raises(BrowserExecutionGuardError):
            await guard.check(facts)


async def test_protected_fill_checks_actual_revision_when_materialized(
    tmp_path: Path,
) -> None:
    del tmp_path
    guard = _guard()
    facts = BrowserGuardFacts(
        tool_name="browser_fill_protected",
        resource=SESSION_RESOURCE,
        session_id=SESSION_ID,
        session_mode="owned_ephemeral",
        headless=True,
        controlled_page_count=1,
        top_level_origin="https://example.com",
        target_frame_origin="https://example.com",
        protected_resource=PROTECTED_RESOURCE,
        protected_revision=3,
        protected_field="security_code",
    )
    await guard.check(facts)
    with pytest.raises(BrowserExecutionGuardError, match="revision"):
        await guard.check(facts.model_copy(update={"protected_revision": 4}))


async def test_public_origin_cannot_hide_inside_private_origin_ceiling(
    tmp_path: Path,
) -> None:
    del tmp_path
    guard = _guard()
    guard.browser_scope = guard.browser_scope.model_copy(
        update={
            "allow_public_https_research": False,
            "private_origin_ceiling": ("https://example.com",),
        }
    )

    with pytest.raises(BrowserExecutionGuardError, match="public HTTPS"):
        await guard.check(_upload())


async def test_resolved_private_origin_requires_exact_private_ceiling(
    tmp_path: Path,
) -> None:
    del tmp_path
    guard = _guard()
    private_origin = "https://internal.example.com"
    facts = _upload(
        top_level_origin=private_origin,
        target_frame_origin=private_origin,
        private_destination_origins=(private_origin,),
    )
    with pytest.raises(BrowserExecutionGuardError, match="private browser destination"):
        await guard.check(facts)

    guard.browser_scope = guard.browser_scope.model_copy(
        update={"private_origin_ceiling": (private_origin,)}
    )
    await guard.check(facts)


async def test_download_bytes_use_download_not_upload_ceiling(tmp_path: Path) -> None:
    del tmp_path
    guard = _guard()
    guard.browser_scope = guard.browser_scope.model_copy(
        update={
            "allowed_tools": (*guard.browser_scope.allowed_tools, "browser_download"),
            "allowed_operations": (
                *guard.browser_scope.allowed_operations,
                "downloads",
                "download_bytes",
            ),
            "budget": guard.browser_scope.budget.model_copy(
                update={"upload_bytes": 100, "download_bytes": 10}
            ),
        }
    )
    facts = BrowserGuardFacts(
        tool_name="browser_download",
        resource=SESSION_RESOURCE,
        session_id=SESSION_ID,
        session_mode="owned_ephemeral",
        headless=True,
        controlled_page_count=1,
        top_level_origin="https://example.com",
        target_frame_origin="https://example.com",
        byte_count=11,
    )

    with pytest.raises(BrowserExecutionGuardError, match="download ceiling"):
        await guard.check(facts)
    await guard.check(facts.model_copy(update={"byte_count": 10}))


async def test_effect_evidence_is_published_with_shared_action_identity(
    tmp_path: Path,
) -> None:
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    jobs = JobRunStore(settings)
    await jobs.initialize()
    run = JobRun(
        id="jobrun_browser",
        provider="openrouter",
        model="test",
        profile_scope=SCOPE,
        session_id="session_browser",
        started_at=datetime.now(UTC),
        trigger="execution",
        trigger_id="execution_" + "1" * 32,
    )
    await jobs.insert(run, scope=SCOPE)
    ledger = BrowserRunLedger(settings)
    lease = await ledger.start_attempt(
        run_id=run.id,
        scope=SCOPE,
        browser_scope=_scope(),
        claim_fence=1,
        worker_id="worker",
        execution_request_id="execution_" + "1" * 32,
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
    identity = EffectIdentity(
        operation="browser.upload",
        target="https://example.com",
        occurrence="exact-upload",
        summary="Upload exact task artifacts",
        action_key="c" * 64,
    )
    action = await jobs.reserve_action(
        job_name="ad-hoc-browser",
        run_id=run.id,
        scope=SCOPE,
        identity=identity,
        effect_budget=1,
    )
    guard = DurableBrowserExecutionGuard(
        browser_scope=_scope(),
        profile_scope=SCOPE,
        provider="openrouter",
        request_id="execution_" + "1" * 32,
        attempt_id=lease.attempt.id,
        run_id=run.id,
        owner_token=lease.owner_token,
        claim_token="claim_browser",
        claim_fence=1,
        resource=None,
        resource_configuration_digest=None,
        executions=cast(ExecutionStore, _Executions()),
        ledger=ledger,
    )
    guard.bind_effect_action(action.id, identity.action_key)
    await guard.record(
        BrowserRuntimeEvidence(
            facts=_upload(),
            disposition="performed",
            action_id="browser_action_" + "7" * 32,
        )
    )
    assert await ledger.action_evidence(lease.attempt.id, scope=SCOPE) == []

    await jobs.resolve_action(
        action.id,
        "performed",
        scope=SCOPE,
        provider_reference=None,
    )
    await guard.settle_effect_action(action.id)

    [evidence] = await ledger.action_evidence(lease.attempt.id, scope=SCOPE)
    assert evidence.action_id == action.id
    assert evidence.logical_effect_key == identity.action_key
    assert evidence.disposition == "performed"
    assert "browser_action_id=" in (evidence.postcondition or "")
