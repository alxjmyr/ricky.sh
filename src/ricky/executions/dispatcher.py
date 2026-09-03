"""Application service for durable submissions and bounded worker dispatch."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from ricky.capabilities.policy import policy_digest
from ricky.config import RickySettings, find_project_root
from ricky.durable_tasks.scoped import ScopedDurableTaskStore, ScopedTaskArtifactStore
from ricky.durable_tasks.store import TaskStoreError
from ricky.durable_tasks.types import DurableTask
from ricky.executions.browser import (
    BrowserTransactionChallenge,
    ParkedBrowserApproval,
    ParkedBrowserTransaction,
)
from ricky.executions.contracts import (
    ExecutionContract,
    load_contract_snapshot,
    load_pinned_runtime,
)
from ricky.executions.store import ExecutionFenceError, ExecutionStore
from ricky.executions.types import (
    ExecutionRequest,
    ExecutionStatus,
    is_retryable_execution_status,
)
from ricky.llm import Provider
from ricky.notifications import NotificationService
from ricky.notifications.routes import RoutePolicy
from ricky.notifications.types import CorrelationRef, NotificationRequest
from ricky.owned_operation import run_with_lease_heartbeat
from ricky.profiles import ProfileScope
from ricky.project_scope import ProjectScope
from ricky.protected_values import ResidentProtectedValueRegistry

if TYPE_CHECKING:
    from ricky.authority.engine import DelegatedRun
    from ricky.authority.registry import AuthorityRegistry
    from ricky.authority.store import AuthorityStore
    from ricky.jobs.runner import JobRunner
    from ricky.jobs.types import JobRun

RunnerFactory = Callable[[], "JobRunner"]
ProviderFactory = Callable[[ExecutionRequest], Provider | None]


class ExecutionDispatchError(RuntimeError):
    """A request cannot safely be submitted or dispatched."""


class GrantRevocationError(ExecutionDispatchError):
    """One or more durable revocation mirrors could not be updated."""

    def __init__(self, failed_subsystems: tuple[str, ...]) -> None:
        self.failed_subsystems = failed_subsystems
        super().__init__("delegation revocation incomplete for: " + ", ".join(failed_subsystems))


class _CancellationRequested(RuntimeError):
    """The durable owner observed a request to stop its active run."""


class _ExecutionOwnershipLost(RuntimeError):
    """The dispatcher could not renew its durable execution claim."""


@dataclass(frozen=True)
class _ActiveExecution:
    run_task: asyncio.Task[JobRun]
    owner_task: asyncio.Task[object]


class ExecutionDispatcher:
    """Submit, claim, and run durable fire-and-report attempts."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        project_root: Path | None = None,
        project_scope: ProjectScope | None = None,
        store: ExecutionStore | None = None,
        runner_factory: RunnerFactory | None = None,
        provider_factory: ProviderFactory | None = None,
        notifications: NotificationService | None = None,
        routes: RoutePolicy | None = None,
        authority: AuthorityStore | None = None,
        authority_registry: AuthorityRegistry | None = None,
        protected_value_registry: ResidentProtectedValueRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.project_scope = project_scope or (
            ProjectScope.bound(project_root)
            if project_root is not None
            else ProjectScope.discover()
        )
        self.project_root = self.project_scope.root
        self.store = store or ExecutionStore(settings)
        self._runner_factory = runner_factory
        self._provider_factory = provider_factory
        self.notifications = notifications or NotificationService(settings)
        self.routes = routes or RoutePolicy(settings)
        self._authority: AuthorityStore | None = authority
        self._authority_registry = authority_registry
        self.protected_value_registry = protected_value_registry
        self._active: dict[str, _ActiveExecution] = {}

    async def start_named_job(
        self,
        name: str,
        *,
        notification_route: str,
        request_key: str,
        profile_scope: ProfileScope,
        task_id: str | None = None,
        task_revision: int | None = None,
        source_conversation_id: str | None = None,
        source_message_id: str | None = None,
        project_scope: ProjectScope | None = None,
    ) -> ExecutionRequest:
        await self.routes.validate(notification_route, profile_scope.label())
        scope = project_scope or self.project_scope
        jobs = self._jobs_for_scope(scope, profile_scope)
        loaded = jobs.load(name)
        jobs.snapshot(loaded)
        if (task_id is None) != (task_revision is None):
            raise ValueError("named task linkage requires both task_id and task_revision")
        if task_id is not None:
            await self._validate_task(task_id, task_revision, profile_scope)
        request = ExecutionRequest(
            id=f"execution_{uuid4().hex}",
            kind="named_job",
            status="queued",
            named_job=loaded.resource.qualified,
            job_digest=loaded.digest,
            project_root_ref=str(scope.root) if scope.enabled else None,
            task_id=task_id,
            task_revision=task_revision,
            profile_scope=profile_scope,
            source_conversation_id=source_conversation_id,
            source_message_id=source_message_id,
            notification_route=notification_route,
            request_key=request_key,
            created_at=datetime.now(UTC),
        )
        await self.store.initialize()
        return await self.store.submit(request, scope=profile_scope)

    async def create_contract_execution_request(
        self,
        contract: ExecutionContract,
        *,
        request_key: str,
        grant_id: str | None = None,
    ) -> ExecutionRequest:
        """Queue one new ad hoc request from an already-compiled exact contract."""

        await self.routes.validate(contract.notification_route, contract.profile_scope.label())
        task = await self._validate_task(
            contract.task_id,
            contract.task_revision,
            contract.profile_scope,
        )
        await self.store.initialize()
        stored = await self.store.get_contract(contract.id, scope=contract.profile_scope)
        if stored != contract:
            raise ExecutionDispatchError("execution contract storage does not match submission")
        snapshotted = load_contract_snapshot(self.settings, contract.digest)
        if snapshotted != contract:
            raise ExecutionDispatchError("execution contract snapshot does not match submission")
        self._validate_current_contract(contract)
        if contract.parent_request_id is not None:
            parent = await self.store.get(contract.parent_request_id, scope=contract.profile_scope)
            if parent.kind != "ad_hoc" or not is_retryable_execution_status(parent.status):
                if parent.status == "uncertain":
                    raise ExecutionDispatchError(
                        "uncertain retry parent must be resolved before submission"
                    )
                raise ExecutionDispatchError(
                    "contract retry parent is not a terminal ad hoc request"
                )
            if parent.task_id != contract.task_id or parent.task_revision != contract.task_revision:
                raise ExecutionDispatchError(
                    "contract retry parent belongs to another task revision"
                )
        if grant_id is not None:
            await self._validate_contract_grant_for_submission(grant_id, contract=contract)
        elif any(item.authority_capability is not None for item in contract.capabilities):
            raise ExecutionDispatchError("effectful execution contract requires an active grant")
        request = ExecutionRequest(
            id=f"execution_{uuid4().hex}",
            kind="ad_hoc",
            status="queued",
            goal=contract.goal,
            contract_id=contract.id,
            contract_digest=contract.digest,
            project_root_ref=contract.project_root_ref,
            task_id=task.id,
            task_revision=task.revision,
            profile_scope=contract.profile_scope,
            source_conversation_id=contract.source_conversation_id,
            source_message_id=contract.source_message_ids[-1],
            grant_id=grant_id,
            notification_route=contract.notification_route,
            request_key=request_key,
            parent_request_id=contract.parent_request_id,
            created_at=datetime.now(UTC),
            expires_at=contract.expires_at,
        )
        submitted = await self.store.submit(request, scope=contract.profile_scope)
        if grant_id is not None:
            try:
                await self._authority_store().attach_execution(
                    grant_id, submitted.id, scope=contract.profile_scope
                )
            except Exception:
                with suppress(Exception):
                    await self.store.cancel(submitted.id, scope=contract.profile_scope)
                raise
        return submitted

    async def _validate_contract_grant_for_submission(
        self,
        grant_id: str,
        *,
        contract: ExecutionContract,
    ) -> None:
        store = self._authority_store()
        await store.initialize()
        grant = await store.load_active(grant_id, scope=contract.profile_scope)
        if grant.task_id != contract.task_id or grant.task_revision != contract.task_revision:
            raise ExecutionDispatchError("delegation grant does not match this task revision")
        if grant.profile_scope != contract.profile_scope:
            raise ExecutionDispatchError("delegation grant belongs to another profile scope")
        if grant.contract_id != contract.id or grant.contract_digest != contract.digest:
            raise ExecutionDispatchError("delegation grant pins another execution contract")
        if grant.confirmations != contract.confirmations:
            raise ExecutionDispatchError("delegation grant confirmation evidence drifted")
        if grant.source.principal_id != contract.principal_id or (
            grant.source.conversation_id != contract.source_conversation_id
        ):
            raise ExecutionDispatchError("delegation grant source differs from its contract")
        if grant.source.inbound_message_id not in contract.source_message_ids:
            raise ExecutionDispatchError("delegation grant source is outside its contract")
        runtime_settings = self.settings.resolve_profile_runtime_settings(contract.profile_scope)
        if grant.policy_digest != runtime_settings.authority.digest():
            raise ExecutionDispatchError("authority policy changed after the grant was issued")

    async def revoke_grant(
        self,
        grant_id: str,
        *,
        scope: ProfileScope,
        actor: str,
        reason: str,
    ) -> None:
        """Stop future delegated tool calls; already-confirmed effects are untouched."""

        from ricky.jobs.store import JobRunStore

        async def revoke_authority() -> None:
            store = self._authority_store()
            await store.initialize()
            await store.revoke(grant_id, scope=scope, actor=actor, reason=reason)

        async def revoke_budget() -> None:
            jobs = JobRunStore(self.settings)
            await jobs.initialize()
            await jobs.set_grant_budget_status(grant_id, "revoked", scope=scope)

        operations = ("authority_store", "job_budget")
        joined = asyncio.gather(
            asyncio.create_task(revoke_authority()),
            asyncio.create_task(revoke_budget()),
            return_exceptions=True,
        )
        cancelled: asyncio.CancelledError | None = None
        try:
            results = await asyncio.shield(joined)
        except asyncio.CancelledError as exc:
            cancelled = exc
            results = await joined
        failed = tuple(
            subsystem
            for subsystem, result in zip(operations, results, strict=True)
            if isinstance(result, BaseException)
        )
        if cancelled is not None:
            if failed:
                cancelled.add_note("delegation revocation also failed for: " + ", ".join(failed))
            raise cancelled
        if failed:
            raise GrantRevocationError(failed)

    def _authority_store(self) -> AuthorityStore:
        from ricky.authority.store import AuthorityStore as _Store

        if self._authority is None:
            self._authority = _Store(self.settings)
        return self._authority

    async def cancel_execution_request(
        self, request_id: str, *, scope: ProfileScope
    ) -> ExecutionRequest:
        await self.store.initialize()
        requested = await self.store.cancel(request_id, scope=scope)
        revocation_error: GrantRevocationError | None = None
        if requested.grant_id is not None:
            try:
                await self.revoke_grant(
                    requested.grant_id,
                    scope=scope,
                    actor="execution_cancel",
                    reason=f"execution request {requested.id} was cancelled",
                )
            except GrantRevocationError as exc:
                revocation_error = exc
        active = self._active.get(request_id)
        if requested.status == "cancel_requested" and active is not None:
            if not active.run_task.done():
                active.run_task.cancel()
            with suppress(BaseException):
                await active.owner_task
            current = await self.store.get(request_id, scope=scope)
            if current.status == "cancel_requested":
                current = await self._settle_cancellation(current)
            if revocation_error is not None:
                raise revocation_error
            return current
        if requested.status == "cancelled":
            await self._notify(requested, None)
        if revocation_error is not None:
            raise revocation_error
        return requested

    async def read_execution_request(
        self, request_id: str, *, scope: ProfileScope
    ) -> ExecutionRequest:
        await self.store.initialize()
        return await self.store.get(request_id, scope=scope)

    async def decide_browser_approval(
        self,
        approval_id: str,
        *,
        scope: ProfileScope,
        approve: bool,
        principal_id: str,
        conversation_id: str,
        source_message_id: str,
        code: str,
    ) -> ParkedBrowserApproval:
        """Apply one exact gateway-authenticated approval or denial."""

        await self.store.initialize()
        return await self.store.decide_browser_approval(
            approval_id,
            scope=scope,
            approve=approve,
            principal_id=principal_id,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            code=code,
        )

    async def read_browser_approval(
        self,
        approval_id: str,
        *,
        scope: ProfileScope,
    ) -> ParkedBrowserApproval:
        await self.store.initialize()
        return await self.store.get_browser_approval(approval_id, scope=scope)

    async def notify_browser_approval(
        self,
        challenge: BrowserTransactionChallenge,
        *,
        scope: ProfileScope,
    ) -> None:
        """Send one trusted mobile-readable review with an exact command."""

        approval = challenge.approval
        await self.store.initialize()
        request = await self.store.get(approval.request_id, scope=scope)
        if request.source_conversation_id != approval.conversation_id:
            raise ExecutionDispatchError("browser approval conversation differs from execution")
        profile_label = request.profile_scope.label()
        correlations = [
            CorrelationRef(
                kind="execution_request",
                id=request.id,
                revision=request.claim_fence,
                profile_label=profile_label,
            )
        ]
        if request.task_id is not None:
            correlations.append(
                CorrelationRef(
                    kind="task",
                    id=request.task_id,
                    revision=request.task_revision,
                    profile_label=profile_label,
                )
            )
        if request.run_id is not None:
            correlations.append(
                CorrelationRef(
                    kind="job_run",
                    id=request.run_id,
                    revision=None,
                    profile_label=profile_label,
                )
            )
        await self.notifications.enqueue(
            NotificationRequest(
                id=f"notification_{uuid4().hex}",
                route=request.notification_route,
                title=(
                    "Browser transaction approval required"
                    if isinstance(approval, ParkedBrowserTransaction)
                    else "Protected destination approval required"
                ),
                body=_browser_approval_body(challenge),
                body_format="portable_markdown_v1",
                urgency="attention",
                source_kind=approval.kind,
                source_id=approval.id,
                dedupe_key=(f"approval:{approval.id}:{approval.revision}:{approval.review_digest}"),
                profile_label=profile_label,
                correlations=correlations,
                created_at=datetime.now(UTC),
                expires_at=approval.expires_at,
            ),
            scope=request.profile_scope,
        )

    async def reconcile_browser_transaction(
        self,
        transaction_id: str,
        *,
        scope: ProfileScope,
        disposition: str,
        actor_principal_id: str,
        source_conversation_id: str,
        source_message_id: str,
        note: str,
    ) -> ExecutionRequest:
        if disposition not in {"confirmed_completed", "confirmed_not_completed"}:
            raise ValueError("invalid browser transaction reconciliation disposition")
        await self.store.initialize()
        request, _ = await self.store.attest_browser_transaction(
            transaction_id,
            scope=scope,
            disposition=cast(Any, disposition),
            actor_principal_id=actor_principal_id,
            source_conversation_id=source_conversation_id,
            source_message_id=source_message_id,
            note=note,
        )
        await self._notify(request, None)
        return request

    async def list_execution_requests(
        self,
        *,
        scope: ProfileScope,
        status: ExecutionStatus | None = None,
        limit: int = 50,
    ) -> list[ExecutionRequest]:
        await self.store.initialize()
        return await self.store.list(scope=scope, status=status, limit=limit)

    async def retry_execution_request(
        self, request_id: str, *, scope: ProfileScope
    ) -> ExecutionRequest:
        await self.store.initialize()
        original = await self.store.get(request_id, scope=scope)
        if original.kind == "ad_hoc":
            raise ExecutionDispatchError(
                "ad hoc retries require a new live capability proposal with retry_of set; "
                "expired confirmations are never replayed"
            )
        return await self.store.retry(request_id, scope=scope)

    async def worker_once(
        self, *, scope: ProfileScope, worker_id: str | None = None
    ) -> list[ExecutionRequest]:
        """Recover stale leases, run one bounded claim batch, and return terminal records."""

        await self.store.initialize()
        await self.store.recover_expired(scope=scope)
        await self.project_notifications(scope=scope)
        identity = worker_id or f"worker_{uuid4().hex}"
        claimed = await self.store.claim(
            worker_id=identity,
            scope=scope,
            limit=self.settings.executions.concurrency,
        )
        if not claimed:
            return []
        return list(await asyncio.gather(*(self._dispatch_one(request) for request in claimed)))

    async def project_notifications(self, *, scope: ProfileScope) -> int:
        """Idempotently fill crash gaps without one route starving queue work."""

        from ricky.jobs.store import JobRunStore

        projected = 0
        job_store = JobRunStore(self.settings)
        await self.notifications.store.initialize()
        projected_source_ids = await self.notifications.store.source_ids(
            source_kind="execution", scope=scope
        )
        requests = await self.store.list_for_notification_projection(scope=scope)
        for request in requests:
            if request.id in projected_source_ids:
                continue
            run = None
            if request.run_id is not None:
                with suppress(Exception):
                    run = await job_store.get(request.run_id, scope=request.profile_scope)
            try:
                await self._notify(request, run)
            except Exception:
                continue
            projected += 1
        return projected

    async def worker(self, *, scope: ProfileScope, worker_id: str | None = None) -> None:
        identity = worker_id or f"worker_{uuid4().hex}"
        while True:
            completed = await self.worker_once(scope=scope, worker_id=identity)
            if not completed:
                await asyncio.sleep(self.settings.executions.poll_seconds)

    async def _dispatch_one(self, request: ExecutionRequest) -> ExecutionRequest:
        assert request.claim_token is not None
        token = request.claim_token
        fence = request.claim_fence
        run_id = f"jobrun_{uuid4().hex}"
        run_task: asyncio.Task[JobRun] | None = None
        try:
            contract: ExecutionContract | None = None
            if request.contract_digest is not None:
                contract = await self._load_contract(request)
            task, sections = await self._dispatch_context(request, contract=contract)
            started = await self.store.start(
                request.id,
                scope=request.profile_scope,
                token=token,
                fence=fence,
                run_id=run_id,
            )
            scope = self._scope_for_request(started, contract=contract)
            runner = self._runner_for_scope(scope)
            provider = self._provider_factory(started) if self._provider_factory else None
            if started.kind == "named_job":
                assert started.named_job is not None and started.job_digest is not None
                current = self._jobs_for_scope(scope, started.profile_scope).load(started.named_job)
                if current.digest != started.job_digest:
                    raise ExecutionDispatchError("pinned named job digest changed")
                run_task = asyncio.create_task(
                    runner.run(
                        started.named_job,
                        provider=provider,
                        trigger="execution",
                        trigger_id=started.id,
                        expected_spec_digest=started.job_digest,
                        run_id=run_id,
                        system_sections=sections,
                        profile_scope=started.profile_scope,
                    )
                )
            elif started.contract_digest is not None:
                assert contract is not None
                delegation = await self._load_contract_delegation(started, contract)
                run_task = asyncio.create_task(
                    runner.run_spec(
                        contract.job_spec(),
                        contract.goal,
                        source_digest=contract.digest,
                        provider=provider,
                        run_id=run_id,
                        trigger_id=started.id,
                        system_sections=sections,
                        delegation=delegation,
                        pinned_runtime=load_pinned_runtime(self.settings, contract),
                        profile_scope=started.profile_scope,
                        execution_request=started,
                        browser_scope=contract.browser,
                        browser_principal_id=contract.principal_id,
                    )
                )
            else:
                raise ExecutionDispatchError("ad hoc execution request has no contract")
            owner_task = asyncio.current_task()
            if owner_task is None:  # pragma: no cover - every coroutine has an owner task
                raise RuntimeError("execution dispatch has no owning asyncio task")
            self._active[started.id] = _ActiveExecution(
                run_task=run_task,
                owner_task=owner_task,
            )
            run = await run_with_lease_heartbeat(
                run_task,
                lease=started,
                renew=self._renew_active_execution,
                interval_seconds=self.settings.executions.heartbeat_seconds,
            )
            status, error = _execution_outcome(run)
            current = await self.store.get(started.id, scope=started.profile_scope)
            if current.status == "cancel_requested":
                return await self._settle_cancellation(current, run=run)
            if current.status != "running":
                return current
            terminal = await self.store.finish(
                started.id,
                scope=started.profile_scope,
                token=token,
                fence=fence,
                status=status,
                error=error,
            )
            terminal = await self._update_task(terminal, task, run)
            await self._notify(terminal, run)
            return terminal
        except _CancellationRequested:
            current = await self.store.get(request.id, scope=request.profile_scope)
            if current.status == "cancel_requested":
                return await self._settle_cancellation(current)
            return current
        except _ExecutionOwnershipLost as exc:
            return await self._settle_ownership_loss(request, cause=exc)
        except asyncio.CancelledError:
            if run_task is not None and not run_task.done():
                run_task.cancel()
                with suppress(BaseException):
                    await run_task
            current = await self.store.get(request.id, scope=request.profile_scope)
            if current.status == "cancel_requested":
                return await self._settle_cancellation(current)
            if current.status in {"cancelled", "uncertain"}:
                return current
            if current.status == "running":
                return await self._settle_interruption(
                    current,
                    clean_error="execution worker cancelled",
                    uncertain_error="execution worker cancelled after work became observable",
                )
            raise
        except Exception as exc:
            current = await self.store.get(request.id, scope=request.profile_scope)
            if current.status == "claimed":
                current = await self.store.start(
                    request.id,
                    scope=request.profile_scope,
                    token=token,
                    fence=fence,
                    run_id=run_id,
                )
            if current.status == "cancel_requested":
                return await self._settle_cancellation(current)
            if current.status == "running":
                with suppress(ExecutionFenceError):
                    terminal = await self.store.finish(
                        request.id,
                        scope=request.profile_scope,
                        token=token,
                        fence=fence,
                        status="failed",
                        error=str(exc)[:2_000],
                    )
                    await self._notify(terminal, None)
                    return terminal
            return await self.store.get(request.id, scope=request.profile_scope)
        finally:
            self._active.pop(request.id, None)

    async def _load_contract(self, request: ExecutionRequest) -> ExecutionContract:
        assert request.contract_id is not None and request.contract_digest is not None
        stored = await self.store.get_contract(request.contract_id, scope=request.profile_scope)
        snapshot = load_contract_snapshot(self.settings, request.contract_digest)
        if stored != snapshot:
            raise ExecutionDispatchError("stored execution contract and snapshot disagree")
        if stored.id != request.contract_id or stored.digest != request.contract_digest:
            raise ExecutionDispatchError("execution request pins another contract")
        if (
            stored.task_id != request.task_id
            or stored.task_revision != request.task_revision
            or stored.profile_scope != request.profile_scope
            or stored.source_conversation_id != request.source_conversation_id
            or stored.notification_route != request.notification_route
        ):
            raise ExecutionDispatchError("execution request linkage disagrees with its contract")
        self._validate_current_contract(stored)
        return stored

    def _validate_current_contract(self, contract: ExecutionContract) -> None:
        runtime_settings = self.settings.resolve_profile_runtime_settings(contract.profile_scope)
        now = datetime.now(UTC)
        if contract.expires_at is not None and contract.expires_at <= now:
            raise ExecutionDispatchError("execution contract expired")
        if any(item.expires_at <= now for item in contract.confirmations):
            raise ExecutionDispatchError("execution contract confirmation expired")
        if contract.agent_policy_digest != runtime_settings.agents.ad_hoc_background.digest():
            raise ExecutionDispatchError("background agent policy changed after compilation")
        route = runtime_settings.gateway.routes.get(contract.route_name)
        if route is None:
            raise ExecutionDispatchError("contract gateway route is no longer configured")
        if contract.route_policy_digest != policy_digest(
            runtime_settings.agents.ad_hoc_background, route
        ):
            raise ExecutionDispatchError("gateway route policy changed after compilation")
        if (
            route.provider != contract.provider
            or route.model != contract.model
            or route.profile_scope() != contract.profile_scope
        ):
            raise ExecutionDispatchError("gateway route binding changed after compilation")
        route_project_root = _route_project_root(route.project_root)
        if route_project_root != contract.project_root_ref:
            raise ExecutionDispatchError("gateway route project root changed after compilation")
        if contract.authority_policy_digest != runtime_settings.authority.digest():
            raise ExecutionDispatchError("authority policy changed after compilation")
        selection = runtime_settings.resolve_profile_selection(
            contract.profile_scope, contract.provider, contract.model
        )
        if selection.provider != contract.provider or selection.model != contract.model:
            raise ExecutionDispatchError("contract provider/model selection drifted")

    def _jobs_for_scope(self, scope: ProjectScope, profile_scope: ProfileScope):
        from ricky.jobs.registry import JobRegistry

        return JobRegistry(self.settings, profile_scope=profile_scope)

    def _runner_for_scope(self, scope: ProjectScope):
        if self._runner_factory is not None:
            return self._runner_factory()
        from ricky.jobs.runner import JobRunner

        return JobRunner(
            self.settings,
            project_scope=scope,
            execution_store=self.store,
            protected_value_registry=self.protected_value_registry,
            browser_approval_notifier=lambda challenge, profile_scope: self.notify_browser_approval(
                challenge, scope=profile_scope
            ),
        )

    @staticmethod
    def _scope_for_request(
        request: ExecutionRequest,
        *,
        contract: ExecutionContract | None,
    ) -> ProjectScope:
        root_ref = contract.project_root_ref if contract is not None else request.project_root_ref
        if contract is not None and request.project_root_ref != root_ref:
            raise ExecutionDispatchError("execution request project root differs from its contract")
        return ProjectScope.disabled() if root_ref is None else ProjectScope.bound(Path(root_ref))

    async def _load_contract_delegation(
        self, request: ExecutionRequest, contract: ExecutionContract
    ) -> DelegatedRun | None:
        if request.grant_id is None:
            if any(item.authority_capability is not None for item in contract.capabilities):
                raise ExecutionDispatchError("effectful execution contract has no active grant")
            return None
        from ricky.authority.engine import DelegatedRun
        from ricky.authority.registry import default_authority_registry

        store = self._authority_store()
        await store.initialize()
        grant = await store.load_active(request.grant_id, scope=request.profile_scope)
        if grant.execution_request_id != request.id:
            raise ExecutionDispatchError("delegation grant belongs to another execution request")
        if grant.contract_id != contract.id or grant.contract_digest != contract.digest:
            raise ExecutionDispatchError("grant pins a different execution contract")
        runtime_settings = self.settings.resolve_profile_runtime_settings(request.profile_scope)
        if grant.policy_digest != runtime_settings.authority.digest():
            raise ExecutionDispatchError("authority policy changed after grant issue")
        registry = self._authority_registry or default_authority_registry()
        declared = {
            item.authority_capability
            for item in contract.capabilities
            if item.authority_capability is not None
        }
        if grant.capabilities() != declared:
            raise ExecutionDispatchError("delegation scopes differ from contract authority")
        guardrails = {item.capability_id: item for item in contract.guardrails}
        by_authority = {
            item.authority_capability: item
            for item in contract.capabilities
            if item.authority_capability is not None
        }
        for scope in grant.scopes:
            evaluator = registry.get(scope.capability)
            if evaluator is None:
                raise ExecutionDispatchError(
                    f"no authority evaluator for capability: {scope.capability}"
                )
            capability = by_authority[scope.capability]
            guardrail = guardrails.get(capability.id)
            if guardrail is None or (
                guardrail.schema_id != scope.schema_id
                or guardrail.schema_version != scope.schema_version
                or guardrail.constraints != scope.constraints
            ):
                raise ExecutionDispatchError(
                    f"grant scope differs from contracted guardrail: {capability.id}"
                )
            if evaluator.schema_id != scope.schema_id or (
                evaluator.schema_version != scope.schema_version
            ):
                raise ExecutionDispatchError(
                    f"authority evaluator schema changed for capability: {scope.capability}"
                )
        from ricky.jobs.store import JobRunStore

        jobs = JobRunStore(self.settings)
        await jobs.initialize()
        await jobs.seed_grant_budget(
            grant_id=grant.id,
            scope=grant.profile_scope,
            task_id=grant.task_id,
            effect_limit=grant.effect_call_limit,
            financial_limit_minor=grant.financial_limit_minor,
            currency=grant.currency,
            expires_at=grant.expires_at,
        )
        return DelegatedRun(grant=grant, registry=registry, authority=store)

    async def _renew_active_execution(self, lease: ExecutionRequest) -> ExecutionRequest:
        assert lease.claim_token is not None
        try:
            renewed = await self.store.renew(
                lease.id,
                scope=lease.profile_scope,
                token=lease.claim_token,
                fence=lease.claim_fence,
            )
        except Exception as exc:
            raise _ExecutionOwnershipLost(
                f"execution lease renewal failed: {type(exc).__name__}"
            ) from exc
        if renewed.status == "cancel_requested":
            raise _CancellationRequested("durable execution cancellation requested")
        return renewed

    async def _settle_cancellation(
        self,
        request: ExecutionRequest,
        *,
        run: JobRun | None = None,
    ) -> ExecutionRequest:
        durable_run, observable = await self._durable_run_evidence(request, run=run)
        status: ExecutionStatus = "uncertain" if observable else "cancelled"
        error = (
            "cancellation interrupted observable or ambiguous execution work"
            if observable
            else "execution cancelled before work became observable"
        )
        return await self._finish_interruption(
            request,
            status=status,
            error=error,
            run=durable_run,
        )

    async def _settle_ownership_loss(
        self,
        request: ExecutionRequest,
        *,
        cause: _ExecutionOwnershipLost,
    ) -> ExecutionRequest:
        current = await self.store.get(request.id, scope=request.profile_scope)
        if current.status == "cancel_requested":
            return await self._settle_cancellation(current)
        if current.status != "running":
            return current
        run, observable = await self._durable_run_evidence(current)
        return await self._finish_interruption(
            current,
            status="uncertain" if observable else "failed",
            error=str(cause),
            run=run,
        )

    async def _settle_interruption(
        self,
        request: ExecutionRequest,
        *,
        clean_error: str,
        uncertain_error: str,
    ) -> ExecutionRequest:
        run, observable = await self._durable_run_evidence(request)
        return await self._finish_interruption(
            request,
            status="uncertain" if observable else "cancelled",
            error=uncertain_error if observable else clean_error,
            run=run,
        )

    async def _finish_interruption(
        self,
        request: ExecutionRequest,
        *,
        status: ExecutionStatus,
        error: str,
        run: JobRun | None,
    ) -> ExecutionRequest:
        assert request.claim_token is not None
        try:
            terminal = await self.store.finish(
                request.id,
                scope=request.profile_scope,
                token=request.claim_token,
                fence=request.claim_fence,
                status=status,
                error=error,
            )
        except ExecutionFenceError:
            return await self.store.get(request.id, scope=request.profile_scope)
        await self._notify(terminal, run)
        return terminal

    async def _durable_run_evidence(
        self,
        request: ExecutionRequest,
        *,
        run: JobRun | None = None,
    ) -> tuple[JobRun | None, bool]:
        """Return the run and whether durable evidence crossed the cancel boundary."""

        if request.run_id is None:
            return None, False
        from ricky.jobs.store import JobRunStore

        store = JobRunStore(self.settings)
        try:
            await store.initialize()
        except Exception:
            return None, True
        if run is None:
            try:
                run = await store.get(request.run_id, scope=request.profile_scope)
            except Exception:
                return None, True
        try:
            actions = await store.actions_for_run(request.run_id, scope=request.profile_scope)
        except Exception:
            return run, True
        if any(action.status in {"reserved", "performed", "in_doubt"} for action in actions):
            return run, True
        if (
            run.final_message
            or run.completion_tokens > 0
            or run.outcome
            in {
                "succeeded",
                "uncertain",
                "approval_required",
            }
        ):
            return run, True
        if run.transcript_path is None:
            return run, False
        try:
            observable = await asyncio.to_thread(
                _transcript_has_observable_work, Path(run.transcript_path)
            )
        except OSError:
            return run, True
        return run, observable

    async def _dispatch_context(
        self,
        request: ExecutionRequest,
        *,
        contract: ExecutionContract | None = None,
    ) -> tuple[DurableTask | None, dict[str, str]]:
        if request.task_id is None:
            return None, {
                "execution": f"Execution request: {request.id}\nDo not infer foreground history."
            }
        assert request.task_revision is not None
        task = await self._validate_task(
            request.task_id, request.task_revision, request.profile_scope
        )
        store = await ScopedDurableTaskStore.create(self.settings, scope=request.profile_scope)
        artifacts = ScopedTaskArtifactStore(store)
        artifact_enabled = contract is None or any(
            item.kind == "task_artifacts" and item.enabled for item in contract.context_sources
        )
        entries = await artifacts.list(task.id) if artifact_enabled else []
        artifact_text: list[str] = []
        remaining = 20_000
        for entry in sorted(entries, key=lambda item: item.path)[:5]:
            if remaining <= 0:
                break
            read = await artifacts.read(task.id, entry.path, line_count=200)
            content = read.content[:remaining]
            artifact_text.append(f"## {entry.path}\n{content}")
            remaining -= len(content)
        payload = task.model_dump(mode="json")
        payload["lease"] = None
        sections = {
            "execution": (
                f"Execution request: {request.id}\n"
                "This is a fresh background session. No foreground conversation history is present."
            ),
            "linked_durable_task": json.dumps(payload, sort_keys=True),
        }
        if artifact_text:
            sections["selected_task_artifacts"] = "\n\n".join(artifact_text)
        return task, sections

    async def _validate_task(
        self, task_id: str, revision: int | None, scope: ProfileScope
    ) -> DurableTask:
        store = await ScopedDurableTaskStore.create(self.settings, scope=scope)
        task = await store.get_task(task_id)
        if revision is not None and task.revision != revision:
            raise ExecutionDispatchError(
                f"durable task revision changed: expected {revision}, found {task.revision}"
            )
        if task.status in {"completed", "cancelled"}:
            raise ExecutionDispatchError("durable task is closed")
        if task.execution_mode == "user":
            raise ExecutionDispatchError("user-owned durable tasks cannot be dispatched")
        if task.execution_mode == "joint" and task.waiting_on == "user":
            raise ExecutionDispatchError("durable task baton currently belongs to the user")
        return task

    async def _update_task(
        self, request: ExecutionRequest, original: DurableTask | None, run: JobRun
    ) -> ExecutionRequest:
        if original is None or request.task_id is None:
            return request
        store = await ScopedDurableTaskStore.create(self.settings, scope=request.profile_scope)
        current = await store.get_task(request.task_id)
        if current.status in {"completed", "cancelled"} or current.execution_mode == "user":
            return request
        holder = f"execution:{request.id}"
        try:
            claimed = await store.claim(
                current.id,
                holder_session_id=holder,
                expected_revision=current.revision,
                authority="agent_autonomy",
                executor_id=request.id,
            )
            assert claimed.lease is not None
            summary = _bounded_result(run, self.settings.executions.result_text_limit)
            if request.status == "succeeded":
                updated = await store.progress(
                    claimed.id,
                    lease=claimed.lease,
                    expected_revision=claimed.revision,
                    current_summary=summary,
                    next_action="Review the background execution result",
                    authority="agent_autonomy",
                    executor_id=request.id,
                )
            else:
                updated = await store.block(
                    claimed.id,
                    lease=claimed.lease,
                    expected_revision=claimed.revision,
                    current_summary=summary,
                    next_action="Review the failed background execution",
                    authority="agent_autonomy",
                    executor_id=request.id,
                )
            if updated.lease is not None:
                await store.release(
                    updated.id,
                    lease=updated.lease,
                    expected_revision=updated.revision,
                    authority="agent_autonomy",
                    executor_id=request.id,
                    summary="Background execution task update complete",
                )
        except TaskStoreError:
            await store.release_session_leases(holder)
        return request

    async def _notify(self, request: ExecutionRequest, run: JobRun | None) -> None:
        profile_label = request.profile_scope.label()
        summary = (
            _bounded_result(run, self.settings.executions.result_text_limit)
            if run is not None
            else (request.error or f"Execution {request.status}")
        )
        correlations = [
            CorrelationRef(
                kind="execution_request",
                id=request.id,
                revision=request.claim_fence,
                profile_label=profile_label,
            )
        ]
        if request.task_id is not None:
            correlations.append(
                CorrelationRef(
                    kind="task",
                    id=request.task_id,
                    revision=request.task_revision,
                    profile_label=profile_label,
                )
            )
        if request.run_id is not None:
            correlations.append(
                CorrelationRef(
                    kind="job_run",
                    id=request.run_id,
                    revision=None,
                    profile_label=profile_label,
                )
            )
        await self.notifications.enqueue(
            NotificationRequest(
                id=f"notification_{uuid4().hex}",
                route=request.notification_route,
                title=f"Execution {request.status}",
                body=summary,
                body_format="portable_markdown_v1",
                urgency="normal" if request.status == "succeeded" else "attention",
                source_kind="execution",
                source_id=request.id,
                dedupe_key=f"result:{request.status}",
                profile_label=profile_label,
                correlations=correlations,
                created_at=datetime.now(UTC),
            ),
            scope=request.profile_scope,
        )


