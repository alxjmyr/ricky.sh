"""Bounded application service that drives the existing agent loop."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from uuid import uuid4

from ricky.agent.events import (
    AgentEvent,
    LlmResponseFinishedEvent,
    ToolCallFinishedEvent,
    ToolCallRejectedEvent,
    TurnFinishedEvent,
    WorkflowEvent,
)
from ricky.agent.loop import AgentLoop
from ricky.agent.session import AgentSession
from ricky.agent.workflow import WorkflowService
from ricky.attachments import AttachmentInput, LoadedAttachment, load_attachments
from ricky.browser.guardrails import BROWSER_READ_TOOLS
from ricky.browser.resources import (
    browser_resource_configuration_digest,
    require_browser_resource,
)
from ricky.browser.tools import browser_tool_descriptors
from ricky.capabilities import CapabilityRegistryError, tool_contract_digest, validate_tool_contract
from ricky.capabilities.enforcement import build_guardrailed_tools
from ricky.config import PersistentBrowserResourceSettings, RickySettings
from ricky.durable_tasks.scoped import ScopedDurableTaskStore
from ricky.durable_tasks.state_guard import DurableTaskStateGuard
from ricky.durable_tasks.store import DurableTaskStore
from ricky.executions.browser import (
    BrowserAttemptLease,
    BrowserExecutionBudget,
    BrowserExecutionScope,
    BrowserResourcePin,
    BrowserTransactionChallenge,
)
from ricky.executions.browser_guard import DurableBrowserExecutionGuard
from ricky.executions.browser_runtime import (
    BackgroundBrowserApprovalContext,
    BackgroundBrowserApprovalCoordinator,
)
from ricky.executions.store import ExecutionStore
from ricky.executions.types import ExecutionRequest
from ricky.jobs.batches import persist_batch, prune_batch_payloads
from ricky.jobs.briefing import job_system_sections
from ricky.jobs.browser_store import BrowserRunLedger
from ricky.jobs.effects import GuardedEffectTool, is_guardable
from ricky.jobs.escalation import escalate_blocked
from ricky.jobs.lock import FileLock, browser_worker_identity, job_lock
from ricky.jobs.notifications import enqueue_job_notification, project_job_notifications
from ricky.jobs.registry import JobRegistry, LoadedJob, context_definition_digest
from ricky.jobs.sources import JobStreamAdapter, JobStreamRegistry, PersistedBatch
from ricky.jobs.spec import JobBudget, JobSpec, PinnedExecutionRuntime, ad_hoc_job_spec
from ricky.jobs.store import JobRunStore, JobStoreError
from ricky.jobs.stream import collect_stream, validate_stream_source
from ricky.jobs.task_pool import DurableTaskPoolAdapter
from ricky.jobs.tools import (
    DISPOSITION_TOOL_NAMES,
    RecordCandidateDispositionTool,
    RecordItemDispositionTool,
    disposition_tools,
)
from ricky.jobs.transcript import JobTranscript, prune_transcripts
from ricky.jobs.types import (
    MAX_FINAL_MESSAGE_CHARS,
    MAX_RUN_ERROR_CHARS,
    JobAction,
    JobApprovalEnvelope,
    JobApprovalTool,
    JobRun,
    RunOutcome,
    RunTrigger,
)
from ricky.jobs.workflow import (
    WorkflowJobPlan,
    configured_workflow_bundle,
    prepare_workflow_job,
    resolve_workflow_job_args,
    workflow_bundle_digest,
    workflow_tool_names,
)
from ricky.llm import Provider, TextPart
from ricky.notifications import NotificationService
from ricky.permissions import PermissionEngine, Policy, PolicyRule
from ricky.profiles import ProfileScope
from ricky.project_scope import ProjectScope
from ricky.protected_values import ProtectedValueBroker, ResidentProtectedValueRegistry
from ricky.runtime import (
    BackgroundBrowserRuntime,
    SessionRuntime,
    build_capability_runtime,
    build_session_runtime,
)
from ricky.skills.registry import SkillRegistry
from ricky.skills.spec import parse_skill_markdown
from ricky.skills.tool import ReadSkillResourceTool, UseSkillTool
from ricky.tool_contracts import EffectAttemptReason
from ricky.tools import StateGuardRegistry, Tool, ToolRegistry
from ricky.workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from ricky.authority.engine import DelegatedRun

EventSink = Callable[[AgentEvent], Awaitable[None]]
BrowserApprovalNotifier = Callable[[BrowserTransactionChallenge, ProfileScope], Awaitable[None]]
_GOOGLE_ACCOUNT_TOOL_PREFIXES = ("gmail_", "gcal_")


class JobConfigurationError(ValueError):
    """A job cannot run with its declared tool profile on this machine."""


@dataclass
class _ObservedRun:
    iterations: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    terminal: TurnFinishedEvent | None = None
    current_iteration: int = 0
    external_effect_attempts: list[_ExternalEffectAttempt] = field(default_factory=list)
    workflow_run_id: str | None = None
    workflow_status: str | None = None


@dataclass(frozen=True)
class _ExternalEffectAttempt:
    """One canonical external call observed by the deterministic event stream."""

    call_id: str
    tool_name: str
    iteration: int
    input_digest: str
    disposition: Literal[
        "rejected",
        "performed",
        "not_performed",
        "in_doubt",
        "unresolved",
    ]
    attempt_reason: EffectAttemptReason | None = None
    action_id: str | None = None

    @property
    def repairable(self) -> bool:
        return self.disposition == "rejected" or self.attempt_reason == "invalid_preflight"


@dataclass(frozen=True)
class _BackgroundBrowserSetup:
    run: JobRun
    ledger: BrowserRunLedger
    attempt: BrowserAttemptLease
    broker: ProtectedValueBroker | None
    runtime: BackgroundBrowserRuntime


class JobRunner:
    """Run fresh read-only sessions with persistence, bounds, and cleanup."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        project_root: Path | None = None,
        project_scope: ProjectScope | None = None,
        store: JobRunStore | None = None,
        event_sink: EventSink | None = None,
        stream_adapters: list[JobStreamAdapter] | None = None,
        execution_store: ExecutionStore | None = None,
        protected_value_registry: ResidentProtectedValueRegistry | None = None,
        browser_approval_notifier: BrowserApprovalNotifier | None = None,
    ) -> None:
        self.settings = settings
        self.project_scope = project_scope or (
            ProjectScope.bound(project_root)
            if project_root is not None
            else ProjectScope.discover()
        )
        self.project_root = self.project_scope.root
        self.store = store or JobRunStore(settings)
        self.event_sink = event_sink
        self.stream_adapters = stream_adapters
        self.execution_store = execution_store
        self.protected_value_registry = protected_value_registry
        self.browser_approval_notifier = browser_approval_notifier
        self.notification_service = (
            NotificationService(settings) if settings.messaging.job_route is not None else None
        )

    def registry_for(self, profile_scope: ProfileScope) -> JobRegistry:
        """Build a job catalog restricted to one immutable runtime scope."""

        return JobRegistry(self.settings, profile_scope=profile_scope)

    async def run(
        self,
        name: str,
        *,
        profile_scope: ProfileScope,
        provider: Provider | None = None,
        dry_run: bool = False,
        trigger: RunTrigger = "manual",
        trigger_id: str | None = None,
        expected_spec_digest: str | None = None,
        expected_runtime_policy_digest: str | None = None,
        run_id: str | None = None,
        system_sections: dict[str, str] | None = None,
    ) -> JobRun:
        """Resolve and execute one named job."""

        loaded = self.registry_for(profile_scope).load(name)
        current_runtime_digest = runtime_policy_digest(loaded.spec, self.settings, profile_scope)
        if expected_spec_digest is not None and loaded.digest != expected_spec_digest:
            raise JobConfigurationError("scheduled job spec changed after approval")
        if (
            expected_runtime_policy_digest is not None
            and current_runtime_digest != expected_runtime_policy_digest
        ):
            raise JobConfigurationError("scheduled job runtime policy changed after approval")
        return await self._execute(
            loaded.spec,
            loaded.goal,
            loaded=loaded,
            provider=provider,
            dry_run=dry_run,
            trigger=trigger,
            trigger_id=trigger_id,
            run_id=run_id,
            system_sections=system_sections,
            profile_scope=profile_scope,
        )

    async def run_spec(
        self,
        spec: JobSpec,
        goal: str,
        *,
        source_digest: str,
        profile_scope: ProfileScope,
        provider: Provider | None = None,
        run_id: str | None = None,
        trigger_id: str | None = None,
        system_sections: dict[str, str] | None = None,
        delegation: DelegatedRun | None = None,
        pinned_runtime: PinnedExecutionRuntime,
        execution_request: ExecutionRequest | None = None,
        browser_scope: BrowserExecutionScope | None = None,
        browser_principal_id: str | None = None,
    ) -> JobRun:
        """Execute one immutable execution contract through the shared run path."""

        if not goal.strip():
            raise JobConfigurationError("execution goal cannot be blank")
        if len(source_digest) != 64:
            raise JobConfigurationError("execution source digest is invalid")
        return await self._execute(
            spec,
            goal.strip(),
            loaded=None,
            provider=provider,
            dry_run=False,
            trigger="execution",
            trigger_id=trigger_id,
            run_id=run_id,
            system_sections=system_sections,
            contract_execution=True,
            source_digest=source_digest,
            delegation=delegation,
            pinned_runtime=pinned_runtime,
            execution_request=execution_request,
            browser_scope=browser_scope,
            browser_principal_id=browser_principal_id,
            profile_scope=profile_scope,
        )

    async def validate(self, name: str, *, profile_scope: ProfileScope) -> LoadedJob:
        """Provider-free proof that a named recurring job is runnable now."""

        loaded, _ = await self.validate_revision(name, profile_scope=profile_scope)
        await self.validate_context_decision(loaded, profile_scope=profile_scope)
        return loaded

    async def validate_revision(
        self,
        name: str,
        *,
        profile_scope: ProfileScope,
    ) -> tuple[LoadedJob, JobApprovalEnvelope]:
        """Validate one exact execution revision and derive its approval envelope."""

        loaded = self.registry_for(profile_scope).load(name)
        selection = self.settings.resolve_profile_selection(
            profile_scope, loaded.spec.provider, loaded.spec.model
        )
        session = AgentSession.create(
            self.settings,
            profile_scope=profile_scope,
            provider=selection.provider,
            model=selection.model,
        )
        named_browser_scope = _named_job_browser_scope(
            loaded.spec,
            settings=self.settings,
            session=session,
        )
        async with build_capability_runtime(
            self.settings,
            session=session,
            project_scope=self.project_scope,
        ) as capabilities:
            streams = JobStreamRegistry(list(capabilities.job_stream_adapters))
            for source in loaded.spec.stream_sources:
                validate_stream_source(source, streams)
            registry = capabilities.chat_registry
            if named_browser_scope is not None:
                selected_browser_tools = set(named_browser_scope.allowed_tools)
                registry = ToolRegistry(
                    [
                        *registry.tools(),
                        *(
                            tool
                            for tool in browser_tool_descriptors()
                            if tool.name in selected_browser_tools
                        ),
                    ]
                )
            tool_names: tuple[str, ...] | None = None
            if loaded.spec.workflow is not None:
                try:
                    plan = prepare_workflow_job(
                        loaded.spec,
                        workflow_registry=capabilities.workflow_registry,
                        tool_registry=capabilities.full_registry,
                        skill_names=capabilities.skill_registry.identifiers(),
                        settings=self.settings.resolve_profile_runtime_settings(profile_scope),
                    )
                except ValueError as exc:
                    raise JobConfigurationError(str(exc)) from exc
                registry = capabilities.full_registry
                tool_names = plan.tool_names
            validate_recurring_tool_profile(
                registry,
                loaded.spec,
                store=self.store,
                task_store=capabilities.durable_tasks,
                run_id="schedule-validation",
                dry_run=True,
                profile_scope=profile_scope,
                tool_names=tool_names,
            )
            exposed_names = tuple(loaded.spec.tools.allow) if tool_names is None else tool_names
            _injected_tool_revision(loaded.spec)
            approval = _job_approval_envelope(
                loaded.spec,
                provider=selection.provider,
                registry=registry,
                tool_names=exposed_names,
                task_store=capabilities.durable_tasks,
                google_accounts=_issued_google_accounts(session, exposed_names),
                browser_scope=named_browser_scope,
            )
        return loaded, approval

    async def validate_context_decision(
        self,
        loaded: LoadedJob,
        *,
        profile_scope: ProfileScope,
        dry_run: bool = False,
    ) -> None:
        """Validate the authored lineage decision against durable prior meaning."""

        await self.store.initialize()
        await self._validate_context_values(
            job_name=loaded.resource.qualified,
            profile_scope=profile_scope,
            dry_run=dry_run,
            context_lineage=loaded.spec.context.lineage,
            context_revision=loaded.spec.context.revision,
            definition_digest=context_definition_digest(loaded),
        )

    async def once(
        self,
        goal: str,
        *,
        profile_scope: ProfileScope,
        tools: list[str] | None = None,
        provider_name: str | None = None,
        model: str | None = None,
        budget: JobBudget | None = None,
        provider: Provider | None = None,
    ) -> JobRun:
        """Execute one ad-hoc read-only goal without a named-job lock."""

        if not goal.strip():
            raise JobConfigurationError("ad-hoc goal cannot be blank")
        selection = self.settings.resolve_profile_selection(profile_scope, provider_name, model)
        spec = ad_hoc_job_spec(
            provider=selection.provider,
            model=selection.model,
            goal=goal.strip(),
            budget=budget or JobBudget(),
            tools=tools or [],
        )
        return await self._execute(
            spec,
            goal.strip(),
            loaded=None,
            provider=provider,
            dry_run=False,
            trigger="manual",
            trigger_id=None,
            run_id=None,
            system_sections=None,
            profile_scope=profile_scope,
        )

    async def _execute(
        self,
        spec: JobSpec,
        goal: str,
        *,
        loaded: LoadedJob | None,
        provider: Provider | None,
        dry_run: bool,
        trigger: RunTrigger,
        trigger_id: str | None,
        run_id: str | None = None,
        system_sections: dict[str, str] | None = None,
        contract_execution: bool = False,
        source_digest: str | None = None,
        delegation: DelegatedRun | None = None,
        pinned_runtime: PinnedExecutionRuntime | None = None,
        execution_request: ExecutionRequest | None = None,
        browser_scope: BrowserExecutionScope | None = None,
        browser_principal_id: str | None = None,
        profile_scope: ProfileScope,
    ) -> JobRun:
        await self.store.initialize()
        if self.notification_service is not None and self.settings.messaging.job_route is not None:
            await project_job_notifications(
                self.settings,
                store=self.store,
                service=self.notification_service,
                route=self.settings.messaging.job_route,
                profile_scope=profile_scope,
            )
        selection = self.settings.resolve_profile_selection(
            profile_scope, spec.provider, spec.model
        )
        session = AgentSession.create(
            self.settings,
            profile_scope=profile_scope,
            provider=selection.provider,
            model=selection.model,
        )
        named_browser_scope = _named_job_browser_scope(
            spec,
            settings=self.settings,
            session=session,
        )
        if named_browser_scope is not None:
            if contract_execution or browser_scope is not None or loaded is None:
                raise JobConfigurationError(
                    "named browser scope cannot be combined with an execution browser contract"
                )
            browser_scope = named_browser_scope
        workflow_digest, resolved_tool_names = _resolved_workflow_revision_inputs(
            spec,
            settings=self.settings,
            profile_scope=profile_scope,
        )
        run = JobRun(
            id=run_id or f"jobrun_{uuid4().hex}",
            job_name=loaded.resource.qualified if loaded is not None else None,
            spec_digest=loaded.digest if loaded is not None else source_digest,
            provider=selection.provider,
            model=selection.model,
            profile_scope=profile_scope,
            session_id=session.id,
            started_at=datetime.now(UTC),
            dry_run=dry_run,
            runtime_policy_digest=_runtime_policy_digest(
                spec,
                session,
                workflow_bundle_digest=workflow_digest,
                resolved_tool_names=resolved_tool_names,
                browser_scope_digest=(
                    named_browser_scope.digest() if named_browser_scope is not None else None
                ),
            ),
            result_notification=spec.result_notification,
            context_lineage=spec.context.lineage if loaded is not None else None,
            context_revision=spec.context.revision if loaded is not None else None,
            context_definition_digest=(
                context_definition_digest(loaded) if loaded is not None else None
            ),
            trigger=trigger,
            trigger_id=trigger_id,
        )

        run_seeded = False
        browser_ledger: BrowserRunLedger | None = None
        browser_attempt = None
        browser_broker: ProtectedValueBroker | None = None
        background_browser: BackgroundBrowserRuntime | None = None
        browser_cleanup_confirmed = False
        if browser_scope is not None:
            setup = await self._setup_background_browser(
                run=run,
                browser_scope=browser_scope,
                profile_scope=profile_scope,
                provider=selection.provider,
                contract_execution=contract_execution,
                loaded=loaded,
                execution_request=execution_request,
                browser_principal_id=browser_principal_id,
                trigger_id=trigger_id,
            )
            run = setup.run
            run_seeded = True
            browser_ledger = setup.ledger
            browser_attempt = setup.attempt
            browser_broker = setup.broker
            background_browser = setup.runtime
        lock: FileLock | None = None
        finished: JobRun | None = None
        preflight_recorded = False
        try:
            try:
                async with build_session_runtime(
                    self.settings,
                    session=session,
                    provider=provider,
                    project_scope=self.project_scope,
                    background_browser=background_browser,
                    protected_value_broker=browser_broker,
                ) as runtime:
                    workflow_plan: WorkflowJobPlan | None = None
                    try:
                        if contract_execution:
                            assert pinned_runtime is not None
                            validate_pinned_execution_runtime(runtime, pinned_runtime)
                            filtered = validate_execution_contract_tools(
                                runtime.capabilities.full_registry,
                                spec,
                                task_store=runtime.durable_tasks,
                                delegation=delegation,
                                authorized_mutating_tools=(
                                    frozenset(pinned_runtime.authorized_mutating_tools)
                                ),
                                store=self.store,
                                run_id=run.id,
                                profile_scope=profile_scope,
                            )
                            permission_engine = recurring_permission_engine(spec, dry_run=False)
                        elif loaded is None:
                            filtered = validate_tool_profile(runtime.registry, spec.tools.allow)
                            permission_engine = PermissionEngine(
                                Policy(
                                    read_only_default="allow",
                                    mutating_default="deny",
                                    destructive_default="deny",
                                )
                            )
                        else:
                            streams = self._stream_registry(runtime)
                            for source in spec.stream_sources:
                                validate_stream_source(source, streams)
                            tool_registry = runtime.registry
                            tool_names: tuple[str, ...] | None = None
                            if spec.workflow is not None:
                                try:
                                    workflow_plan = prepare_workflow_job(
                                        spec,
                                        workflow_registry=runtime.workflow_registry,
                                        tool_registry=runtime.capabilities.full_registry,
                                        skill_names=runtime.skill_registry.identifiers(),
                                        settings=self.settings.resolve_profile_runtime_settings(
                                            profile_scope
                                        ),
                                    )
                                except ValueError as exc:
                                    raise JobConfigurationError(str(exc)) from exc
                                tool_registry = runtime.capabilities.full_registry
                                tool_names = workflow_plan.tool_names
                            filtered = validate_recurring_tool_profile(
                                tool_registry,
                                spec,
                                store=self.store,
                                task_store=runtime.durable_tasks,
                                run_id=run.id,
                                dry_run=dry_run,
                                profile_scope=profile_scope,
                                tool_names=tool_names,
                            )
                            permission_engine = recurring_permission_engine(spec, dry_run=dry_run)
                    except JobConfigurationError as exc:
                        await self._record_preflight_failure(run, str(exc))
                        preflight_recorded = True
                        raise
                    if loaded is not None:
                        lock = job_lock(self.store.root, loaded.resource.qualified)
                        if not lock.acquire():
                            skipped = run.model_copy(
                                update={
                                    "outcome": "skipped_locked",
                                    "finished_at": datetime.now(UTC),
                                    "error": (
                                        f"job '{loaded.resource.qualified}' is already running"
                                    ),
                                }
                            )
                            await self.store.insert(skipped, scope=profile_scope)
                            preflight_recorded = True
                            return skipped
                    if loaded is not None:
                        try:
                            self.registry_for(profile_scope).snapshot(loaded)
                        except Exception as exc:
                            await self._record_preflight_failure(
                                run,
                                f"spec snapshot failed: {exc}"[:MAX_RUN_ERROR_CHARS],
                            )
                            preflight_recorded = True
                            raise
                        try:
                            await self._validate_context_revision(run)
                        except JobConfigurationError as exc:
                            await self._record_preflight_failure(run, str(exc))
                            preflight_recorded = True
                            raise
                    finished = await self._run_started(
                        run,
                        spec,
                        goal,
                        loaded,
                        session,
                        runtime,
                        filtered,
                        permission_engine,
                        system_sections=system_sections,
                        pinned_runtime=pinned_runtime,
                        workflow_plan=workflow_plan,
                        run_seeded=run_seeded,
                    )
                browser_cleanup_confirmed = True
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                if finished is None:
                    interrupted = run.model_copy(
                        update={
                            "outcome": "interrupted",
                            "finished_at": datetime.now(UTC),
                            "error": "job launch interrupted before execution",
                        }
                    )
                    if run_seeded:
                        await self._finish_cancellation_safe(interrupted)
                    else:
                        await self.store.insert(interrupted, scope=profile_scope)
                    return interrupted
                finished = finished.model_copy(
                    update={
                        "outcome": "interrupted",
                        "finished_at": datetime.now(UTC),
                        "error": "job interrupted during runtime cleanup",
                    }
                )
            except Exception as exc:
                if finished is not None:
                    finished = finished.model_copy(
                        update={
                            "outcome": "failed",
                            "error": f"runtime cleanup failed: {exc}"[:MAX_RUN_ERROR_CHARS],
                            "finished_at": datetime.now(UTC),
                        }
                    )
                else:
                    if not preflight_recorded:
                        await self._record_preflight_failure(
                            run,
                            f"runtime construction failed: {exc}"[:MAX_RUN_ERROR_CHARS],
                        )
                    raise
            if finished is None:
                raise RuntimeError("job runtime ended without a finished run")
            finished = await self._finish_cancellation_safe(finished)
            if (
                finished.outcome == "succeeded"
                and finished.job_name is not None
                and not finished.dry_run
            ):
                await self.store.commit_stream_cursors(finished.id, scope=profile_scope)
            if (
                self.notification_service is not None
                and self.settings.messaging.job_route is not None
            ):
                await enqueue_job_notification(
                    finished,
                    route=self.settings.messaging.job_route,
                    profile_label=session.profile_scope.label(),
                    profile_scope=profile_scope,
                    service=self.notification_service,
                )
            await prune_transcripts(
                self.store,
                keep=self.settings.jobs.transcript_retention,
                settings=self.settings,
                scope=profile_scope,
            )
            await prune_batch_payloads(
                self.store,
                scope=profile_scope,
                keep=self.settings.jobs.batch_retention,
            )
            return finished
        finally:
            if browser_attempt is not None and browser_ledger is not None:
                with suppress(Exception):
                    stored = await self.store.get(run.id, scope=profile_scope)
                    browser_status = (
                        "completed"
                        if stored.outcome == "succeeded"
                        else "cancelled"
                        if stored.outcome == "interrupted"
                        else "in_doubt"
                        if stored.outcome == "uncertain"
                        else "failed"
                    )
                    await browser_ledger.transition(
                        browser_attempt.attempt.id,
                        scope=profile_scope,
                        owner_token=browser_attempt.owner_token,
                        claim_fence=browser_attempt.attempt.claim_fence,
                        status=browser_status,
                        cleanup=("confirmed" if browser_cleanup_confirmed else "failed"),
                        error=stored.error,
                    )
            if browser_broker is not None:
                await browser_broker.aclose()
            if lock is not None:
                lock.release()

    async def _setup_background_browser(
        self,
        *,
        run: JobRun,
        browser_scope: BrowserExecutionScope,
        profile_scope: ProfileScope,
        provider: str,
        contract_execution: bool,
        loaded: LoadedJob | None,
        execution_request: ExecutionRequest | None,
        browser_principal_id: str | None,
        trigger_id: str | None,
    ) -> _BackgroundBrowserSetup:
        """Create durable browser ownership and either hand it off or terminalize it."""

        seeded = False
        ledger: BrowserRunLedger | None = None
        attempt: BrowserAttemptLease | None = None
        broker: ProtectedValueBroker | None = None
        try:
            if contract_execution:
                if (
                    execution_request is None
                    or self.execution_store is None
                    or execution_request.id != trigger_id
                    or execution_request.run_id != run.id
                    or execution_request.claim_token is None
                    or execution_request.claimed_by is None
                    or browser_principal_id is None
                ):
                    raise JobConfigurationError(
                        "background browser scope lacks an exact active execution claim"
                    )
                if browser_scope.mode == "transaction" and self.browser_approval_notifier is None:
                    raise JobConfigurationError(
                        "transaction browser execution lacks an approval notification route"
                    )
                claim_fence = execution_request.claim_fence
                worker_id = execution_request.claimed_by
                execution_request_id = execution_request.id
                claim_token = execution_request.claim_token
            else:
                if loaded is None or browser_scope.mode != "read_only":
                    raise JobConfigurationError(
                        "only named jobs may own a browser without an execution claim"
                    )
                claim_fence = 1
                worker_id = browser_worker_identity(self.store.root)
                execution_request_id = None
                claim_token = None

            if browser_scope.protected_resources and (
                self.protected_value_registry is None
                or any(
                    item.resource.profile not in self.protected_value_registry.unlocked_profiles
                    for item in browser_scope.protected_resources
                )
            ):
                raise JobConfigurationError(
                    "background protected values require a resident unlocked vault"
                )

            transcript_path = self.store.root / "transcripts" / f"{run.id}.jsonl"
            run = run.model_copy(update={"transcript_path": str(transcript_path)})
            insert = asyncio.create_task(self.store.insert(run, scope=profile_scope))
            try:
                await asyncio.shield(insert)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                await insert
                seeded = True
                raise
            seeded = True
            ledger = BrowserRunLedger(self.settings)
            initialization = asyncio.create_task(ledger.initialize())
            try:
                await asyncio.shield(initialization)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                await initialization
                raise
            resource_pin = browser_scope.resources[0] if browser_scope.resources else None
            starting = asyncio.create_task(
                ledger.start_attempt(
                    run_id=run.id,
                    scope=profile_scope,
                    browser_scope=browser_scope,
                    claim_fence=claim_fence,
                    worker_id=worker_id,
                    execution_request_id=execution_request_id,
                    resource=resource_pin.resource if resource_pin is not None else None,
                    resource_kind="persistent" if resource_pin is not None else "ephemeral",
                    resource_configuration_digest=(
                        resource_pin.configuration_digest if resource_pin is not None else None
                    ),
                )
            )
            try:
                attempt = await asyncio.shield(starting)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                attempt = await starting
                raise
            owned_attempt = cast(BrowserAttemptLease, attempt)
            running = asyncio.create_task(
                ledger.transition(
                    owned_attempt.attempt.id,
                    scope=profile_scope,
                    owner_token=owned_attempt.owner_token,
                    claim_fence=claim_fence,
                    status="running",
                )
            )
            try:
                await asyncio.shield(running)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                await running
                raise
            guard = DurableBrowserExecutionGuard(
                browser_scope=browser_scope,
                profile_scope=profile_scope,
                provider=provider,
                request_id=execution_request_id,
                attempt_id=owned_attempt.attempt.id,
                run_id=run.id,
                owner_token=owned_attempt.owner_token,
                claim_token=claim_token,
                claim_fence=claim_fence,
                resource=resource_pin.resource if resource_pin is not None else None,
                resource_configuration_digest=(
                    resource_pin.configuration_digest if resource_pin is not None else None
                ),
                executions=self.execution_store if contract_execution else None,
                ledger=ledger,
            )
            coordinator: BackgroundBrowserApprovalCoordinator | None = None
            if contract_execution:
                assert execution_request is not None
                assert browser_principal_id is not None
                assert self.execution_store is not None
                assert claim_token is not None
                context = BackgroundBrowserApprovalContext(
                    request_id=execution_request.id,
                    task_id=cast(str, execution_request.task_id),
                    contract_digest=cast(str, execution_request.contract_digest),
                    run_id=run.id,
                    attempt_id=owned_attempt.attempt.id,
                    claim_token=claim_token,
                    claim_fence=claim_fence,
                    principal_id=browser_principal_id,
                    conversation_id=(execution_request.source_conversation_id or "local-execution"),
                    source_message_id=(execution_request.source_message_id or execution_request.id),
                    approval_ttl_seconds=browser_scope.budget.approval_ttl_seconds,
                    profile_scope=profile_scope,
                    owner_token=owned_attempt.owner_token,
                    provider=provider,
                    resource=resource_pin.resource if resource_pin is not None else None,
                    resource_kind="persistent" if resource_pin is not None else "ephemeral",
                    resource_configuration_digest=(
                        resource_pin.configuration_digest if resource_pin is not None else None
                    ),
                    budget_ceiling=browser_scope.budget,
                    attachments=browser_scope.attachments,
                )

                async def notify_browser_approval(
                    challenge: BrowserTransactionChallenge,
                ) -> None:
                    if self.browser_approval_notifier is None:
                        raise JobConfigurationError("browser approval notification is unavailable")
                    await self.browser_approval_notifier(challenge, profile_scope)

                coordinator = BackgroundBrowserApprovalCoordinator(
                    context=context,
                    store=self.execution_store,
                    ledger=ledger,
                    notifier=notify_browser_approval,
                    protected_values=None,
                )
            if browser_scope.protected_resources:
                assert self.protected_value_registry is not None
                assert coordinator is not None
                broker = self.protected_value_registry.lease(
                    scope=profile_scope,
                    consumer_ids=frozenset({"browser.fill"}),
                    destination_responder=coordinator.approve_destination,
                )
                coordinator.protected_values = broker

            async def resolve_browser_attachments(
                attachment_ids: tuple[str, ...],
            ) -> tuple[LoadedAttachment, ...]:
                return await _resolve_execution_browser_attachments(
                    self.settings,
                    project_root=self.project_root,
                    profile_scope=profile_scope,
                    scope=browser_scope,
                    attachment_ids=attachment_ids,
                )

            runtime = BackgroundBrowserRuntime(
                mode=browser_scope.mode,
                allowed_tools=frozenset(browser_scope.allowed_tools),
                guard=guard,
                tool_wrapper=coordinator.wrap if coordinator is not None else None,
                attachment_resolver=(
                    resolve_browser_attachments if browser_scope.attachments else None
                ),
            )
            return _BackgroundBrowserSetup(
                run=run,
                ledger=ledger,
                attempt=owned_attempt,
                broker=broker,
                runtime=runtime,
            )
        except BaseException as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            if cancelled:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
            error = (
                "job launch interrupted during browser setup"
                if cancelled
                else f"browser runtime setup failed: {exc}"[:MAX_RUN_ERROR_CHARS]
            )

            async def terminalize() -> None:
                try:
                    if seeded:
                        terminal = run.model_copy(
                            update={
                                "outcome": "interrupted" if cancelled else "failed",
                                "finished_at": datetime.now(UTC),
                                "error": error,
                            }
                        )
                        await self._finish_cancellation_safe(terminal)
                    if attempt is not None and ledger is not None:
                        await ledger.transition(
                            attempt.attempt.id,
                            scope=profile_scope,
                            owner_token=attempt.owner_token,
                            claim_fence=attempt.attempt.claim_fence,
                            status="cancelled" if cancelled else "failed",
                            cleanup="confirmed",
                            error=error,
                        )
                finally:
                    if broker is not None:
                        await broker.aclose()

            cleanup = asyncio.create_task(terminalize())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                await cleanup
            raise

    async def _finish_cancellation_safe(self, run: JobRun) -> JobRun:
        live = await self.store.get(run.id, scope=run.profile_scope)
        final = run.model_copy(update={"effect_calls": live.effect_calls})
        task = asyncio.create_task(self.store.finish(final, scope=run.profile_scope))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            await task
        return await self.store.get(run.id, scope=run.profile_scope)

    async def _record_preflight_failure(self, run: JobRun, error: str) -> None:
        failed = run.model_copy(
            update={
                "outcome": "failed",
                "finished_at": datetime.now(UTC),
                "error": error[:MAX_RUN_ERROR_CHARS],
            }
        )
        try:
            await self.store.get(run.id, scope=run.profile_scope)
        except JobStoreError:
            await self.store.insert(failed, scope=run.profile_scope)
        else:
            await self._finish_cancellation_safe(failed)

    async def _run_started(
        self,
        run: JobRun,
        spec: JobSpec,
        goal: str,
        loaded: LoadedJob | None,
        session: AgentSession,
        runtime: SessionRuntime,
        filtered: ToolRegistry,
        permission_engine: PermissionEngine,
        *,
        system_sections: dict[str, str] | None = None,
        pinned_runtime: PinnedExecutionRuntime | None = None,
        workflow_plan: WorkflowJobPlan | None = None,
        run_seeded: bool = False,
    ) -> JobRun:
        transcript_path = self.store.root / "transcripts" / f"{run.id}.jsonl"
        live = run.model_copy(
            update={
                "transcript_path": str(transcript_path),
                "workflow_name": (
                    workflow_plan.qualified_name if workflow_plan is not None else None
                ),
            }
        )
        if not run_seeded:
            await self.store.insert(live, scope=run.profile_scope)
        transcript = JobTranscript(transcript_path)
        observed = _ObservedRun()
        task: asyncio.Task[None] | None = None
        outcome: RunOutcome = "failed"
        error: str | None = None
        opened = False
        batches: list[PersistedBatch] = []
        try:
            await transcript.open()
            opened = True
            sections = job_system_sections(spec, named=loaded is not None)
            sections.update(system_sections or {})
            selected_skills = runtime.skill_registry
            if pinned_runtime is not None:
                selected_skills = _pinned_skill_registry(pinned_runtime)
                filtered = _rebind_skill_tools(filtered, selected_skills)
                filtered = ToolRegistry(
                    build_guardrailed_tools(
                        list(filtered.tools()),
                        guardrails=pinned_runtime.guardrails,
                        guardrail_tools=pinned_runtime.guardrail_tools,
                        registry=runtime.capabilities.guardrail_registry,
                    )
                )
            if pinned_runtime is not None and pinned_runtime.skill_instructions:
                sections["contract_skills"] = "\n\n".join(
                    f"## {name}\n{body}"
                    for name, body in sorted(pinned_runtime.skill_instructions.items())
                )
            if loaded is not None:
                batches, recurring_sections = await self._prepare_recurring(live, loaded, runtime)
                sections.update(recurring_sections)
                if batches:
                    filtered = ToolRegistry(
                        [
                            *filtered.tools(),
                            *cast(
                                list[Tool],
                                disposition_tools(
                                    self.store,
                                    runtime.durable_tasks,
                                    job_name=loaded.resource.qualified,
                                    batches=batches,
                                    dry_run=live.dry_run,
                                ),
                            ),
                        ]
                    )
            if workflow_plan is not None:
                assert loaded is not None
                task = asyncio.create_task(
                    self._consume_workflow_job(
                        run=live,
                        spec=spec,
                        goal=goal,
                        plan=workflow_plan,
                        session=session,
                        runtime=runtime,
                        registry=filtered,
                        permission_engine=permission_engine,
                        transcript=transcript,
                        observed=observed,
                    )
                )
            else:
                loop = AgentLoop(
                    provider=runtime.provider,
                    registry=filtered,
                    settings=self.settings,
                    permission_engine=permission_engine,
                    skill_registry=selected_skills,
                    memory=(
                        runtime.memory
                        if pinned_runtime is None or pinned_runtime.memory_index
                        else None
                    ),
                    workflow_registry=None,
                    cwd=self.project_root,
                    artifact_store=runtime.capabilities.session_artifacts,
                    deferred_tools=tuple(
                        tool
                        for tool in (
                            filtered.tools()
                            if pinned_runtime is not None
                            else runtime.capabilities.tools
                        )
                        if getattr(tool, "deferred_until_artifact", False)
                    ),
                )
                task = asyncio.create_task(
                    self._consume(
                        loop,
                        session=session,
                        goal=goal,
                        budget=spec.budget,
                        loaded=loaded,
                        transcript=transcript,
                        observed=observed,
                        sections=sections,
                    )
                )
            done, _ = await asyncio.wait({task}, timeout=spec.budget.wall_clock_seconds)
            if not done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                refresh_error: str | None = None
                if workflow_plan is not None:
                    refresh_error = await self._refresh_interrupted_workflow(
                        live, workflow_plan, observed
                    )
                outcome = "budget_exceeded"
                error = f"wall-clock budget exceeded: {spec.budget.wall_clock_seconds:g} seconds"
                if refresh_error is not None:
                    error = f"{error}; {refresh_error}"[:MAX_RUN_ERROR_CHARS]
            else:
                await task
                if workflow_plan is not None:
                    outcome, error = _workflow_terminal_outcome(observed.workflow_status)
                else:
                    outcome, error = _terminal_outcome(observed.terminal)
                if outcome == "succeeded" and not live.dry_run:
                    outcome, error = await self._verified_effect_outcome(
                        live.id,
                        profile_scope=live.profile_scope,
                        observed=observed,
                    )
                if outcome == "succeeded" and batches:
                    try:
                        await self.store.verify_run_accounting(live.id, scope=live.profile_scope)
                    except Exception as exc:
                        outcome = "failed"
                        error = str(exc)[:MAX_RUN_ERROR_CHARS]
                if outcome == "succeeded" and not live.dry_run and loaded is not None:
                    await self._escalate_blocked(live, loaded, runtime, batches)
        except asyncio.CancelledError:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            refresh_error = None
            if workflow_plan is not None:
                refresh_error = await self._refresh_interrupted_workflow(
                    live, workflow_plan, observed
                )
            outcome = "interrupted"
            error = "job run interrupted"
            if refresh_error is not None:
                error = f"{error}; {refresh_error}"[:MAX_RUN_ERROR_CHARS]
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
        except Exception as exc:  # noqa: BLE001 - persisted as a bounded run failure.
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            outcome = "failed"
            error = f"{type(exc).__name__}: {exc}"[:MAX_RUN_ERROR_CHARS]
            if workflow_plan is not None:
                refresh_error = await self._refresh_interrupted_workflow(
                    live, workflow_plan, observed
                )
                if refresh_error is not None:
                    error = f"{error}; {refresh_error}"[:MAX_RUN_ERROR_CHARS]
        finally:
            if opened:
                try:
                    await transcript.close()
                except Exception as exc:  # noqa: BLE001 - preserve the primary outcome.
                    if error is None:
                        outcome = "failed"
                        error = f"transcript close failed: {exc}"[:MAX_RUN_ERROR_CHARS]

        finished = live.model_copy(
            update={
                "outcome": outcome,
                "finished_at": datetime.now(UTC),
                "iterations": observed.iterations,
                "prompt_tokens": observed.prompt_tokens,
                "completion_tokens": observed.completion_tokens,
                "final_message": _final_message(session),
                "error": error,
                "workflow_args": (
                    (await self.store.get(live.id, scope=live.profile_scope)).workflow_args
                    if workflow_plan is not None
                    else None
                ),
                "workflow_run_id": observed.workflow_run_id,
                "workflow_status": observed.workflow_status,
            }
        )
        return finished

    async def _refresh_interrupted_workflow(
        self,
        run: JobRun,
        plan: WorkflowJobPlan,
        observed: _ObservedRun,
    ) -> str | None:
        """Project the workflow checkpoint after cancellation has been joined."""

        if observed.workflow_run_id is None:
            return None
        try:
            workflow = await WorkflowRunStore(self.settings, project_root=self.project_root).load(
                observed.workflow_run_id,
                profile_scope=run.profile_scope,
                scope=plan.storage_scope,
            )
            observed.workflow_status = workflow.status
            stored = await self.store.get(run.id, scope=run.profile_scope)
            if stored.workflow_args is None:
                return None
            await self.store.update_workflow_link(
                run.id,
                scope=run.profile_scope,
                workflow_name=plan.qualified_name,
                workflow_args=stored.workflow_args,
                workflow_run_id=workflow.id,
                workflow_status=workflow.status,
            )
        except Exception as exc:  # noqa: BLE001 - preserve the primary stop reason.
            return f"workflow checkpoint refresh failed: {exc}"
        return None

    async def _verified_effect_outcome(
        self,
        run_id: str,
        *,
        profile_scope: ProfileScope,
        observed: _ObservedRun,
    ) -> tuple[RunOutcome, str | None]:
        """Prevent model prose from overriding external-effect receipt evidence."""

        actions = await self.store.actions_for_run(run_id, scope=profile_scope)
        return _effect_truth_outcome(actions, observed.external_effect_attempts)

    async def _prepare_recurring(
        self,
        run: JobRun,
        loaded: LoadedJob,
        runtime: SessionRuntime,
    ) -> tuple[list[PersistedBatch], dict[str, str]]:
        batches: list[PersistedBatch] = []
        context: list[dict[str, object]] = []
        stream_registry = self._stream_registry(runtime)
        for source in loaded.spec.stream_sources:
            payload = await collect_stream(
                source,
                registry=stream_registry,
                store=self.store,
                job_name=loaded.resource.qualified,
                profile_scope=run.profile_scope,
            )
            batch = await persist_batch(
                self.store,
                run_id=run.id,
                job_name=loaded.resource.qualified,
                source_name=source.name,
                kind="stream",
                payload=payload,
                item_ids=[item.id for item in payload.items],
                complete=payload.complete,
                dry_run=run.dry_run,
                profile_scope=run.profile_scope,
                upper_bound=payload.upper_bound,
                input_cursor=payload.input_cursor,
                next_cursor=payload.next_cursor,
            )
            batches.append(batch)
            context.append(
                {
                    "batch_id": batch.id,
                    "source": source.name,
                    "kind": "stream",
                    "complete": payload.complete,
                    "items": [item.model_dump(mode="json") for item in payload.items],
                }
            )
        if loaded.spec.task_sources:
            payload = await DurableTaskPoolAdapter().collect(
                loaded.spec.task_sources,
                task_store=runtime.durable_tasks,
                job_store=self.store,
                job_name=loaded.resource.qualified,
                profile_scope=run.profile_scope,
                total_limit=self.settings.jobs.candidate_limit,
            )
            batch = await persist_batch(
                self.store,
                run_id=run.id,
                job_name=loaded.resource.qualified,
                source_name="task-pool",
                kind="task_pool",
                payload=payload,
                item_ids=[candidate.identity for candidate in payload.candidates],
                complete=True,
                dry_run=run.dry_run,
                profile_scope=run.profile_scope,
            )
            batches.append(batch)
            context.append(
                {
                    "batch_id": batch.id,
                    "source": "task-pool",
                    "kind": "task_pool",
                    "candidates": [
                        candidate.model_dump(mode="json") | {"candidate_id": candidate.identity}
                        for candidate in payload.candidates
                    ],
                }
            )
        sections = {
            "recurring_inputs": (
                "These inputs were persisted before reasoning. Account for every exact identity "
                "with record_item_disposition or record_candidate_disposition. Task tags are "
                "eligibility keys, not assignment or authority. Claim tasks with the shown exact "
                "revision. A dry run must only reason and record dry-run dispositions.\n"
                + json.dumps(context, sort_keys=True, separators=(",", ":"))
            )
        }
        sections.update(await self._history_sections(run, loaded))
        return batches, sections

    def _stream_registry(self, runtime: SessionRuntime) -> JobStreamRegistry:
        adapters = (
            self.stream_adapters
            if self.stream_adapters is not None
            else list(runtime.capabilities.job_stream_adapters)
        )
        return JobStreamRegistry(adapters)

    async def _history_sections(self, run: JobRun, loaded: LoadedJob) -> dict[str, str]:
        assert run.context_lineage is not None
        prior = await self.store.list_context_runs(
            scope=run.profile_scope,
            job_name=loaded.resource.qualified,
            dry_run=run.dry_run,
            context_lineage=run.context_lineage,
            limit=self.settings.jobs.history_summary_limit + 1,
        )
        summaries: list[str] = []
        for item in prior:
            if item.id == run.id or item.outcome is None:
                continue
            if item.final_message:
                summaries.append(
                    f"{item.finished_at.isoformat() if item.finished_at else item.id}: "
                    f"{item.outcome}: {item.final_message[:2000]}"
                )
        actions = await self.store.list_actions(
            scope=run.profile_scope,
            job_name=loaded.resource.qualified,
            limit=self.settings.jobs.history_summary_limit or 1,
        )
        effect_lines = [
            f"{action.operation} {action.target} occurrence={action.occurrence} "
            f"status={action.status}: {action.summary}"
            for action in actions
        ]
        return {
            "job_history": "Own-job bounded prior summaries:\n"
            + ("\n".join(summaries) if summaries else "[none]"),
            "job_effect_history": "Own-job bounded prior effects:\n"
            + ("\n".join(effect_lines) if effect_lines else "[none]"),
        }

    async def _validate_context_revision(self, run: JobRun) -> None:
        """Require authored acknowledgement before changed meaning reuses a lineage."""

        assert run.job_name is not None
        assert run.context_lineage is not None
        assert run.context_revision is not None
        assert run.context_definition_digest is not None
        await self._validate_context_values(
            job_name=run.job_name,
            profile_scope=run.profile_scope,
            dry_run=run.dry_run,
            context_lineage=run.context_lineage,
            context_revision=run.context_revision,
            definition_digest=run.context_definition_digest,
        )

    async def _validate_context_values(
        self,
        *,
        job_name: str,
        profile_scope: ProfileScope,
        dry_run: bool,
        context_lineage: int,
        context_revision: int,
        definition_digest: str,
    ) -> None:
        evidence = await self.store.context_revision_evidence(
            scope=profile_scope,
            job_name=job_name,
            dry_run=dry_run,
            context_lineage=context_lineage,
        )
        if not evidence:
            return
        latest_revision = max(item.revision for item in evidence)
        if context_revision < latest_revision:
            raise JobConfigurationError(
                "job context revision moved backward within the current lineage"
            )
        if context_revision == latest_revision and any(
            item.revision == context_revision
            and item.definition_digest is not None
            and item.definition_digest != definition_digest
            for item in evidence
        ):
            raise JobConfigurationError(
                "job goal or workflow changed without a context decision; increment "
                "context.revision to preserve prior context, or increment context.lineage "
                "and reset context.revision to 1 to start fresh"
            )

    async def _escalate_blocked(
        self,
        run: JobRun,
        loaded: LoadedJob,
        runtime: SessionRuntime,
        batches: list[PersistedBatch],
    ) -> None:
        for batch in batches:
            for disposition in await self.store.dispositions(batch.id, scope=run.profile_scope):
                if disposition.kind != "blocked":
                    continue
                await escalate_blocked(
                    job_store=self.store,
                    task_store=runtime.durable_tasks,
                    job_name=loaded.resource.qualified,
                    run_id=run.id,
                    source_identity=f"{batch.source_name}:{disposition.item_id}",
                    summary=disposition.summary or "Recurring work requires user input.",
                    profile_scope=run.profile_scope,
                )

    async def _consume_workflow_job(
        self,
        *,
        run: JobRun,
        spec: JobSpec,
        goal: str,
        plan: WorkflowJobPlan,
        session: AgentSession,
        runtime: SessionRuntime,
        registry: ToolRegistry,
        permission_engine: PermissionEngine,
        transcript: JobTranscript,
        observed: _ObservedRun,
    ) -> None:
        """Resolve an invocation and run its workflow under the job envelope."""

        prior_success: JobRun | None = None
        if run.job_name is not None:
            assert run.context_lineage is not None
            prior = await self.store.list_context_runs(
                scope=run.profile_scope,
                job_name=run.job_name,
                dry_run=run.dry_run,
                context_lineage=run.context_lineage,
                limit=self.settings.jobs.history_summary_limit + 2,
            )
            prior_success = next(
                (item for item in prior if item.id != run.id and item.outcome == "succeeded"),
                None,
            )
        resolution = await resolve_workflow_job_args(
            spec,
            plan=plan,
            goal=goal,
            provider=runtime.provider,
            session=session,
            prior_success=prior_success,
            settings=self.settings.resolve_profile_runtime_settings(run.profile_scope),
        )
        observed.iterations += resolution.attempts
        observed.prompt_tokens += resolution.usage.prompt_tokens
        observed.completion_tokens += resolution.usage.completion_tokens
        await self.store.update_workflow_link(
            run.id,
            scope=run.profile_scope,
            workflow_name=plan.qualified_name,
            workflow_args=resolution.args,
        )
        if runtime.workflow_registry is None:
            raise JobConfigurationError("workflows are disabled")
        skill_bodies = {
            identity: skill.body
            for identity in runtime.skill_registry.identifiers()
            if (skill := runtime.skill_registry.get(identity)) is not None
        }
        workflow_store = WorkflowRunStore(self.settings, project_root=self.project_root)
        service = WorkflowService(
            provider=runtime.provider,
            tool_registry=registry,
            settings=self.settings.resolve_profile_runtime_settings(run.profile_scope),
            workflow_registry=runtime.workflow_registry,
            skill_bodies=skill_bodies,
            permission_engine=permission_engine,
            run_store=workflow_store,
            cwd=self.project_root,
            dry_run=run.dry_run,
        )
        events = service.start(session, plan.qualified_name, resolution.args)
        try:
            async for event in events:
                await transcript.append(event)
                self._observe_effect_event(event, observed)
                if isinstance(event, WorkflowEvent):
                    if event.action == "run_created":
                        observed.workflow_run_id = event.run_id
                        observed.workflow_status = "pending"
                        await self.store.update_workflow_link(
                            run.id,
                            scope=run.profile_scope,
                            workflow_name=plan.qualified_name,
                            workflow_args=resolution.args,
                            workflow_run_id=event.run_id,
                            workflow_status="pending",
                        )
                    elif event.action == "run_completed":
                        status = event.details.get("status")
                        if isinstance(status, str):
                            observed.workflow_status = status
                if self.event_sink is not None:
                    await self.event_sink(event)
        finally:
            await events.aclose()
        if observed.workflow_run_id is None:
            raise RuntimeError("workflow execution did not emit a run identity")
        workflow_run = await workflow_store.load(
            observed.workflow_run_id,
            profile_scope=run.profile_scope,
            scope=plan.storage_scope,
        )
        observed.workflow_status = workflow_run.status
        observed.iterations += sum(
            len(record.attempts)
            for record in [
                *workflow_run.steps.values(),
                *(
                    record
                    for items in workflow_run.item_runs.values()
                    for item in items
                    for record in item.steps.values()
                ),
            ]
        )
        observed.prompt_tokens += workflow_run.cumulative_usage.prompt_tokens
        observed.completion_tokens += workflow_run.cumulative_usage.completion_tokens
        await self.store.update_workflow_link(
            run.id,
            scope=run.profile_scope,
            workflow_name=plan.qualified_name,
            workflow_args=resolution.args,
            workflow_run_id=workflow_run.id,
            workflow_status=workflow_run.status,
        )

    async def _consume(
        self,
        loop: AgentLoop,
        *,
        session: AgentSession,
        goal: str,
        budget: JobBudget,
        loaded: LoadedJob | None,
        transcript: JobTranscript,
        observed: _ObservedRun,
        sections: dict[str, str],
    ) -> None:
        async for event in loop.run_turn(
            session,
            goal,
            max_iterations=budget.iterations,
            max_completion_tokens_per_request=budget.max_completion_tokens_per_request,
            extra_system_sections=sections,
        ):
            await transcript.append(event)
            if self.event_sink is not None:
                await self.event_sink(event)
            if isinstance(event, TurnFinishedEvent):
                observed.terminal = event
                observed.iterations = event.iterations
                observed.prompt_tokens = event.usage.prompt_tokens
                observed.completion_tokens = event.usage.completion_tokens
            elif isinstance(event, LlmResponseFinishedEvent):
                observed.current_iteration = event.iteration
            self._observe_effect_event(event, observed)

    @staticmethod
    def _observe_effect_event(event: AgentEvent, observed: _ObservedRun) -> None:
        if isinstance(event, ToolCallRejectedEvent) and event.external_effect:
            observed.external_effect_attempts.append(
                _ExternalEffectAttempt(
                    call_id=event.call_id,
                    tool_name=event.tool_name,
                    iteration=observed.current_iteration,
                    input_digest=event.input_digest,
                    disposition="rejected",
                )
            )
        elif isinstance(event, ToolCallFinishedEvent) and event.effect_kind == "external":
            observed.external_effect_attempts.append(
                _ExternalEffectAttempt(
                    call_id=event.call_id,
                    tool_name=event.tool_name,
                    iteration=observed.current_iteration,
                    input_digest=event.input_digest or _event_call_digest(event),
                    disposition=event.effect_disposition or "unresolved",
                    attempt_reason=event.effect_attempt_reason,
                    action_id=event.effect_action_id,
                )
            )


