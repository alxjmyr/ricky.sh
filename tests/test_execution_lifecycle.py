"""Execution cancellation, renewal, reconciliation, and revocation lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

import ricky.jobs.store as job_store_module
from ricky.capabilities import (
    AuthenticatedSource,
    CapabilityRegistry,
    GuardrailFieldProposal,
    GuardrailProposal,
    GuardrailRegistry,
)
from ricky.capabilities.policy import policy_digest
from ricky.config import ExecutionSettings, MessagingSettings, RickySettings
from ricky.durable_tasks.store import DurableTaskStore
from ricky.executions.compiler import (
    CompileBinding,
    ExecutionContractCompileError,
    ExecutionContractCompiler,
)
from ricky.executions.dispatcher import (
    ExecutionDispatcher,
    GrantRevocationError,
)
from ricky.executions.drafts import (
    AdHocConfirmation,
    AdHocGuardrailContinuation,
    DraftStatus,
    ExecutionDraft,
    summary_digest,
)
from ricky.executions.store import (
    ExecutionFenceError,
    ExecutionStore,
    ExecutionStoreError,
)
from ricky.executions.types import ExecutionRequest
from ricky.gateway.conversations import ConversationCoordinator
from ricky.gateway.service import GatewayService
from ricky.jobs.store import JobRunStore
from ricky.jobs.types import JobRun
from ricky.profiles import ProfileScope
from ricky.project_scope import ProjectScope
from ricky.skills.registry import SkillRegistry
from ricky.tools.base import EffectIdentity

PERSONAL_SCOPE = ProfileScope.create("personal")


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / ".ricky"),
            "providers": {"openrouter": {"default_model": "test-model"}},
            "memory": {"enabled": False},
            "workflow": {"enabled": False},
            "executions": ExecutionSettings(
                claim_seconds=1,
                heartbeat_seconds=0.01,
                concurrency=1,
                poll_seconds=0.01,
            ),
            "messaging": MessagingSettings.model_validate(
                {
                    "telegram_accounts": {
                        "personal/owner": {"bot_token": "test-token"},
                    },
                    "transports": {"main": {"type": "telegram", "account": "personal/owner"}},
                    "routes": {
                        "owner": {
                            "transport": "main",
                            "destination": "chat-owner",
                            "owner_profile": "personal",
                            "accepted_profiles": ["shared", "personal"],
                        }
                    },
                    "agent_routes": ["owner"],
                }
            ),
            "gateway": {
                "routes": {
                    "owner": {
                        "provider": "openrouter",
                        "model": "test-model",
                        "primary_profile": "personal",
                        "project_root": str(tmp_path),
                    }
                }
            },
        }
    )


def _job(tmp_path: Path) -> None:
    bundle = tmp_path / "user" / "profiles" / "personal" / "jobs" / "brief"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "job.toml").write_text(
        """version = 3