def _execution_outcome(run: JobRun) -> tuple[ExecutionStatus, str | None]:
    if run.outcome == "succeeded":
        return "succeeded", None
    if run.outcome == "approval_required":
        return "blocked", run.error or "background execution requires attention"
    if run.outcome == "interrupted":
        return "uncertain", run.error or "background execution was interrupted"
    if run.outcome == "uncertain":
        return "uncertain", run.error or "external effect outcome is uncertain"
    return "failed", run.error or f"job run ended with {run.outcome}"


def _bounded_result(run: JobRun, limit: int) -> str:
    if run.outcome == "succeeded":
        text = run.final_message or f"Job run {run.id} succeeded"
    else:
        text = run.error or f"Job run {run.id} ended with {run.outcome}"
    return text[:limit]


def _browser_approval_body(challenge: BrowserTransactionChallenge) -> str:
    approval = challenge.approval
    binding = approval.binding
    lines = [
        "Action required: approve or cancel this exact live browser occurrence.",
        f"Approval id: `{approval.id}`",
        f"Expires: {approval.expires_at.isoformat()}",
    ]
    if isinstance(approval, ParkedBrowserTransaction):
        assert binding.resource_digest is not None
        assert binding.resource_kind is not None
        assert binding.provider is not None
        assert binding.session_digest is not None
        assert binding.page_digest is not None
        assert binding.budget_ceiling is not None
        if binding.resource is None:
            lines.append(f"Browser resource: ephemeral (`sha256:{binding.resource_digest}`)")
        else:
            lines.append(
                f"Browser resource: `{binding.resource.qualified}` ({binding.resource_kind}, "
                f"identity sha256 `{binding.resource_digest}`)"
            )
        if binding.resource_configuration_digest is not None:
            lines.append(
                f"Browser resource configuration sha256: `{binding.resource_configuration_digest}`"
            )
        lines.extend(
            [
                f"Pinned model provider: `{binding.provider}`",
                f"Session occurrence sha256: `{binding.session_digest}`",
                f"Page occurrence sha256: `{binding.page_digest}`",
                f"Page generation: {binding.page_generation}",
                f"Snapshot occurrence sha256: `{binding.snapshot_digest}`",
                f"Target occurrence sha256: `{binding.target_digest}`",
                f"Live occurrence sha256: `{binding.occurrence_digest}`",
                "Browser execution budget ceiling:",
                "```json",
                binding.budget_ceiling.model_dump_json(indent=2),
                "```",
            ]
        )
    lines.extend(
        [
            f"Top-level origin: `{binding.top_level_origin}`",
            f"Target-frame origin: `{binding.target_frame_origin}`",
            f"Target: {binding.target_description}",
        ]
    )
    if binding.destination_projections:
        lines.append("Known destinations:")
        lines.extend(f"- `{item}`" for item in binding.destination_projections)
    if isinstance(approval, ParkedBrowserTransaction):
        lines.extend(
            [
                (
                    "Commit target: last-resort visual coordinate fallback"
                    if approval.target_mode == "coordinate"
                    else "Commit target: semantic browser target"
                ),
                "Proposed transaction details (derived from untrusted page/model content):",
                "```json",
                approval.envelope.model_dump_json(indent=2),
                "```",
            ]
        )
        if approval.coordinate is not None:
            coordinate = approval.coordinate
            lines.extend(
                [
                    f"Masked screenshot disclosed to: `{binding.provider}`",
                    f"Coordinate fallback reason: `{coordinate.reason}`",
                    (f"Exact CSS coordinate: ({coordinate.x}, {coordinate.y})"),
                    (
                        "Viewport (CSS pixels): "
                        f"{coordinate.viewport_width} × {coordinate.viewport_height}"
                    ),
                    f"Scroll position (CSS pixels): ({coordinate.scroll_x}, {coordinate.scroll_y})",
                    (f"Image pixels per CSS pixel: {coordinate.coordinate_scale}"),
                    f"Masked image sha256: `{coordinate.masked_image_digest}`",
                    (f"Visual snapshot occurrence sha256: `{coordinate.visual_snapshot_digest}`"),
                    (f"Semantic resolution sha256: `{coordinate.semantic_resolution_digest}`"),
                    (f"Nested hit-target sha256: `{coordinate.nested_hit_target_digest}`"),
                ]
            )
        if approval.protected_uses:
            lines.append("Protected values used (aliases only):")
            lines.extend(
                f"- `{item.resource.qualified}` revision {item.revision}, field `{item.field}`"
                for item in approval.protected_uses
            )
        if approval.attachments:
            lines.append("Submitted task artifacts:")
            lines.extend(
                f"- `{item.id}` ({item.byte_count} bytes, sha256 {item.sha256})"
                for item in approval.attachments
            )
    else:
        lines.extend(
            [
                "Protected destination authorization for this execution only:",
                (
                    f"- `{approval.protected_use.resource.qualified}` revision "
                    f"{approval.protected_use.revision}, field "
                    f"`{approval.protected_use.field}`"
                ),
                "This does not create a durable protected-destination approval.",
            ]
        )
    lines.extend(
        [
            "",
            f"Approve: `/approve {approval.id} {challenge.code}`",
            f"Deny: `/deny {approval.id} {challenge.code}`",
            f"Cancel execution: `/cancel {approval.request_id}`",
        ]
    )
    return "\n".join(lines)


def _route_project_root(configured_root: str | None) -> str | None:
    if configured_root is None:
        return None
    configured = Path(configured_root).expanduser()
    if not configured.is_absolute():
        configured = find_project_root() / configured
    return str(configured.resolve())


def _transcript_has_observable_work(path: Path) -> bool:
    observable_kinds = {
        "text_delta",
        "thinking_delta",
        "llm_response_finished",
        "tool_call_requested",
        "tool_call_started",
        "tool_call_finished",
    }
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                kind = json.loads(line).get("kind")
            except (json.JSONDecodeError, AttributeError):
                return True
            if kind in observable_kinds:
                return True
    return False