async def _resolve_execution_browser_attachments(
    settings: RickySettings,
    *,
    project_root: Path | None,
    profile_scope: ProfileScope,
    scope: BrowserExecutionScope,
    attachment_ids: tuple[str, ...],
) -> tuple[LoadedAttachment, ...]:
    """Freeze only exact task artifacts pinned by one execution contract."""

    pins = {item.id: item for item in scope.attachments}
    if (
        not attachment_ids
        or len(attachment_ids) != len(set(attachment_ids))
        or any(attachment_id not in pins for attachment_id in attachment_ids)
    ):
        raise ValueError("browser upload requested attachments outside the execution contract")
    selected = [pins[attachment_id] for attachment_id in attachment_ids]
    loaded = await asyncio.to_thread(
        load_attachments,
        [
            AttachmentInput(
                task_id=pin.task_id,
                task_artifact_path=pin.artifact_path,
                profile=pin.profile,
            )
            for pin in selected
        ],
        cwd=project_root or Path.cwd(),
        settings=settings,
        profile_scope=profile_scope,
        count_limit=min(settings.browser.upload_count_limit, scope.budget.uploads),
        file_byte_limit=min(
            settings.browser.upload_file_byte_limit,
            scope.budget.upload_bytes,
        ),
        total_byte_limit=min(
            settings.browser.upload_total_byte_limit,
            scope.budget.upload_bytes,
        ),
    )
    if any(
        attachment.sha256 != pin.sha256 or attachment.size_bytes != pin.byte_count
        for attachment, pin in zip(loaded, selected, strict=True)
    ):
        raise ValueError("browser upload attachment changed after execution approval")
    return tuple(loaded)


