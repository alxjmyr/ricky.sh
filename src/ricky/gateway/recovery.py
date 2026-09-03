"""Deterministic startup recovery for interrupted gateway work.

Recovery never asks a model whether an external effect occurred. Every rule maps
one durable record to exactly one terminal state, using only evidence the store
already holds:

* work with no observable attempt is safe to reclaim;
* work that began but cannot be observed becomes ``uncertain`` or ``in_doubt``;
* a stale worker can never commit after recovery, because either the fence
  advanced or the record left the state that worker requires.

``inspect`` is read-only and is the default operator behaviour. ``apply``
performs exactly the planned actions and is idempotent: running it twice leaves
the same durable state, because every write is conditional on the record still
being in its pre-recovery state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ricky.config import RickySettings
from ricky.executions.store import ExecutionNotFoundError, ExecutionStore
from ricky.gateway.store import GatewayStore
from ricky.jobs.browser_store import BrowserRunLedger
from ricky.jobs.lock import browser_worker_is_alive
from ricky.jobs.store import JobRunStore
from ricky.messaging.store import MessagingStore
from ricky.notifications.store import NotificationStore
from ricky.profiles import ProfileScope
from ricky.sessions.store import SessionStore

RecoverySubsystem = Literal[
    "inbox",
    "poller",
    "foreground_turn",
    "session",
    "execution_draft",
    "execution_contract",
    "execution",
    "browser_attempt",
    "outbox",
    "effect",
]
RecoveryDisposition = Literal[
    "reclaimed",
    "uncertain",
    "in_doubt",
    "released",
    "expired",
    "requires_review",
    "failed",
]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RecoveryAction(_FrozenModel):
    """One deterministic transition recovery will make, or has made."""

    subsystem: RecoverySubsystem
    record_id: str = Field(min_length=1, max_length=500)
    from_state: str = Field(min_length=1, max_length=100)
    to_state: str = Field(min_length=1, max_length=100)
    disposition: RecoveryDisposition
    reason: str = Field(min_length=1, max_length=500)
    applied: bool = False


class RecoveryPlan(_FrozenModel):
    """The complete set of transitions for one recovery pass."""

    generated_at: datetime
    applied: bool
    actions: tuple[RecoveryAction, ...] = ()
    failures: tuple[str, ...] = ()

    @field_validator("generated_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("generated_at must be timezone-aware UTC")
        return value

    def by_subsystem(self, subsystem: RecoverySubsystem) -> tuple[RecoveryAction, ...]:
        """Return every planned action for one subsystem."""

        return tuple(action for action in self.actions if action.subsystem == subsystem)


_INBOX_INTERRUPTED = "foreground turn had already started; its output cannot be observed"
_INBOX_SAFE = "claim expired with no started turn"
_POLLER_SAFE = "poller lease expired; long poll writes nothing until it commits a batch"
_SESSION_INTERRUPTED = "session lease expired while a turn was running"
_SESSION_SAFE = "session lease expired with no running turn"
_OUTBOX_SAFE = "delivery claim expired before any transport part was prepared"
_OUTBOX_AMBIGUOUS = "delivery claim expired after transport parts were prepared"
_EFFECT_AMBIGUOUS = "external effect was reserved but never resolved"
_EXECUTION_STARTED = "worker lease expired after the run started"
_EXECUTION_SAFE = "expired pre-run claim safely requeued"


class GatewayRecovery:
    """Inspect and repair every interrupted record for one user data root."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        scope: ProfileScope,
        messaging: MessagingStore | None = None,
        notifications: NotificationStore | None = None,
        gateway: GatewayStore | None = None,
        sessions: SessionStore | None = None,
        executions: ExecutionStore | None = None,
        jobs: JobRunStore | None = None,
        browser_ledger: BrowserRunLedger | None = None,
    ) -> None:
        self.settings = settings
        self.profile_scope = scope
        self.messaging = messaging or MessagingStore(settings)
        self.notifications = notifications or NotificationStore(settings)
        self.gateway = gateway or GatewayStore(settings)
        self.sessions = sessions or SessionStore(settings)
        self.executions = executions or ExecutionStore(settings)
        self.jobs = jobs or JobRunStore(settings)
        self.browser_ledger = browser_ledger or BrowserRunLedger(settings)

    async def inspect(self, *, now: datetime | None = None) -> RecoveryPlan:
        """Report every transition recovery would make. Changes no state."""

        return await self._run(apply_changes=False, now=now)

    async def apply(self, *, now: datetime | None = None) -> RecoveryPlan:
        """Perform exactly the planned transitions. Safe to run repeatedly."""

        return await self._run(apply_changes=True, now=now)

    async def recover_browser_attempts(
        self,
        *,
        now: datetime | None = None,
    ) -> tuple[RecoveryAction, ...]:
        """Apply the live browser-owner recovery rule during gateway maintenance."""

        await self.executions.initialize()
        await self.browser_ledger.initialize()
        return tuple(await self._recover_browser_attempts(True, now or datetime.now(UTC)))

    async def _run(self, *, apply_changes: bool, now: datetime | None) -> RecoveryPlan:
        moment = now or datetime.now(UTC)
        await self._initialize()
        actions: list[RecoveryAction] = []
        failures: list[str] = []
        for step in (
            self._recover_inbox,
            self._recover_pollers,
            self._recover_foreground_turns,
            self._recover_sessions,
            self._recover_execution_drafts,
            self._recover_execution_contracts,
            self._recover_executions,
            self._recover_browser_attempts,
            self._recover_outbox,
            self._recover_effects,
        ):
            try:
                actions.extend(await step(apply_changes, moment))
            except Exception as exc:  # noqa: BLE001 - one broken subsystem must not hide others
                failures.append(f"{step.__name__.removeprefix('_recover_')}: {exc}")
        return RecoveryPlan(
            generated_at=moment,
            applied=apply_changes,
            actions=tuple(actions),
            failures=tuple(failures),
        )

    async def _initialize(self) -> None:
        await self.messaging.initialize()
        await self.notifications.initialize()
        await self.gateway.initialize()
        await self.sessions.initialize()
        await self.executions.initialize()
        await self.jobs.initialize()
        await self.browser_ledger.initialize()

    async def _recover_inbox(self, apply_changes: bool, now: datetime) -> list[RecoveryAction]:
        """Rule: a claimed message is uncertain only when its turn had started."""

        actions: list[RecoveryAction] = []
        for stale in await self.messaging.stale_inbox_claims(now=now):
            result = await self.gateway.result_for_message(
                stale.message.id,
                scope=self.profile_scope,
            )
            started = result is not None and result.status == "running"
            terminal = result is not None and result.status != "running"
            target: Literal["pending", "processed", "uncertain"]
            if result is not None and result.status in {"committed", "failed"}:
                target = "processed"
            elif result is not None:
                target = "uncertain"
            else:
                target = "pending"
            actions.append(
                RecoveryAction(
                    subsystem="inbox",
                    record_id=stale.message.id,
                    from_state="claimed",
                    to_state=target,
                    disposition="uncertain" if target == "uncertain" else "reclaimed",
                    reason=(
                        "terminal gateway result authoritatively settled the inbox"
                        if terminal
                        else (_INBOX_INTERRUPTED if started else _INBOX_SAFE)
                    ),
                    applied=apply_changes,
                )
            )
            if apply_changes:
                if terminal:
                    settled_status: Literal["processed", "uncertain"] = (
                        "processed" if target == "processed" else "uncertain"
                    )
                    await self.messaging.settle_inbox_from_terminal_result(
                        stale.message.id,
                        status=settled_status,
                        now=now,
                    )
                else:
                    reclaim_status: Literal["pending", "uncertain"] = (
                        "uncertain" if target == "uncertain" else "pending"
                    )
                    await self.messaging.recover_inbox_claim(
                        stale.message.id, status=reclaim_status, now=now
                    )
        return actions

    async def _recover_pollers(self, apply_changes: bool, now: datetime) -> list[RecoveryAction]:
        """Rule: a poll that never committed a batch left no durable evidence."""

        actions: list[RecoveryAction] = []
        for stale in await self.messaging.stale_poller_leases(now=now):
            actions.append(
                RecoveryAction(
                    subsystem="poller",
                    record_id=f"{stale.transport}:{stale.account}",
                    from_state="leased",
                    to_state="free",
                    disposition="released",
                    reason=_POLLER_SAFE,
                    applied=apply_changes,
                )
            )
            if apply_changes:
                await self.messaging.clear_poller_lease(stale.transport, stale.account)
        return actions

    async def _recover_foreground_turns(
        self, apply_changes: bool, now: datetime
    ) -> list[RecoveryAction]:
        """Rule: a running turn owns unobservable output, so it becomes uncertain."""

        actions: list[RecoveryAction] = []
        for result in await self.gateway.running_results(scope=self.profile_scope):
            actions.append(
                RecoveryAction(
                    subsystem="foreground_turn",
                    record_id=result.message_id,
                    from_state="running",
                    to_state="uncertain",
                    disposition="uncertain",
                    reason=_INBOX_INTERRUPTED,
                    applied=apply_changes,
                )
            )
            if apply_changes:
                await self.gateway.recover_running_result(
                    result.message_id,
                    scope=self.profile_scope,
                    error=_INBOX_INTERRUPTED,
                )
        return actions

    async def _recover_sessions(self, apply_changes: bool, now: datetime) -> list[RecoveryAction]:
        """Rule: release an expired lease; its fence advances so no stale commit lands."""

        actions: list[RecoveryAction] = []
        for stale in await self.sessions.stale_leases(scope=self.profile_scope, now=now):
            interrupted = bool(stale.running_turn_ids)
            actions.append(
                RecoveryAction(
                    subsystem="session",
                    record_id=stale.session_id,
                    from_state="leased",
                    to_state="uncertain" if interrupted else "released",
                    disposition="uncertain" if interrupted else "released",
                    reason=_SESSION_INTERRUPTED if interrupted else _SESSION_SAFE,
                    applied=apply_changes,
                )
            )
            if apply_changes:
                await self.sessions.recover_expired_lease(
                    stale.session_id,
                    scope=self.profile_scope,
                    error=_SESSION_INTERRUPTED if interrupted else _SESSION_SAFE,
                    now=now,
                )
        return actions

    async def _recover_executions(self, apply_changes: bool, now: datetime) -> list[RecoveryAction]:
        """Rule: a claim with a recorded run start is uncertain; a pre-run claim requeues."""

        actions: list[RecoveryAction] = []
        expired = await self.executions.list(
            scope=self.profile_scope, status="claimed", limit=1_000
        )
        running = await self.executions.list(
            scope=self.profile_scope, status="running", limit=1_000
        )
        awaiting_protected = await self.executions.list(
            scope=self.profile_scope,
            status="awaiting_protected_approval",
            limit=1_000,
        )
        awaiting_transaction = await self.executions.list(
            scope=self.profile_scope,
            status="awaiting_transaction_approval",
            limit=1_000,
        )
        cancelling = await self.executions.list(
            scope=self.profile_scope, status="cancel_requested", limit=1_000
        )
        for request in expired + running + awaiting_protected + awaiting_transaction + cancelling:
            if request.claim_expires_at is None or request.claim_expires_at > now:
                continue
            parked = request.status in {
                "awaiting_protected_approval",
                "awaiting_transaction_approval",
            }
            started = request.status in {"running", "cancel_requested"}
            actions.append(
                RecoveryAction(
                    subsystem="execution",
                    record_id=request.id,
                    from_state=request.status,
                    to_state="blocked" if parked else "uncertain" if started else "queued",
                    disposition=(
                        "requires_review" if parked else "uncertain" if started else "reclaimed"
                    ),
                    reason=(
                        "live browser approval invalidated after owner lease expired"
                        if parked
                        else _EXECUTION_STARTED
                        if started
                        else _EXECUTION_SAFE
                    ),
                    applied=apply_changes,
                )
            )
        if apply_changes and actions:
            await self.executions.recover_expired(scope=self.profile_scope, now=now)
        return actions

    async def _recover_execution_drafts(
        self, apply_changes: bool, now: datetime
    ) -> list[RecoveryAction]:
        """Expire stale evidence; retain live drafts for source-bound continuation."""

        actions: list[RecoveryAction] = []
        open_states = {"collecting_guardrails", "awaiting_confirmation", "ready"}
        drafts = await self.executions.list_drafts(scope=self.profile_scope, limit=1_000)
        for draft in drafts:
            if draft.target == "gateway_foreground" and draft.status == "executing":
                actions.append(
                    RecoveryAction(
                        subsystem="execution_draft",
                        record_id=draft.id,
                        from_state="executing",
                        to_state="uncertain",
                        disposition="uncertain",
                        reason=(
                            "gateway stopped after reserving an exact foreground call; "
                            "replay is forbidden"
                        ),
                        applied=apply_changes,
                    )
                )
                continue
            if draft.status not in open_states:
                continue
            expired = draft.expires_at <= now
            actions.append(
                RecoveryAction(
                    subsystem="execution_draft",
                    record_id=draft.id,
                    from_state=draft.status,
                    to_state="expired" if expired else draft.status,
                    disposition="expired" if expired else "requires_review",
                    reason=(
                        "live review evidence expired and cannot authorize execution"
                        if expired
                        else "durable draft is resumable only from a new authenticated message"
                    ),
                    applied=apply_changes and expired,
                )
            )
        if apply_changes:
            await self.executions.expire_drafts(scope=self.profile_scope, now=now)
            await self.executions.recover_interrupted_foreground_drafts(
                scope=self.profile_scope,
                now=now,
            )
        return actions

    async def _recover_browser_attempts(
        self,
        apply_changes: bool,
        now: datetime,
    ) -> list[RecoveryAction]:
        """Invalidate live browser owners whose exact execution claim was lost."""

        actions: list[RecoveryAction] = []
        live_statuses = {
            "claimed",
            "running",
            "awaiting_protected_approval",
            "awaiting_transaction_approval",
            "cancel_requested",
        }
        for attempt in await self.browser_ledger.active_attempts(scope=self.profile_scope):
            request_id = attempt.execution_request_id
            reason: str | None = None
            if request_id is None:
                run = await self.jobs.get(attempt.run_id, scope=self.profile_scope)
                if run.outcome is not None:
                    reason = f"browser owner was lost after named job became {run.outcome}"
                elif (
                    browser_worker_is_alive(
                        self.browser_ledger.root,
                        attempt.worker_id,
                    )
                    is False
                ):
                    reason = "named-job browser worker process exited without cleanup"
            else:
                try:
                    request = await self.executions.get(request_id, scope=self.profile_scope)
                except ExecutionNotFoundError:
                    reason = "browser attempt references a missing execution request"
                else:
                    if (
                        request.run_id != attempt.run_id
                        or request.claim_fence != attempt.claim_fence
                    ):
                        reason = "browser attempt differs from the current execution occurrence"
                    elif request.status not in live_statuses:
                        reason = f"browser owner was lost after execution became {request.status}"
                    elif request.claim_expires_at is None or request.claim_expires_at <= now:
                        reason = (
                            "browser owner lease expired and its live process cannot be resumed"
                        )
            if reason is None:
                continue
            ambiguous = await self.browser_ledger.has_ambiguous_effect_evidence(
                attempt.id,
                scope=self.profile_scope,
            )
            target = "in_doubt" if ambiguous else "failed"
            actions.append(
                RecoveryAction(
                    subsystem="browser_attempt",
                    record_id=attempt.id,
                    from_state=attempt.status,
                    to_state=target,
                    disposition="in_doubt" if ambiguous else "failed",
                    reason=reason,
                    applied=apply_changes,
                )
            )
            if apply_changes:
                await self.browser_ledger.recover_lost_attempt(
                    attempt.id,
                    scope=self.profile_scope,
                    reason=reason,
                    now=now,
                )
        return actions

    async def _recover_execution_contracts(
        self, apply_changes: bool, now: datetime
    ) -> list[RecoveryAction]:
        """Expose committed contracts whose request submission never committed."""

        del apply_changes, now
        actions: list[RecoveryAction] = []
        for contract in await self.executions.list_contracts(
            scope=self.profile_scope,
            limit=1_000,
        ):
            if (
                await self.executions.request_for_contract(
                    contract.id,
                    scope=self.profile_scope,
                )
                is not None
            ):
                continue
            actions.append(
                RecoveryAction(
                    subsystem="execution_contract",
                    record_id=contract.id,
                    from_state="compiled",
                    to_state="awaiting_submission_review",
                    disposition="requires_review",
                    reason=(
                        "immutable contract committed without a request; recovery will not "
                        "guess whether foreground submission was intended"
                    ),
                    applied=False,
                )
            )
        return actions

    async def _recover_outbox(self, apply_changes: bool, now: datetime) -> list[RecoveryAction]:
        """Rule: a send is in doubt once a transport part exists for the claimed fence."""

        actions: list[RecoveryAction] = []
        for entry in await self.notifications.stale_claims(
            scope=self.profile_scope,
            now=now,
        ):
            parts = await self.messaging.delivery_parts(entry.id)
            attempted = any(part.fence == entry.fence for part in parts)
            target: Literal["pending", "in_doubt"] = "in_doubt" if attempted else "pending"
            actions.append(
                RecoveryAction(
                    subsystem="outbox",
                    record_id=entry.id,
                    from_state="claimed",
                    to_state=target,
                    disposition="in_doubt" if attempted else "reclaimed",
                    reason=_OUTBOX_AMBIGUOUS if attempted else _OUTBOX_SAFE,
                    applied=apply_changes,
                )
            )
            if apply_changes:
                await self.notifications.recover_claim(
                    entry.id,
                    scope=self.profile_scope,
                    disposition=target,
                    error=_OUTBOX_AMBIGUOUS if attempted else _OUTBOX_SAFE,
                    now=now,
                )
        return actions

    async def _recover_effects(self, apply_changes: bool, now: datetime) -> list[RecoveryAction]:
        """Rule: an unresolved reservation is ambiguous and needs a human."""

        actions: list[RecoveryAction] = []
        for action in await self.jobs.reserved_actions(scope=self.profile_scope):
            actions.append(
                RecoveryAction(
                    subsystem="effect",
                    record_id=action.id,
                    from_state="reserved",
                    to_state="in_doubt",
                    disposition="in_doubt",
                    reason=_EFFECT_AMBIGUOUS,
                    applied=apply_changes,
                )
            )
            if apply_changes:
                await self.jobs.strand_reserved_action(
                    action.id,
                    scope=self.profile_scope,
                    error=_EFFECT_AMBIGUOUS,
                )
        return actions