name = "brief"
description = "Prepare a brief."
provider = "openrouter"
model = "test-model"
goal = "Prepare the brief."
[budget]
wall_clock_seconds = 10
iterations = 2
max_completion_tokens_per_request = 100
effect_calls = 1
[tools]
allow = []
""",
        encoding="utf-8",
    )


class _ControlledRunner:
    def __init__(self, settings: RickySettings, *, observable: bool = False) -> None:
        self.store = JobRunStore(settings)
        self.observable = observable
        self.started = asyncio.Event()
        self.cancelled = False
        self.joined = asyncio.Event()

    async def run(self, name: str, **kwargs: object) -> JobRun:
        run_id = cast(str, kwargs["run_id"])
        transcript = self.store.root / "transcripts" / f"{run_id}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(
            json.dumps({"kind": "text_delta", "delta": "visible"}) + "\n"
            if self.observable
            else json.dumps({"kind": "llm_request_started"}) + "\n",
            encoding="utf-8",
        )
        live = JobRun(
            id=run_id,
            job_name=name,
            spec_digest="a" * 64,
            provider="openrouter",
            model="test-model",
            profile_scope=cast(ProfileScope, kwargs["profile_scope"]),
            session_id=f"session_{uuid4().hex}",
            started_at=datetime.now(UTC),
            transcript_path=str(transcript),
            trigger="execution",
            trigger_id=cast(str, kwargs["trigger_id"]),
        )
        await self.store.initialize()
        await self.store.insert(live, scope=live.profile_scope)
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
        finally:
            self.joined.set()
        interrupted = live.model_copy(
            update={
                "outcome": "interrupted",
                "finished_at": datetime.now(UTC),
                "error": "controlled run interrupted",
            }
        )
        await self.store.finish(interrupted, scope=live.profile_scope)
        return await self.store.get(run_id, scope=live.profile_scope)


async def _queued(settings: RickySettings, *, key: str) -> ExecutionRequest:
    store = ExecutionStore(settings)
    await store.initialize()
    return await store.submit(
        ExecutionRequest(
            id=f"execution_{uuid4().hex}",
            kind="named_job",
            status="queued",
            named_job="personal/brief",
            job_digest="a" * 64,
            profile_scope=PERSONAL_SCOPE,
            notification_route="owner",
            request_key=key,
            created_at=datetime.now(UTC),
        ),
        scope=PERSONAL_SCOPE,
    )


async def test_cross_process_cancel_request_is_observed_and_cleanly_settled(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _job(tmp_path)
    runner = _ControlledRunner(settings)
    owner = ExecutionDispatcher(
        settings,
        project_root=tmp_path,
        runner_factory=cast(Any, lambda: runner),
    )
    request = await owner.start_named_job(
        "brief",
        notification_route="owner",
        request_key="cross-process",
        profile_scope=PERSONAL_SCOPE,
    )
    worker = asyncio.create_task(owner.worker_once(scope=PERSONAL_SCOPE, worker_id="owner"))
    await asyncio.wait_for(runner.started.wait(), timeout=2)

    controller = ExecutionDispatcher(settings, project_root=tmp_path)
    requested = await controller.cancel_execution_request(request.id, scope=PERSONAL_SCOPE)
    [terminal] = await asyncio.wait_for(worker, timeout=2)

    assert requested.status == "cancel_requested"
    assert terminal.status == "cancelled"
    assert runner.cancelled and runner.joined.is_set()


async def test_expired_cancel_request_recovers_before_any_reclaim(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    now = datetime(2026, 8, 21, 12, tzinfo=UTC)
    request = await _queued(settings, key="startup-cancel")
    store = ExecutionStore(settings)
    claimed = (await store.claim(scope=PERSONAL_SCOPE, worker_id="lost-owner", limit=1, now=now))[0]
    assert claimed.claim_token is not None
    running = await store.start(
        request.id,
        scope=PERSONAL_SCOPE,
        token=claimed.claim_token,
        fence=claimed.claim_fence,
        run_id=f"jobrun_{uuid4().hex}",
    )
    cancelling = await store.cancel(running.id, scope=PERSONAL_SCOPE)

    recovered = await store.recover_expired(scope=PERSONAL_SCOPE, now=now + timedelta(seconds=2))

    assert cancelling.status == "cancel_requested"
    assert recovered[0].status == "uncertain"
    assert (
        await store.claim(
            scope=PERSONAL_SCOPE,
            worker_id="new-owner",
            limit=1,
            now=now + timedelta(seconds=2),
        )
        == []
    )


async def test_same_process_cancel_joins_and_preserves_observable_uncertainty(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _job(tmp_path)
    runner = _ControlledRunner(settings, observable=True)
    dispatcher = ExecutionDispatcher(
        settings,
        project_root=tmp_path,
        runner_factory=cast(Any, lambda: runner),
    )
    request = await dispatcher.start_named_job(
        "brief",
        notification_route="owner",
        request_key="same-process",
        profile_scope=PERSONAL_SCOPE,
    )
    worker = asyncio.create_task(dispatcher.worker_once(scope=PERSONAL_SCOPE, worker_id="owner"))
    await asyncio.wait_for(runner.started.wait(), timeout=2)

    cancelled = await dispatcher.cancel_execution_request(request.id, scope=PERSONAL_SCOPE)
    [terminal] = await asyncio.wait_for(worker, timeout=2)

    assert cancelled.status == terminal.status == "uncertain"
    assert runner.cancelled and runner.joined.is_set()


@pytest.mark.parametrize("failure", [ExecutionFenceError("lost"), ExecutionStoreError("db")])
async def test_every_renewal_failure_cancels_joins_and_records_the_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    settings = _settings(tmp_path)
    _job(tmp_path)
    runner = _ControlledRunner(settings)
    dispatcher = ExecutionDispatcher(
        settings,
        project_root=tmp_path,
        runner_factory=cast(Any, lambda: runner),
    )
    await dispatcher.start_named_job(
        "brief",
        notification_route="owner",
        request_key=type(failure).__name__,
        profile_scope=PERSONAL_SCOPE,
    )

    async def fail_renew(*args: object, **kwargs: object) -> ExecutionRequest:
        del args, kwargs
        await runner.started.wait()
        raise failure

    monkeypatch.setattr(dispatcher.store, "renew", fail_renew)
    [terminal] = await asyncio.wait_for(
        dispatcher.worker_once(scope=PERSONAL_SCOPE, worker_id="owner"), timeout=2
    )

    assert terminal.status == "failed"
    assert terminal.error == f"execution lease renewal failed: {type(failure).__name__}"
    assert runner.cancelled and runner.joined.is_set()


@pytest.mark.parametrize("disposition", ["reserved", "performed"])
async def test_reserved_or_performed_effect_forces_uncertain_cancellation(
    tmp_path: Path,
    disposition: str,
) -> None:
    settings = _settings(tmp_path)
    request = await _queued(settings, key=f"effect-{disposition}")
    store = ExecutionStore(settings)
    claimed = (await store.claim(scope=PERSONAL_SCOPE, worker_id="owner", limit=1))[0]
    assert claimed.claim_token is not None
    running = await store.start(
        request.id,
        scope=PERSONAL_SCOPE,
        token=claimed.claim_token,
        fence=claimed.claim_fence,
        run_id=f"jobrun_{uuid4().hex}",
    )
    jobs = JobRunStore(settings)
    await jobs.initialize()
    await jobs.insert(
        JobRun(
            id=cast(str, running.run_id),
            job_name="brief",
            provider="openrouter",
            model="test-model",
            profile_scope=PERSONAL_SCOPE,
            session_id="session_effect",
            started_at=datetime.now(UTC),
            trigger="execution",
            trigger_id=running.id,
        ),
        scope=PERSONAL_SCOPE,
    )
    action = await jobs.reserve_action(
        job_name="brief",
        run_id=cast(str, running.run_id),
        identity=EffectIdentity(
            operation="send",
            target="recipient",
            occurrence="once",
            summary="Send one message",
            action_key="b" * 64,
        ),
        effect_budget=1,
        scope=PERSONAL_SCOPE,
    )
    if disposition == "performed":
        await jobs.resolve_action(
            action.id,
            "performed",
            scope=PERSONAL_SCOPE,
            provider_reference="receipt",
        )
    cancelling = await store.cancel(running.id, scope=PERSONAL_SCOPE)

    terminal = await ExecutionDispatcher(settings, project_root=tmp_path)._settle_cancellation(
        cancelling
    )

    assert terminal.status == "uncertain"


@pytest.mark.parametrize(
    ("disposition", "resolved_status"),
    [("confirmed_completed", "succeeded"), ("confirmed_not_completed", "failed")],
)
async def test_uncertain_execution_requires_resolution_before_retry(
    tmp_path: Path,
    disposition: str,
    resolved_status: str,
) -> None:
    settings = _settings(tmp_path)
    request = await _queued(settings, key=disposition)
    store = ExecutionStore(settings)
    claimed = (await store.claim(scope=PERSONAL_SCOPE, worker_id="owner", limit=1))[0]
    assert claimed.claim_token is not None
    running = await store.start(
        request.id,
        scope=PERSONAL_SCOPE,
        token=claimed.claim_token,
        fence=claimed.claim_fence,
        run_id=f"jobrun_{uuid4().hex}",
    )
    uncertain = await store.finish(
        running.id,
        scope=PERSONAL_SCOPE,
        token=claimed.claim_token,
        fence=claimed.claim_fence,
        status="uncertain",
    )
    with pytest.raises(ExecutionStoreError, match="must be resolved"):
        await store.retry(uncertain.id, scope=PERSONAL_SCOPE)

    resolved = await store.resolve(
        uncertain.id,
        scope=PERSONAL_SCOPE,
        disposition=cast(Any, disposition),
        actor="owner",
        note="Operator reconciled the durable evidence.",
    )
    child = await store.retry(resolved.id, scope=PERSONAL_SCOPE)

    assert resolved.status == resolved_status
    assert child.parent_request_id == resolved.id


async def test_compiler_retry_uses_the_same_reconciliation_boundary(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    conversation_id = f"conversation_{uuid4().hex}"
    compiler = _compiler(settings, conversation_id)
    tasks = await DurableTaskStore.create(settings, profile="personal")
    task = await tasks.create_task(
        title="Reconcile",
        objective="Reconcile the prior attempt",
        closure_criteria="The prior result is known",
        execution_mode="agent",
        authority="deterministic_user_command",
        executor_id="test",
    )
    request = ExecutionRequest(
        id=f"execution_{uuid4().hex}",
        kind="ad_hoc",
        status="queued",
        goal="Complete the task.",
        contract_id=f"contract_{uuid4().hex}",
        contract_digest="a" * 64,
        task_id=task.id,
        task_revision=task.revision,
        profile_scope=PERSONAL_SCOPE,
        notification_route="owner",
        request_key="compiler-retry",
        created_at=datetime.now(UTC),
    )
    await compiler.store.initialize()
    await compiler.store.submit(request, scope=PERSONAL_SCOPE)
    claimed = (await compiler.store.claim(scope=PERSONAL_SCOPE, worker_id="owner", limit=1))[0]
    assert claimed.claim_token is not None
    running = await compiler.store.start(
        claimed.id,
        scope=PERSONAL_SCOPE,
        token=claimed.claim_token,
        fence=claimed.claim_fence,
        run_id=f"jobrun_{uuid4().hex}",
    )
    uncertain = await compiler.store.finish(
        running.id,
        scope=PERSONAL_SCOPE,
        token=claimed.claim_token,
        fence=claimed.claim_fence,
        status="uncertain",
    )
    with pytest.raises(ExecutionContractCompileError, match="must be resolved"):
        await compiler._validate_retry(uncertain.id, task)
    await compiler.store.resolve(
        uncertain.id,
        scope=PERSONAL_SCOPE,
        disposition="confirmed_not_completed",
        actor="owner",
        note="No external work occurred.",
    )

    await compiler._validate_retry(uncertain.id, task)


def _source(conversation_id: str, text: str) -> AuthenticatedSource:
    return AuthenticatedSource(
        principal_id="telegram:owner:1",
        conversation_id=conversation_id,
        message_id=f"inbound_{uuid4().hex}",
        text_digest=hashlib.sha256(text.encode()).hexdigest(),
        text_snapshot=text,
        received_at=datetime.now(UTC),
    )


def _compiler(settings: RickySettings, conversation_id: str) -> ExecutionContractCompiler:
    route = settings.gateway.routes["owner"]
    return ExecutionContractCompiler(
        settings,
        capabilities=CapabilityRegistry(),
        guardrails=GuardrailRegistry(),
        skills=SkillRegistry(),
        route=route,
        binding=CompileBinding(
            principal_id="telegram:owner:1",
            conversation_id=conversation_id,
            route_name="owner",
            notification_route=f"conversation:{conversation_id}",
            profile_scope=PERSONAL_SCOPE,
            provider=route.provider,
            model=route.model,
            project_root_ref=route.project_root,
        ),
    )


def _draft(
    compiler: ExecutionContractCompiler,
    source: AuthenticatedSource,
    status: DraftStatus,
) -> ExecutionDraft:
    now = datetime.now(UTC)
    confirmation_summary = "Approve this exact background execution."
    contract_states = {"queued", "executing", "completed", "uncertain"}
    return ExecutionDraft(
        id=f"draft_{uuid4().hex}",
        target="ad_hoc_background",
        status=status,
        revision=1,
        principal_id=source.principal_id,
        conversation_id=source.conversation_id,
        task_id=f"task_{uuid4().hex}",
        task_revision=1,
        profile_scope=PERSONAL_SCOPE,
        goal="Complete bounded work.",
        requested_capabilities=("builtin.fake",),
        sources=(source,),
        pending_questions=("What scope?",) if status == "collecting_guardrails" else (),
        confirmation_required=status == "awaiting_confirmation",
        confirmation_summary=(confirmation_summary if status == "awaiting_confirmation" else None),
        confirmation_summary_digest=(
            summary_digest(confirmation_summary) if status == "awaiting_confirmation" else None
        ),
        agent_policy_digest=compiler.settings.agents.ad_hoc_background.digest(),
        route_policy_digest=policy_digest(
            compiler.settings.agents.ad_hoc_background, compiler.route
        ),
        inventory_digest=compiler.capabilities.digest(),
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=1),
        contract_id=("contract_" + "a" * 32 if status in contract_states else None),
        request_id=("execution_" + "a" * 32 if status in contract_states else None),
    )


@pytest.mark.parametrize(
    "status",
    ["ready", "queued", "executing", "completed", "uncertain", "expired", "cancelled", "rejected"],
)
async def test_background_draft_terminal_states_cannot_be_continued(
    tmp_path: Path,
    status: DraftStatus,
) -> None:
    settings = _settings(tmp_path)
    conversation_id = f"conversation_{uuid4().hex}"
    compiler = _compiler(settings, conversation_id)
    initial_source = _source(conversation_id, "Start bounded work.")
    draft = _draft(compiler, initial_source, status)
    await compiler.store.initialize()
    await compiler.store.create_draft(draft, scope=PERSONAL_SCOPE)
    before = (await compiler.store.get_draft(draft.id, scope=PERSONAL_SCOPE)).model_dump_json()
    continuation = AdHocGuardrailContinuation(
        action="supply_guardrails",
        draft_id=draft.id,
        expected_draft_revision=draft.revision,
        guardrails=(
            GuardrailProposal(
                capability_id="builtin.fake",
                fields=(GuardrailFieldProposal(field="scope", value="bounded"),),
            ),
        ),
    )

    with pytest.raises(ExecutionContractCompileError, match=f"from {status}"):
        await compiler.review(
            continuation,
            source=_source(conversation_id, "Use only the bounded scope."),
        )

    assert (
        await compiler.store.get_draft(draft.id, scope=PERSONAL_SCOPE)
    ).model_dump_json() == before


async def test_confirmation_requires_the_exact_awaiting_confirmation_state(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    conversation_id = f"conversation_{uuid4().hex}"
    compiler = _compiler(settings, conversation_id)
    draft = _draft(compiler, _source(conversation_id, "Start."), "collecting_guardrails")
    await compiler.store.initialize()
    await compiler.store.create_draft(draft, scope=PERSONAL_SCOPE)

    with pytest.raises(ExecutionContractCompileError, match="awaiting confirmation"):
        await compiler.review(
            AdHocConfirmation(
                action="confirm",
                draft_id=draft.id,
                expected_draft_revision=draft.revision,
            ),
            source=_source(conversation_id, "Confirmed."),
        )


class _RevocationStore:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.attempted = False

    async def initialize(self) -> None:
        return None

    async def revoke(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.attempted = True
        if self.failure is not None:
            raise self.failure

    async def set_grant_budget_status(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.attempted = True
        if self.failure is not None:
            raise self.failure


class _BlockingRevocationStore(_RevocationStore):
    def __init__(self, release: asyncio.Event) -> None:
        super().__init__()
        self.release = release
        self.started = asyncio.Event()
        self.joined = asyncio.Event()

    async def _block(self) -> None:
        self.attempted = True
        self.started.set()
        try:
            await self.release.wait()
        finally:
            self.joined.set()

    async def revoke(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        await self._block()

    async def set_grant_budget_status(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        await self._block()


@pytest.mark.parametrize(
    ("authority_fails", "budget_fails", "failed"),
    [
        (True, False, ("authority_store",)),
        (False, True, ("job_budget",)),
        (True, True, ("authority_store", "job_budget")),
    ],
)
async def test_revocation_surfaces_every_failed_subsystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority_fails: bool,
    budget_fails: bool,
    failed: tuple[str, ...],
) -> None:
    authority = _RevocationStore(RuntimeError("authority") if authority_fails else None)
    budget = _RevocationStore(RuntimeError("budget") if budget_fails else None)
    monkeypatch.setattr(job_store_module, "JobRunStore", lambda settings: budget)
    dispatcher = ExecutionDispatcher(
        _settings(tmp_path), project_root=tmp_path, authority=cast(Any, authority)
    )

    with pytest.raises(GrantRevocationError) as captured:
        await dispatcher.revoke_grant(
            "grant_" + "a" * 32,
            scope=PERSONAL_SCOPE,
            actor="owner",
            reason="stop",
        )

    assert captured.value.failed_subsystems == failed
    assert authority.attempted and budget.attempted


async def test_revocation_cancellation_still_joins_both_mirrors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()
    authority = _BlockingRevocationStore(release)
    budget = _BlockingRevocationStore(release)
    monkeypatch.setattr(job_store_module, "JobRunStore", lambda settings: budget)
    dispatcher = ExecutionDispatcher(
        _settings(tmp_path), project_root=tmp_path, authority=cast(Any, authority)
    )
    revocation = asyncio.create_task(
        dispatcher.revoke_grant(
            "grant_" + "a" * 32,
            scope=PERSONAL_SCOPE,
            actor="owner",
            reason="stop",
        )
    )
    await asyncio.gather(authority.started.wait(), budget.started.wait())

    revocation.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await revocation

    assert authority.joined.is_set() and budget.joined.is_set()


async def test_gateway_service_binds_its_dispatcher_into_same_process_controls(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    first = ExecutionDispatcher(settings, project_root=tmp_path)
    owned = ExecutionDispatcher(settings, project_root=tmp_path)
    coordinator = ConversationCoordinator(settings, dispatcher=first)

    GatewayService(
        settings,
        messaging=cast(Any, object()),
        conversations=coordinator,
        dispatcher=owned,
    )

    assert coordinator.dispatcher is owned
    assert coordinator.execution_store is owned.store


def test_projectless_dispatcher_never_falls_back_to_the_current_checkout(
    tmp_path: Path,
) -> None:
    """A dispatcher discovers no job from any project directory."""

    settings = _settings(tmp_path)
    stray = tmp_path / ".ricky" / "jobs" / "stray"
    stray.mkdir(parents=True, exist_ok=True)
    (stray / "job.toml").write_text(
        'version = 3\nname = "stray"\ndescription = "Project job."\n'
        'provider = "openrouter"\nmodel = "test-model"\ngoal = "Report."\n'
        "[budget]\nwall_clock_seconds = 10\niterations = 2\n"
        "max_completion_tokens_per_request = 100\neffect_calls = 0\n"
        "[tools]\nallow = []\n",
        encoding="utf-8",
    )
    dispatcher = ExecutionDispatcher(settings, project_scope=ProjectScope.disabled())

    assert dispatcher.project_root is None
    registry = dispatcher._jobs_for_scope(dispatcher.project_scope, PERSONAL_SCOPE)
    assert not hasattr(registry, "project_dir")
    assert registry.find("stray") is None