def _effect_truth_outcome(
    actions: list[JobAction],
    attempts: list[_ExternalEffectAttempt],
) -> tuple[RunOutcome, str | None]:
    """Reconcile every observed canonical call with its strongest durable evidence."""

    if any(action.status in {"reserved", "in_doubt"} for action in actions):
        return (
            "uncertain",
            "external effect lacks a confirmed receipt; operator reconciliation required",
        )

    remaining = _remaining_external_effect_attempts(attempts)
    actions_by_id = {action.id: action for action in actions}
    correlated_action_ids = [
        attempt.action_id for attempt in remaining if attempt.action_id is not None
    ]
    uncorrelated = any(
        attempt.disposition in {"performed", "in_doubt", "unresolved"}
        and (
            attempt.action_id is None
            or attempt.action_id not in actions_by_id
            or actions_by_id[attempt.action_id].status not in {"performed", "in_doubt", "reserved"}
        )
        for attempt in remaining
    )
    duplicate_correlation = len(correlated_action_ids) != len(set(correlated_action_ids))
    missing_observation = any(action.id not in correlated_action_ids for action in actions)
    if (
        uncorrelated
        or duplicate_correlation
        or missing_observation
        or any(attempt.disposition in {"in_doubt", "unresolved"} for attempt in remaining)
    ):
        return (
            "uncertain",
            "external effect lacks a confirmed receipt; operator reconciliation required",
        )

    if any(action.status == "not_performed" for action in actions):
        return "failed", "external effect was confirmed not performed"
    if any(attempt.disposition in {"rejected", "not_performed"} for attempt in remaining):
        return "failed", "external effect call did not produce a performed receipt"
    return "succeeded", None


def _remaining_external_effect_attempts(
    attempts: list[_ExternalEffectAttempt],
) -> list[_ExternalEffectAttempt]:
    """Remove exact duplicate conflicts and one-to-one corrections of invalid proposals."""

    canonical: list[tuple[int, _ExternalEffectAttempt]] = []
    seen_repairable: set[tuple[int, str, str, str]] = set()
    for position, attempt in enumerate(attempts):
        if attempt.repairable:
            key = (
                attempt.iteration,
                attempt.tool_name,
                attempt.input_digest,
                attempt.disposition,
            )
            if key in seen_repairable:
                continue
            seen_repairable.add(key)
        canonical.append((position, attempt))

    performed_keys = {
        (attempt.tool_name, attempt.input_digest)
        for _, attempt in canonical
        if attempt.disposition == "performed" and attempt.action_id is not None
    }
    superseded = {
        position
        for position, attempt in canonical
        if attempt.disposition == "not_performed"
        and attempt.attempt_reason == "denied"
        and (attempt.tool_name, attempt.input_digest) in performed_keys
    }
    for performed_position, performed in canonical:
        if performed.disposition != "performed":
            continue
        candidates = [
            (position, attempt)
            for position, attempt in canonical
            if position not in superseded
            and attempt.repairable
            and attempt.tool_name == performed.tool_name
            and attempt.iteration < performed.iteration
            and position < performed_position
        ]
        if candidates:
            superseded.add(max(candidates, key=lambda item: (item[1].iteration, item[0]))[0])
    return [attempt for position, attempt in canonical if position not in superseded]


def _event_call_digest(event: ToolCallFinishedEvent) -> str:
    """Bounded compatibility identity for older serialized finish events."""

    return hashlib.sha256(f"{event.tool_name}:{event.call_id}".encode()).hexdigest()


def _named_job_browser_scope(
    spec: JobSpec,
    *,
    settings: RickySettings,
    session: AgentSession,
) -> BrowserExecutionScope | None:
    """Compile one authored named-job browser ceiling against current local policy."""

    descriptor_names = {tool.name for tool in browser_tool_descriptors()}
    selected = set(spec.tools.allow) & descriptor_names
    browser_named = {name for name in spec.tools.allow if name.startswith("browser_")}
    if spec.browser is None:
        if browser_named:
            raise JobConfigurationError(
                "named browser tools require an explicit [browser] job scope"
            )
        return None
    if spec.workflow is not None:
        raise JobConfigurationError("workflow-backed jobs cannot own a browser")
    if not selected or browser_named != selected:
        raise JobConfigurationError("named browser scope requires only recognized browser tools")
    read_ceiling = set(BROWSER_READ_TOOLS) | {"browser_session_open_resource"}
    if not selected <= read_ceiling:
        raise JobConfigurationError("named jobs may select only the read-oriented browser surface")

    runtime = settings.resolve_profile_runtime_settings(session.profile_scope)
    owner = runtime.browser.background
    if not (runtime.browser.enabled and owner.enabled and owner.read_enabled):
        raise JobConfigurationError("named background browser research is disabled")
    if spec.browser.allow_public_https_research and not owner.allow_public_https_research:
        raise JobConfigurationError("public HTTPS background research is disabled")

    resource_pins: tuple[BrowserResourcePin, ...] = ()
    if spec.browser.resource is None:
        if not owner.allow_ephemeral:
            raise JobConfigurationError("ephemeral background browsers are disabled")
        if "browser_session_open" not in selected:
            raise JobConfigurationError(
                "an ephemeral named browser scope must expose browser_session_open"
            )
        if "browser_session_open_resource" in selected:
            raise JobConfigurationError(
                "an ephemeral named browser scope cannot expose resource opening"
            )
    else:
        if not owner.interaction_enabled:
            raise JobConfigurationError(
                "persistent browser disclosure requires background interaction enabled"
            )
        if "browser_session_open_resource" not in selected:
            raise JobConfigurationError(
                "a persistent named browser scope must expose browser_session_open_resource"
            )
        if "browser_session_open" in selected:
            raise JobConfigurationError(
                "a persistent named browser scope cannot expose ephemeral session opening"
            )
        resolved = require_browser_resource(
            runtime,
            scope=session.profile_scope,
            ref=spec.browser.resource,
        )
        if (
            resolved is None
            or not isinstance(resolved.settings, PersistentBrowserResourceSettings)
            or resolved.settings.headless is not True
        ):
            raise JobConfigurationError(
                "named browser resource must be Ricky-owned, persistent, and headless"
            )
        resource_pins = (
            BrowserResourcePin(
                resource=resolved.ref,
                kind="persistent",
                configuration_digest=browser_resource_configuration_digest(resolved),
                authenticated_origin_ceiling=spec.browser.allowed_origins,
            ),
        )

    if "browser_visual_snapshot" in selected:
        launch_profile = (
            spec.browser.resource.profile
            if spec.browser.resource is not None
            else session.profile_scope.primary
        )
        profile = runtime.profile_configs.get(launch_profile)
        if (
            not spec.browser.allow_masked_visual_observations
            or profile is None
            or profile.browser is None
            or session.provider not in profile.browser.screenshot_allowed_providers
        ):
            raise JobConfigurationError(
                "named visual snapshots require exact job and provider disclosure approval"
            )
    elif spec.browser.allow_masked_visual_observations:
        raise JobConfigurationError(
            "named browser enables visual disclosure without exposing browser_visual_snapshot"
        )

    operations: set[str] = {"session_starts"}
    if selected & {"browser_session_open", "browser_session_open_resource"}:
        operations.add("controlled_pages")
    if "browser_navigate" in selected:
        operations.add("navigations")
    if "browser_scroll" in selected:
        operations.add("scrolls")
    if "browser_snapshot" in selected:
        operations.add("semantic_observations")
    if "browser_visual_snapshot" in selected:
        operations.add("visual_observations")
    budget = BrowserExecutionBudget.model_validate(
        owner.budget.model_dump(mode="json"),
        strict=True,
    )
    return BrowserExecutionScope.model_validate(
        {
            "mode": "read_only",
            "resources": resource_pins,
            "allow_ephemeral": spec.browser.resource is None,
            "allow_public_https_research": (
                spec.browser.allow_public_https_research and owner.allow_public_https_research
            ),
            "private_origin_ceiling": spec.browser.allowed_origins,
            "allowed_tools": tuple(sorted(selected)),
            "allowed_operations": tuple(sorted(operations)),
            "allow_masked_visual_observations": (spec.browser.allow_masked_visual_observations),
            "budget": budget,
        },
        strict=True,
    )


def _job_approval_envelope(
    spec: JobSpec,
    *,
    provider: str,
    registry: ToolRegistry,
    tool_names: tuple[str, ...],
    task_store: DurableTaskStore | ScopedDurableTaskStore,
    google_accounts: dict[str, str],
    browser_scope: BrowserExecutionScope | None,
) -> JobApprovalEnvelope:
    """Project the exact authority and trust facts that require reapproval."""

    state_guards = StateGuardRegistry([DurableTaskStateGuard(task_store)])
    tools: dict[str, JobApprovalTool] = {}
    for name in sorted(tool_names):
        tool = registry.get(name)
        if tool is None:
            raise JobConfigurationError(f"job tool is unknown or unavailable: {name}")
        metadata = validate_tool_contract(tool, state_guards=state_guards)
        tools[name] = JobApprovalTool.model_validate(
            {
                "contract_digest": tool_contract_digest(tool),
                "risk": tool.risk,
                "effect_kind": metadata["effect_kind"],
                "unattended": metadata["unattended"],
                "state_guard_id": metadata["state_guard_id"],
            }
        )
    source_scopes: dict[str, str] = {}
    for source in spec.task_sources:
        payload = source.model_dump(mode="json")
        payload.pop("limit")
        payload.pop("reconsider_after_hours")
        source_scopes[f"task:{source.name}"] = _canonical_json(payload)
    for source in spec.stream_sources:
        payload = source.model_dump(mode="json")
        payload.pop("item_limit")
        source_scopes[f"stream:{source.name}"] = _canonical_json(payload)
    return JobApprovalEnvelope(
        provider=provider,
        tools=tools,
        mutating_tools=tuple(sorted(spec.permissions.allow_mutating)),
        source_scopes=source_scopes,
        google_accounts=google_accounts,
        workflow_args=spec.workflow.args if spec.workflow is not None else None,
        browser_scope=(
            _canonical_json(browser_scope.model_dump(mode="json"))
            if browser_scope is not None
            else None
        ),
        effect_calls=spec.budget.effect_calls,
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def validate_tool_profile(registry: ToolRegistry, names: list[str]) -> ToolRegistry:
    """Resolve an exact profile and prove every exposed tool is read-only."""

    selected = []
    for name in names:
        tool = registry.get(name)
        if tool is None:
            raise JobConfigurationError(f"job tool is unknown or unavailable: {name}")
        if tool.risk != "read_only":
            raise JobConfigurationError(f"job tool must be read_only; {name} is {tool.risk}")
        selected.append(tool)
    return ToolRegistry(selected)


def validate_recurring_tool_profile(
    registry: ToolRegistry,
    spec: JobSpec,
    *,
    store: JobRunStore,
    task_store: DurableTaskStore | ScopedDurableTaskStore,
    run_id: str,
    dry_run: bool,
    profile_scope: ProfileScope,
    tool_names: tuple[str, ...] | None = None,
) -> ToolRegistry:
    """Prove standing authority and wrap every recurring mutation before model use."""

    selected: list[Tool] = []
    state_guards = StateGuardRegistry([DurableTaskStateGuard(task_store)])
    mutating = set(spec.permissions.allow_mutating)
    exposed_names = tuple(spec.tools.allow) if tool_names is None else tool_names
    for name in exposed_names:
        tool = registry.get(name)
        if tool is None:
            raise JobConfigurationError(f"job tool is unknown or unavailable: {name}")
        metadata = validate_tool_contract(tool, state_guards=state_guards)
        if metadata["unattended"] == "forbidden":
            raise JobConfigurationError(f"job tool is never available unattended: {name}")
        if tool.risk == "read_only":
            selected.append(tool)
            continue
        if name not in mutating:
            raise JobConfigurationError(
                f"recurring mutating tool is missing from allow_mutating: {name}"
            )
        if metadata["effect_kind"] == "ricky_state":
            guard_id = metadata["state_guard_id"]
            if guard_id is None:
                selected.append(tool)
            else:
                selected.append(state_guards.wrap(guard_id, tool))
            continue
        if metadata["effect_kind"] != "external" or not is_guardable(tool):
            raise JobConfigurationError(
                f"recurring external mutation lacks effect identity contract: {name}"
            )
        if dry_run:
            selected.append(tool)
        else:
            selected.append(
                cast(
                    Tool,
                    GuardedEffectTool(
                        tool,
                        store=store,
                        job_name=spec.name,
                        run_id=run_id,
                        profile_scope=profile_scope,
                        effect_budget=spec.budget.effect_calls,
                    ),
                )
            )
    undeclared = mutating - set(exposed_names)
    if undeclared:
        raise JobConfigurationError(
            f"allow_mutating contains unexposed tools: {', '.join(sorted(undeclared))}"
        )
    return ToolRegistry(selected)


def validate_execution_contract_tools(
    registry: ToolRegistry,
    spec: JobSpec,
    *,
    task_store: DurableTaskStore | ScopedDurableTaskStore,
    delegation: DelegatedRun | None = None,
    authorized_mutating_tools: frozenset[str] = frozenset(),
    store: JobRunStore | None = None,
    run_id: str | None = None,
    profile_scope: ProfileScope,
) -> ToolRegistry:
    """Allow reads, task coordination, and contract-authorized effects.

    A compiled contract authorizes its exact mutating tools through standing or
    confirmed capability policy. Capability-specific grants remain an optional
    additional scope. Every ordinary external mutation must implement effect
    identity and is wrapped by the shared ledger before model use.
    """

    from ricky.authority.engine import DelegatedAuthorityError, build_delegated_tools

    delegated_names = delegation.tool_names() if delegation is not None else frozenset()
    unknown_authorized = authorized_mutating_tools - set(spec.tools.allow)
    if unknown_authorized:
        raise JobConfigurationError(
            "contract authorizes mutating tools outside its tool surface: "
            + ", ".join(sorted(unknown_authorized))
        )
    selected: list[Tool] = []
    state_guards = StateGuardRegistry([DurableTaskStateGuard(task_store)])
    ordinary_effects: set[str] = set()
    mutating = set(spec.permissions.allow_mutating)
    for name in spec.tools.allow:
        tool = registry.get(name)
        if tool is None:
            raise JobConfigurationError(f"execution contract tool is unknown: {name}")
        if tool.risk == "read_only":
            selected.append(tool)
            continue
        metadata = validate_tool_contract(tool, state_guards=state_guards)
        if metadata["unattended"] == "forbidden":
            raise JobConfigurationError(f"tool is never available unattended: {name}")
        if metadata["effect_kind"] == "ricky_state" and name in mutating:
            guard_id = metadata["state_guard_id"]
            if guard_id is None:
                selected.append(tool)
            else:
                selected.append(state_guards.wrap(guard_id, tool))
            continue
        if name in delegated_names and name in mutating:
            selected.append(tool)
            continue
        if name in authorized_mutating_tools and name in mutating:
            if metadata["effect_kind"] != "external" or not is_guardable(tool):
                raise JobConfigurationError(
                    f"contract-authorized external mutation lacks effect identity: {name}"
                )
            selected.append(tool)
            ordinary_effects.add(name)
            continue
        raise JobConfigurationError(
            f"execution contract tool is not authorized for unattended use: {name}"
        )
    if mutating - set(spec.tools.allow):
        raise JobConfigurationError("execution contract authorizes an unexposed mutation")
    if delegation is not None:
        if store is None or run_id is None:
            raise JobConfigurationError("a delegated run requires the effect ledger and run id")
        try:
            selected = build_delegated_tools(
                selected,
                delegation,
                jobs=store,
                run_id=run_id,
                effect_budget=spec.budget.effect_calls,
            )
        except DelegatedAuthorityError as exc:
            raise JobConfigurationError(str(exc)) from exc
    if ordinary_effects:
        if store is None or run_id is None:
            raise JobConfigurationError(
                "a contract-authorized effect requires the effect ledger and run id"
            )
        selected = [
            (
                cast(
                    Tool,
                    GuardedEffectTool(
                        tool,
                        store=store,
                        job_name=spec.name,
                        run_id=run_id,
                        profile_scope=profile_scope,
                        effect_budget=spec.budget.effect_calls,
                    ),
                )
                if tool.name in ordinary_effects
                else tool
            )
            for tool in selected
        ]
    return ToolRegistry(selected)


def validate_pinned_execution_runtime(
    runtime: SessionRuntime,
    selection: PinnedExecutionRuntime,
) -> None:
    """Fail before a run if any exact contracted resource has drifted."""

    state_guards = StateGuardRegistry([DurableTaskStateGuard(runtime.durable_tasks)])
    for name, expected in selection.tool_digests.items():
        tool = runtime.capabilities.full_registry.get(name)
        if tool is None:
            raise JobConfigurationError(f"contracted tool is unavailable: {name}")
        if tool_contract_digest(tool) != expected:
            raise JobConfigurationError(f"contracted tool schema changed: {name}")
        metadata = validate_tool_contract(tool, state_guards=state_guards)
        if (
            metadata["effect_kind"] != selection.tool_effect_kinds[name]
            or metadata["unattended"] != selection.tool_unattended[name]
            or metadata["state_guard_id"] != selection.tool_state_guards[name]
        ):
            raise JobConfigurationError(f"contracted tool execution facts changed: {name}")


def _pinned_skill_registry(selection: PinnedExecutionRuntime) -> SkillRegistry:
    skills = []
    for name, raw_bundle in sorted(selection.skill_bundle_paths.items()):
        bundle = Path(raw_bundle).resolve()
        profile, separator, local_name = name.partition("/")
        if not separator:
            raise JobConfigurationError(f"contracted skill identity is not qualified: {name}")
        skill = parse_skill_markdown(
            bundle / "SKILL.md",
            profile=profile,
            bundle_path=bundle,
        )
        if (
            skill.name != local_name
            or skill.qualified_name != name
            or skill.body != selection.skill_instructions[name]
        ):
            raise JobConfigurationError(f"contracted skill snapshot changed: {name}")
        skills.append(skill)
    return SkillRegistry(skills)


def _rebind_skill_tools(registry: ToolRegistry, skills: SkillRegistry) -> ToolRegistry:
    replacements: dict[str, Tool] = {
        "use_skill": cast(Tool, UseSkillTool(skills)),
        "read_skill_resource": cast(Tool, ReadSkillResourceTool(skills)),
    }
    return ToolRegistry([replacements.get(tool.name, tool) for tool in registry.tools()])


def recurring_permission_engine(spec: JobSpec, *, dry_run: bool) -> PermissionEngine:
    rules = [
        PolicyRule(
            tool_name=name,
            decision="deny" if dry_run else "allow",
            reason=(
                "dry-run mechanically denies every mutation"
                if dry_run
                else "exact mutating permission in the validated run specification"
            ),
        )
        for name in spec.permissions.allow_mutating
    ]
    rules.extend(
        PolicyRule(
            tool_name=name,
            decision="allow",
            reason="runner-injected continuity accounting control",
        )
        for name in DISPOSITION_TOOL_NAMES
    )
    return PermissionEngine(
        Policy(
            rules=rules,
            read_only_default="allow",
            mutating_default="deny",
            destructive_default="deny",
        )
    )


def runtime_policy_digest(
    spec: JobSpec,
    settings: RickySettings,
    profile_scope: ProfileScope,
) -> str:
    """Digest the exact non-secret execution revision a schedule pins."""

    selection = settings.resolve_profile_selection(profile_scope, spec.provider, spec.model)
    session = AgentSession.create(
        settings,
        profile_scope=profile_scope,
        provider=selection.provider,
        model=selection.model,
    )
    workflow_digest, resolved_tool_names = _resolved_workflow_revision_inputs(
        spec,
        settings=settings,
        profile_scope=profile_scope,
    )
    browser_scope = _named_job_browser_scope(spec, settings=settings, session=session)
    return _runtime_policy_digest(
        spec,
        session,
        workflow_bundle_digest=workflow_digest,
        resolved_tool_names=resolved_tool_names,
        browser_scope_digest=(browser_scope.digest() if browser_scope is not None else None),
    )


def _runtime_policy_digest(
    spec: JobSpec,
    session: AgentSession,
    *,
    workflow_bundle_digest: str | None = None,
    resolved_tool_names: tuple[str, ...] | None = None,
    browser_scope_digest: str | None = None,
) -> str:
    tool_names = resolved_tool_names or tuple(spec.tools.allow)
    payload = {
        "provider": session.provider,
        "model": session.model,
        "profile_scope": session.profile_scope.model_dump(mode="json"),
        "tools": spec.tools.allow,
        "mutating": spec.permissions.allow_mutating,
        "issued_google_accounts": _issued_google_accounts(session, tool_names),
        "injected_tools": _injected_tool_revision(spec),
        "workflow": (
            {
                "name": spec.workflow.name,
                "args": spec.workflow.args,
                "bundle_digest": workflow_bundle_digest,
            }
            if spec.workflow is not None
            else None
        ),
    }
    if browser_scope_digest is not None:
        payload["browser_scope_digest"] = browser_scope_digest
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resolved_workflow_revision_inputs(
    spec: JobSpec,
    *,
    settings: RickySettings,
    profile_scope: ProfileScope,
) -> tuple[str | None, tuple[str, ...]]:
    loaded = configured_workflow_bundle(
        spec,
        settings=settings,
        profile_scope=profile_scope,
    )
    if loaded is None:
        return None, tuple(spec.tools.allow)
    return workflow_bundle_digest(loaded), workflow_tool_names(loaded.spec)


def _issued_google_accounts(
    session: AgentSession,
    tool_names: tuple[str, ...],
) -> dict[str, str]:
    """Project non-secret issued identities only when resolved tools can use them."""

    if not any(name.startswith(_GOOGLE_ACCOUNT_TOOL_PREFIXES) for name in tool_names):
        return {}
    raw_accounts = session.settings_snapshot.get("google_accounts")
    if not isinstance(raw_accounts, dict):
        return {}
    accounts: dict[str, str] = {}
    for name, raw in raw_accounts.items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            continue
        email = raw.get("email")
        if isinstance(email, str) and email.strip():
            accounts[name] = email.strip()
    return dict(sorted(accounts.items()))


def _injected_tool_revision(spec: JobSpec) -> dict[str, dict[str, str | None]]:
    """Pin harness bookkeeping contracts without making them approval-bearing."""

    if not spec.task_sources and not spec.stream_sources:
        return {}
    revision: dict[str, dict[str, str | None]] = {}
    tool_types = (RecordItemDispositionTool, RecordCandidateDispositionTool)
    for tool_type in tool_types:
        tool = cast(Tool, tool_type)
        try:
            metadata = validate_tool_contract(tool)
        except CapabilityRegistryError as exc:
            raise JobConfigurationError(
                f"internal disposition tool violates its confined Ricky-state contract: "
                f"{tool.name}: {exc}"
            ) from exc
        if (
            metadata["risk"] != "mutating"
            or metadata["effect_kind"] != "ricky_state"
            or metadata["unattended"] != "allowed"
            or metadata["state_guard_id"] is not None
            or metadata["capability_id"] is not None
        ):
            raise JobConfigurationError(
                f"internal disposition tool violates its confined Ricky-state contract: {tool.name}"
            )
        revision[tool.name] = {
            "contract_digest": tool_contract_digest(tool),
            "risk": metadata["risk"],
            "effect_kind": metadata["effect_kind"],
            "unattended": metadata["unattended"],
            "state_guard_id": metadata["state_guard_id"],
        }
    if tuple(sorted(revision)) != tuple(sorted(DISPOSITION_TOOL_NAMES)):
        raise JobConfigurationError("internal disposition tool inventory is inconsistent")
    return revision


def _terminal_outcome(event: TurnFinishedEvent | None) -> tuple[RunOutcome, str | None]:
    if event is None:
        return "failed", "agent loop ended without turn_finished"
    if event.interrupted:
        return "interrupted", "agent turn interrupted"
    if event.error is not None:
        if event.error.startswith("maximum turn iterations exceeded"):
            return "budget_exceeded", event.error[:MAX_RUN_ERROR_CHARS]
        return "failed", event.error[:MAX_RUN_ERROR_CHARS]
    return "succeeded", None


def _workflow_terminal_outcome(status: str | None) -> tuple[RunOutcome, str | None]:
    if status in {"completed", "completed_with_errors"}:
        return "succeeded", None
    if status == "in_doubt":
        return "uncertain", "workflow contains an in-doubt effect; reconciliation required"
    if status == "interrupted":
        return "interrupted", "workflow execution was interrupted"
    if status is None:
        return "failed", "workflow execution ended without a terminal status"
    return "failed", f"workflow ended with status {status}"


def _final_message(session: AgentSession) -> str | None:
    for message in reversed(session.history):
        if message.role != "assistant":
            continue
        text = "".join(part.text for part in message.content if isinstance(part, TextPart))
        if text:
            return text[:MAX_FINAL_MESSAGE_CHARS]
    return None
