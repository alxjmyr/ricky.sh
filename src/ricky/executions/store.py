"""SQLite persistence and fenced state machine for durable executions."""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast
from uuid import uuid4

from pydantic import TypeAdapter

from ricky.config import RickySettings, user_data_path
from ricky.executions.browser import (
    BrowserApprovalDraft,
    BrowserTransactionAttestation,
    BrowserTransactionChallenge,
    ParkedBrowserApproval,
    ParkedBrowserTransaction,
    challenge_digest,
    issue_parked_approval,
)
from ricky.executions.contracts import ConfirmationRef, ExecutionContract
from ricky.executions.drafts import (
    DraftActivityKind,
    DraftStatus,
    ExecutionDraft,
    ExecutionDraftActivity,
)
from ricky.executions.types import (
    ExecutionActivity,
    ExecutionActivityKind,
    ExecutionRequest,
    ExecutionResolution,
    ExecutionResolutionDisposition,
    ExecutionStatus,
    is_retryable_execution_status,
    validate_execution_id,
)
from ricky.profiles import ProfileScope

SCHEMA_VERSION = 8
_TERMINAL = {"succeeded", "failed", "blocked", "cancelled", "uncertain"}
_ACTIVE = {
    "claimed",
    "running",
    "awaiting_protected_approval",
    "awaiting_transaction_approval",
    "cancel_requested",
}
_PARKED = {"awaiting_protected_approval", "awaiting_transaction_approval"}
_APPROVAL_ADAPTER = TypeAdapter(ParkedBrowserApproval)


class _ProfileScoped(Protocol):
    profile_scope: ProfileScope


class ExecutionStoreError(RuntimeError):
    """The durable execution store rejected an operation."""


class ExecutionNotFoundError(ExecutionStoreError):
    """The requested execution does not exist."""


class ExecutionFenceError(ExecutionStoreError):
    """A stale or invalid worker attempted a state transition."""


class ExecutionDraftFenceError(ExecutionStoreError):
    """A stale foreground turn attempted to mutate a newer execution draft."""


class BrowserApprovalError(ExecutionStoreError):
    """An exact parked browser approval could not be applied safely."""


class ExecutionStore:
    """Short-transaction durable queue with leases and monotonic fences."""

    def __init__(self, settings: RickySettings) -> None:
        self.settings = settings.executions
        self.user_root = user_data_path(settings)
        self.db_path = self.user_root / self.settings.store_path

    async def initialize(self) -> None:
        await self._run(self._initialize)

    async def submit(
        self,
        request: ExecutionRequest,
        *,
        scope: ProfileScope,
    ) -> ExecutionRequest:
        if request.status != "queued" or request.claim_fence != 0:
            raise ValueError("new execution requests must be unclaimed and queued")
        _require_profile_access(scope, request.profile_scope, "execution request", request.id)
        return await self._run(self._submit, request)

    async def create_draft(
        self,
        draft: ExecutionDraft,
        *,
        scope: ProfileScope,
    ) -> ExecutionDraft:
        if draft.revision != 1:
            raise ValueError("new execution drafts must start at revision 1")
        _require_profile_access(scope, draft.profile_scope, "execution draft", draft.id)
        return await self._run(self._create_draft, draft)

    async def get_draft(self, draft_id: str, *, scope: ProfileScope) -> ExecutionDraft:
        found = await self._run(self._get_draft, draft_id)
        if found is None or not scope.permits(found.profile_scope.label()):
            raise ExecutionNotFoundError(f"execution draft not found: {draft_id}")
        return found

    async def find_open_draft(
        self,
        *,
        conversation_id: str,
        task_id: str,
        scope: ProfileScope,
    ) -> ExecutionDraft | None:
        found = await self._run(self._find_open_draft, conversation_id, task_id)
        return found if found is not None and scope.permits(found.profile_scope.label()) else None

    async def find_drafts_by_message(
        self,
        message_id: str,
        *,
        scope: ProfileScope,
        limit: int = 50,
    ) -> list[ExecutionDraft]:
        if limit < 1 or limit > 1_000:
            raise ValueError("execution draft lookup limit must be between 1 and 1000")
        found = await self._run(self._find_drafts_by_message, message_id, 1_000)
        return _permitted(scope, found)[:limit]

    async def find_draft_for_contract(
        self,
        contract_id: str,
        *,
        scope: ProfileScope,
    ) -> ExecutionDraft | None:
        found = await self._run(self._find_draft_for_contract, contract_id)
        return found if found is not None and scope.permits(found.profile_scope.label()) else None

    async def update_draft(
        self,
        draft: ExecutionDraft,
        *,
        expected_revision: int,
        kind: DraftActivityKind,
        summary: str,
        scope: ProfileScope,
    ) -> ExecutionDraft:
        if draft.revision != expected_revision + 1:
            raise ValueError("updated execution draft must advance exactly one revision")
        current = await self.get_draft(draft.id, scope=scope)
        _require_profile_access(scope, draft.profile_scope, "execution draft", draft.id)
        if current.profile_scope != draft.profile_scope:
            raise ExecutionDraftFenceError("execution draft profile scope is immutable")
        return await self._run(self._update_draft, draft, expected_revision, kind, summary[:2_000])

    async def list_drafts(
        self,
        *,
        scope: ProfileScope,
        status: DraftStatus | None = None,
        limit: int = 50,
    ) -> list[ExecutionDraft]:
        if limit < 1 or limit > 1_000:
            raise ValueError("execution draft list limit must be between 1 and 1000")
        found = await self._run(self._list_drafts, status, 1_000)
        return _permitted(scope, found)[:limit]

    async def draft_activities(
        self,
        draft_id: str,
        *,
        scope: ProfileScope,
        limit: int = 100,
    ) -> list[ExecutionDraftActivity]:
        draft = await self.get_draft(draft_id, scope=scope)
        return await self._run(
            self._draft_activities,
            draft_id,
            draft.profile_scope,
            limit,
        )

    async def cancel_draft(
        self,
        draft_id: str,
        *,
        scope: ProfileScope,
        now: datetime | None = None,
    ) -> ExecutionDraft:
        current = await self.get_draft(draft_id, scope=scope)
        if current.status == "cancelled":
            return current
        if current.status in {
            "queued",
            "executing",
            "completed",
            "uncertain",
            "expired",
            "rejected",
        }:
            raise ExecutionStoreError(f"cannot cancel execution draft in {current.status} state")
        cancelled = current.model_copy(
            update={
                "status": "cancelled",
                "revision": current.revision + 1,
                "updated_at": now or datetime.now(UTC),
                "reason": "cancelled by user",
            }
        )
        return await self.update_draft(
            cancelled,
            expected_revision=current.revision,
            kind="cancelled",
            summary="Execution draft cancelled by user",
            scope=scope,
        )

    async def expire_drafts(
        self,
        *,
        scope: ProfileScope,
        now: datetime | None = None,
    ) -> list[ExecutionDraft]:
        moment = now or datetime.now(UTC)
        expired: list[ExecutionDraft] = []
        for current in await self.list_drafts(scope=scope, limit=1_000):
            if (
                current.status
                not in {
                    "collecting_guardrails",
                    "awaiting_confirmation",
                    "ready",
                }
                or current.expires_at > moment
            ):
                continue
            candidate = current.model_copy(
                update={
                    "status": "expired",
                    "revision": current.revision + 1,
                    "updated_at": moment,
                    "reason": "live review evidence expired",
                }
            )
            try:
                expired.append(
                    await self.update_draft(
                        candidate,
                        expected_revision=current.revision,
                        kind="expired",
                        summary="Execution draft expired",
                        scope=scope,
                    )
                )
            except ExecutionDraftFenceError:
                continue
        return expired

    async def recover_interrupted_foreground_drafts(
        self,
        *,
        scope: ProfileScope,
        now: datetime | None = None,
    ) -> list[ExecutionDraft]:
        moment = now or datetime.now(UTC)
        recovered: list[ExecutionDraft] = []
        for current in await self.list_drafts(
            scope=scope,
            status="executing",
            limit=1_000,
        ):
            if current.target != "gateway_foreground":
                continue
            candidate = current.model_copy(
                update={
                    "status": "uncertain",
                    "revision": current.revision + 1,
                    "updated_at": moment,
                    "reason": "gateway stopped after direct call reservation",
                }
            )
            try:
                recovered.append(
                    await self.update_draft(
                        candidate,
                        expected_revision=current.revision,
                        kind="uncertain",
                        summary="Interrupted exact foreground call is uncertain",
                        scope=scope,
                    )
                )
            except ExecutionDraftFenceError:
                continue
        return recovered

    async def get_confirmation(
        self,
        confirmation_id: str,
        *,
        scope: ProfileScope,
    ) -> ConfirmationRef:
        found = await self._run(self._get_confirmation, confirmation_id)
        if found is None:
            raise ExecutionNotFoundError(f"execution confirmation not found: {confirmation_id}")
        await self.get_draft(found.draft_id, scope=scope)
        return found

    async def attach_contract(
        self,
        contract: ExecutionContract,
        *,
        draft_id: str,
        expected_revision: int,
        scope: ProfileScope,
        now: datetime | None = None,
    ) -> ExecutionDraft:
        """Atomically persist one contract and bind it to its reviewed draft."""

        _require_profile_access(scope, contract.profile_scope, "execution contract", contract.id)
        draft = await self.get_draft(draft_id, scope=scope)
        if draft.profile_scope != contract.profile_scope:
            raise ExecutionDraftFenceError("execution contract profile scope differs from draft")
        return await self._run(
            self._attach_contract,
            contract,
            draft_id,
            expected_revision,
            now or datetime.now(UTC),
        )

    async def get_contract(
        self,
        contract_id_or_digest: str,
        *,
        scope: ProfileScope,
    ) -> ExecutionContract:
        found = await self._run(self._get_contract, contract_id_or_digest)
        if found is None or not scope.permits(found.profile_scope.label()):
            raise ExecutionNotFoundError(f"execution contract not found: {contract_id_or_digest}")
        return found

    async def list_contracts(
        self,
        *,
        scope: ProfileScope,
        limit: int = 50,
    ) -> list[ExecutionContract]:
        if limit < 1 or limit > 1_000:
            raise ValueError("execution contract list limit must be between 1 and 1000")
        found = await self._run(self._list_contracts, 1_000)
        return _permitted(scope, found)[:limit]

    async def request_for_contract(
        self,
        contract_id: str,
        *,
        scope: ProfileScope,
    ) -> ExecutionRequest | None:
        await self.get_contract(contract_id, scope=scope)
        found = await self._run(self._request_for_contract, contract_id)
        return found if found is None or scope.permits(found.profile_scope.label()) else None

    async def get(self, request_id: str, *, scope: ProfileScope) -> ExecutionRequest:
        found = await self._run(self._get, validate_execution_id(request_id))
        if found is None or not scope.permits(found.profile_scope.label()):
            raise ExecutionNotFoundError(f"execution request not found: {request_id}")
        return found

    async def park_browser_approval(
        self,
        draft: BrowserApprovalDraft,
        *,
        scope: ProfileScope,
        token: str,
        fence: int,
        code: str | None = None,
    ) -> BrowserTransactionChallenge:
        """Park one live prepared occurrence before any effect reservation."""

        request = await self.get(draft.request_id, scope=scope)
        _require_profile_access(scope, request.profile_scope, "execution request", request.id)
        if draft.claim_fence != fence:
            raise ExecutionFenceError("parked browser approval carries another claim fence")
        plaintext = code or secrets.token_urlsafe(24)
        stored = issue_parked_approval(draft, code=plaintext)
        parked = await self._run(self._park_browser_approval, stored, token, fence)
        return BrowserTransactionChallenge(approval=parked, code=plaintext)

    async def get_browser_approval(
        self,
        approval_id: str,
        *,
        scope: ProfileScope,
    ) -> ParkedBrowserApproval:
        found = await self._run(self._get_browser_approval, approval_id)
        if found is None:
            raise ExecutionNotFoundError(f"browser approval not found: {approval_id}")
        request = await self.get(found.request_id, scope=scope)
        _require_profile_access(scope, request.profile_scope, "browser approval", approval_id)
        return found

    async def browser_approvals_for_request(
        self,
        request_id: str,
        *,
        scope: ProfileScope,
    ) -> list[ParkedBrowserApproval]:
        await self.get(request_id, scope=scope)
        return await self._run(self._browser_approvals_for_request, request_id)

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
        now: datetime | None = None,
    ) -> ParkedBrowserApproval:
        """Apply one authenticated source-bound approval or denial exactly once."""

        current = await self.get_browser_approval(approval_id, scope=scope)
        if current.conversation_id != conversation_id:
            raise BrowserApprovalError("browser approval belongs to another conversation")
        return await self._run(
            self._decide_browser_approval,
            approval_id,
            approve,
            principal_id,
            conversation_id,
            source_message_id,
            challenge_digest(code),
            now or datetime.now(UTC),
        )

    async def expire_browser_approvals(
        self,
        *,
        scope: ProfileScope,
        now: datetime | None = None,
    ) -> list[ParkedBrowserApproval]:
        """Expire only accessible pending approvals without dispatching or resuming."""

        return await self._run(
            self._expire_browser_approvals,
            scope,
            now or datetime.now(UTC),
        )

    async def resume_browser_approval(
        self,
        approval_id: str,
        *,
        scope: ProfileScope,
        token: str,
        fence: int,
        consume: bool,
        now: datetime | None = None,
    ) -> ParkedBrowserApproval:
        """Wake the owning runtime; consume approval only immediately before reservation."""

        current = await self.get_browser_approval(approval_id, scope=scope)
        await self.get(current.request_id, scope=scope)
        return await self._run(
            self._resume_browser_approval,
            approval_id,
            token,
            fence,
            consume,
            now or datetime.now(UTC),
        )

    async def invalidate_browser_approval(
        self,
        approval_id: str,
        *,
        scope: ProfileScope,
        token: str,
        fence: int,
        reason: str,
        now: datetime | None = None,
    ) -> ParkedBrowserApproval:
        if not reason.strip():
            raise ValueError("browser approval invalidation reason cannot be blank")
        current = await self.get_browser_approval(approval_id, scope=scope)
        await self.get(current.request_id, scope=scope)
        return await self._run(
            self._invalidate_browser_approval,
            approval_id,
            token,
            fence,
            reason[:2_000],
            now or datetime.now(UTC),
        )

    async def wait_for_browser_approval(
        self,
        approval_id: str,
        *,
        scope: ProfileScope,
        poll_seconds: float = 0.25,
    ) -> ParkedBrowserApproval:
        """Wait only for the matching decision, cancellation, invalidation, or expiry."""

        if poll_seconds <= 0 or poll_seconds > 5:
            raise ValueError("browser approval poll interval must be between 0 and 5 seconds")
        while True:
            current = await self.get_browser_approval(approval_id, scope=scope)
            request = await self.get(current.request_id, scope=scope)
            if current.state != "pending" or request.status == "cancel_requested":
                return current
            if current.expires_at <= datetime.now(UTC):
                await self.expire_browser_approvals(scope=scope)
                continue
            await asyncio.sleep(poll_seconds)

    async def attest_browser_transaction(
        self,
        transaction_id: str,
        *,
        scope: ProfileScope,
        disposition: ExecutionResolutionDisposition,
        actor_principal_id: str,
        source_conversation_id: str,
        source_message_id: str,
        note: str,
        now: datetime | None = None,
    ) -> tuple[ExecutionRequest, BrowserTransactionAttestation]:
        if not note.strip():
            raise ValueError("browser transaction attestation note cannot be blank")
        approval = await self.get_browser_approval(transaction_id, scope=scope)
        if not isinstance(approval, ParkedBrowserTransaction):
            raise BrowserApprovalError("only browser transactions can be reconciled")
        if approval.conversation_id != source_conversation_id:
            raise BrowserApprovalError("browser transaction belongs to another conversation")
        if approval.principal_id != actor_principal_id:
            raise BrowserApprovalError("browser transaction belongs to another principal")
        return await self._run(
            self._attest_browser_transaction,
            transaction_id,
            disposition,
            actor_principal_id,
            source_conversation_id,
            source_message_id,
            note[:2_000],
            now or datetime.now(UTC),
        )

    async def browser_transaction_attestations(
        self,
        transaction_id: str,
        *,
        scope: ProfileScope,
    ) -> list[BrowserTransactionAttestation]:
        approval = await self.get_browser_approval(transaction_id, scope=scope)
        if not isinstance(approval, ParkedBrowserTransaction):
            raise BrowserApprovalError("only browser transactions have attestations")
        return await self._run(self._browser_transaction_attestations, transaction_id)

    async def list(
        self,
        *,
        scope: ProfileScope,
        status: ExecutionStatus | None = None,
        limit: int = 50,
    ) -> list[ExecutionRequest]:
        if limit < 1 or limit > 1_000:
            raise ValueError("execution list limit must be between 1 and 1000")
        found = await self._run(self._list, status, 1_000)
        return _permitted(scope, found)[:limit]

    async def list_for_notification_projection(
        self,
        *,
        scope: ProfileScope,
    ) -> list[ExecutionRequest]:
        """Return all terminal executions oldest first for source-id projection."""

        return _permitted(scope, await self._run(self._list_for_notification_projection))

    async def list_by_conversation(
        self,
        conversation_id: str,
        *,
        scope: ProfileScope,
        statuses: Sequence[ExecutionStatus] = (),
        limit: int = 100,
    ) -> list[ExecutionRequest]:
        """List one conversation's requests before applying the bounded limit."""

        if not conversation_id.strip():
            raise ValueError("conversation_id cannot be blank")
        if limit < 1 or limit > 1_000:
            raise ValueError("execution list limit must be between 1 and 1000")
        found = await self._run(
            self._list_by_conversation,
            conversation_id,
            tuple(statuses),
            1_000,
        )
        return _permitted(scope, found)[:limit]

    async def list_by_source_message(
        self,
        message_id: str,
        *,
        scope: ProfileScope,
        limit: int = 100,
    ) -> list[ExecutionRequest]:
        """List requests created by one authenticated source message."""

        if not message_id.strip():
            raise ValueError("message_id cannot be blank")
        if limit < 1 or limit > 1_000:
            raise ValueError("execution list limit must be between 1 and 1000")
        found = await self._run(self._list_by_source_message, message_id, 1_000)
        return _permitted(scope, found)[:limit]

    async def protected_message_ids(self, *, scope: ProfileScope) -> tuple[str, ...]:
        """Return all inbound ids retained by drafts or unresolved requests."""

        return await self._run(self._protected_message_ids, scope)

    async def protected_parent_request_ids(self, *, scope: ProfileScope) -> tuple[str, ...]:
        """Return every parent request referenced by an immutable contract."""

        return await self._run(self._protected_parent_request_ids, scope)

    async def claim(
        self,
        *,
        scope: ProfileScope,
        worker_id: str,
        limit: int,
        now: datetime | None = None,
    ) -> list[ExecutionRequest]:
        if not worker_id.strip() or len(worker_id) > 200:
            raise ValueError("worker_id must be 1-200 characters")
        if limit < 1 or limit > self.settings.concurrency:
            raise ValueError("claim limit exceeds configured concurrency")
        return await self._run(self._claim, scope, worker_id, limit, now or datetime.now(UTC))

    async def start(
        self,
        request_id: str,
        *,
        scope: ProfileScope,
        token: str,
        fence: int,
        run_id: str,
    ) -> ExecutionRequest:
        await self.get(request_id, scope=scope)
        return await self._run(
            self._transition_claimed,
            validate_execution_id(request_id),
            token,
            fence,
            "running",
            "started",
            run_id,
            None,
        )

    async def renew(
        self,
        request_id: str,
        *,
        scope: ProfileScope,
        token: str,
        fence: int,
        now: datetime | None = None,
    ) -> ExecutionRequest:
        await self.get(request_id, scope=scope)
        return await self._run(
            self._renew, validate_execution_id(request_id), token, fence, now or datetime.now(UTC)
        )

    async def release(
        self,
        request_id: str,
        *,
        scope: ProfileScope,
        token: str,
        fence: int,
    ) -> ExecutionRequest:
        await self.get(request_id, scope=scope)
        return await self._run(
            self._transition_claimed,
            validate_execution_id(request_id),
            token,
            fence,
            "queued",
            "released",
            None,
            None,
        )

    async def finish(
        self,
        request_id: str,
        *,
        scope: ProfileScope,
        token: str,
        fence: int,
        status: ExecutionStatus,
        error: str | None = None,
    ) -> ExecutionRequest:
        if status not in {"succeeded", "failed", "blocked", "cancelled", "uncertain"}:
            raise ValueError("invalid terminal execution status")
        await self.get(request_id, scope=scope)
        return await self._run(
            self._finish, validate_execution_id(request_id), token, fence, status, error
        )

    async def cancel(self, request_id: str, *, scope: ProfileScope) -> ExecutionRequest:
        await self.get(request_id, scope=scope)
        return await self._run(self._cancel, validate_execution_id(request_id))

    async def retry(
        self,
        request_id: str,
        *,
        scope: ProfileScope,
        created_at: datetime | None = None,
    ) -> ExecutionRequest:
        await self.get(request_id, scope=scope)
        return await self._run(
            self._retry, validate_execution_id(request_id), created_at or datetime.now(UTC)
        )

    async def resolve(
        self,
        request_id: str,
        *,
        scope: ProfileScope,
        disposition: ExecutionResolutionDisposition,
        actor: str,
        note: str,
    ) -> ExecutionRequest:
        if not actor.strip() or not note.strip():
            raise ValueError("resolution actor and note cannot be blank")
        await self.get(request_id, scope=scope)
        return await self._run(
            self._resolve,
            validate_execution_id(request_id),
            disposition,
            actor,
            note,
            datetime.now(UTC),
        )

    async def activities(
        self,
        request_id: str,
        *,
        scope: ProfileScope,
        limit: int = 100,
    ) -> list[ExecutionActivity]:
        request = await self.get(request_id, scope=scope)
        return await self._run(
            self._activities,
            validate_execution_id(request_id),
            request.profile_scope,
            limit,
        )

    async def resolutions(
        self,
        request_id: str,
        *,
        scope: ProfileScope,
    ) -> list[ExecutionResolution]:
        request = await self.get(request_id, scope=scope)
        return await self._run(
            self._resolutions,
            validate_execution_id(request_id),
            request.profile_scope,
        )

    async def recover_expired(
        self,
        *,
        scope: ProfileScope,
        now: datetime | None = None,
    ) -> list[ExecutionRequest]:
        return await self._run(self._recover_expired, scope, now or datetime.now(UTC))

    async def counts(self, *, scope: ProfileScope) -> dict[str, int]:
        """Count execution requests by status without loading any goal text."""

        return await self._run(self._counts, scope)

    async def prunable(
        self,
        *,
        scope: ProfileScope,
        keep: int,
        before: datetime,
    ) -> list[str]:
        """List succeeded or cancelled request ids beyond the retention ceiling."""

        return await self._run(self._prunable, scope, keep, before)

    async def prune(self, request_ids: Sequence[str], *, scope: ProfileScope) -> int:
        """Delete exactly these terminal requests and their activity records."""

        return await self._run(self._prune, scope, tuple(request_ids))

    async def find_by_task(
        self,
        task_id: str,
        *,
        scope: ProfileScope,
        limit: int = 50,
    ) -> list[ExecutionRequest]:
        """List execution requests attached to one durable task, newest first."""

        if limit < 1 or limit > 1_000:
            raise ValueError("execution list limit must be between 1 and 1000")
        found = await self._run(self._find_by_task, task_id, 1_000)
        return _permitted(scope, found)[:limit]

    async def _run[T](self, operation: Callable[..., T], *args: object) -> T:
        task = asyncio.create_task(asyncio.to_thread(operation, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.shield(task)
            raise
        except sqlite3.Error as exc:
            raise ExecutionStoreError("execution store operation failed") from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=self.settings.sqlite_busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(f"PRAGMA busy_timeout = {self.settings.sqlite_busy_timeout_ms}")
        return connection

    def _initialize(self) -> None:
        from ricky.executions.upgrade import (
            create_current_executions_database,
            inspect_executions_database,
        )

        if not self.db_path.exists():
            create_current_executions_database(self.db_path)
        else:
            inspection = inspect_executions_database(self.db_path)
            if inspection.state != "current":
                raise ExecutionStoreError(inspection.detail)
        # Requests carry goals and source identities, so they are retained as private evidence.
        self.db_path.chmod(0o600)

    def _submit(self, request: ExecutionRequest) -> ExecutionRequest:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM execution_requests WHERE request_key = ?", (request.request_key,)
            ).fetchone()
            if existing is not None:
                found = _row(existing)
                if not _same_submission(found, request):
                    raise ExecutionStoreError(
                        "execution request key is already bound to another submission"
                    )
                connection.commit()
                return found
            connection.execute(
                f"INSERT INTO execution_requests ({','.join(_COLUMNS)}) "
                f"VALUES ({','.join('?' for _ in _COLUMNS)})",
                _values(request),
            )
            self._activity(
                connection, request, "submitted", None, "queued", "Execution request submitted"
            )
            connection.commit()
        return request

    def _create_draft(self, draft: ExecutionDraft) -> ExecutionDraft:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT data_json FROM execution_drafts WHERE id=?", (draft.id,)
            ).fetchone()
            if existing is not None:
                found = ExecutionDraft.model_validate_json(existing["data_json"])
                excluded = {"created_at", "updated_at", "expires_at"}
                if found.model_dump(exclude=excluded) != draft.model_dump(exclude=excluded):
                    raise ExecutionDraftFenceError("deterministic execution draft collision")
                connection.commit()
                return found
            connection.execute(
                """
                INSERT INTO execution_drafts
                    (id, conversation_id, task_id, status, revision, updated_at,
                     expires_at, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft.id,
                    draft.conversation_id,
                    draft.task_id or "",
                    draft.status,
                    draft.revision,
                    _dt(draft.updated_at),
                    _dt(draft.expires_at),
                    draft.model_dump_json(),
                ),
            )
            self._replace_draft_sources(connection, draft)
            self._replace_draft_guardrail_fields(connection, draft)
            self._draft_activity(
                connection,
                draft,
                "created",
                None,
                draft.status,
                "Execution draft created",
            )
            connection.commit()
        return draft

    def _get_draft(self, draft_id: str) -> ExecutionDraft | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data_json FROM execution_drafts WHERE id=?", (draft_id,)
            ).fetchone()
        return ExecutionDraft.model_validate_json(row["data_json"]) if row is not None else None

    def _find_open_draft(self, conversation_id: str, task_id: str) -> ExecutionDraft | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT data_json FROM execution_drafts
                WHERE conversation_id=? AND task_id=?
                  AND status IN ('collecting_guardrails','awaiting_confirmation','ready')
                ORDER BY updated_at DESC, id DESC LIMIT 1
                """,
                (conversation_id, task_id),
            ).fetchone()
        return ExecutionDraft.model_validate_json(row["data_json"]) if row is not None else None

    def _find_drafts_by_message(self, message_id: str, limit: int) -> list[ExecutionDraft]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT d.data_json FROM execution_drafts AS d
                JOIN execution_draft_sources AS s ON s.draft_id=d.id
                WHERE s.message_id=?
                ORDER BY d.updated_at DESC, d.id DESC LIMIT ?
                """,
                (message_id, limit),
            ).fetchall()
        return [ExecutionDraft.model_validate_json(row["data_json"]) for row in rows]

    def _find_draft_for_contract(self, contract_id: str) -> ExecutionDraft | None:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT data_json FROM execution_drafts ORDER BY updated_at DESC"
            ).fetchall()
        for row in rows:
            draft = ExecutionDraft.model_validate_json(row["data_json"])
            if draft.contract_id == contract_id:
                return draft
        return None

    def _update_draft(
        self,
        draft: ExecutionDraft,
        expected_revision: int,
        kind: DraftActivityKind,
        summary: str,
    ) -> ExecutionDraft:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data_json, revision FROM execution_drafts WHERE id=?", (draft.id,)
            ).fetchone()
            if row is None:
                raise ExecutionNotFoundError(f"execution draft not found: {draft.id}")
            before = ExecutionDraft.model_validate_json(row["data_json"])
            if int(row["revision"]) != expected_revision:
                raise ExecutionDraftFenceError(
                    f"stale execution draft revision: expected {expected_revision}, "
                    f"found {row['revision']}"
                )
            immutable = (
                "target",
                "principal_id",
                "conversation_id",
                "task_id",
                "task_revision",
                "retry_of",
                "profile_scope",
                "goal",
                "requested_capabilities",
                "foreground_call",
                "created_at",
            )
            if any(getattr(before, name) != getattr(draft, name) for name in immutable):
                raise ExecutionDraftFenceError("execution draft immutable identity changed")
            if draft.contract_id != before.contract_id:
                raise ExecutionDraftFenceError(
                    "execution contract linkage changes only through atomic attachment"
                )
            if before.request_id is not None and draft.request_id != before.request_id:
                raise ExecutionDraftFenceError("execution request linkage is immutable")
            if (
                before.request_id is None
                and draft.request_id is not None
                and (before.contract_id is None or draft.status != "queued")
            ):
                raise ExecutionDraftFenceError(
                    "a request can attach only while queueing a compiled draft"
                )
            if before.confirmation is None and draft.confirmation is not None:
                confirmation = draft.confirmation
                if confirmation.draft_revision != expected_revision or (
                    before.confirmation_summary_digest != confirmation.summary_digest
                ):
                    raise ExecutionDraftFenceError(
                        "confirmation targets another draft revision or summary"
                    )
                connection.execute(
                    """
                    INSERT INTO execution_contract_confirmations
                        (id, draft_id, draft_revision, summary_digest, expires_at, data_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        confirmation.id,
                        confirmation.draft_id,
                        confirmation.draft_revision,
                        confirmation.summary_digest,
                        _dt(confirmation.expires_at),
                        confirmation.model_dump_json(),
                    ),
                )
            elif before.confirmation != draft.confirmation:
                raise ExecutionDraftFenceError("stored confirmation evidence is immutable")
            if before.confirmation is not None or before.contract_id is not None:
                protected_evidence = (
                    "sources",
                    "collected_guardrail_fields",
                    "guardrails",
                    "pending_questions",
                    "confirmation_summary",
                    "confirmation_summary_digest",
                )
                if any(
                    getattr(before, name) != getattr(draft, name) for name in protected_evidence
                ):
                    raise ExecutionDraftFenceError(
                        "confirmed or compiled draft evidence is immutable"
                    )
            changed = connection.execute(
                """
                UPDATE execution_drafts
                SET status=?, revision=?, updated_at=?, expires_at=?, data_json=?
                WHERE id=? AND revision=?
                """,
                (
                    draft.status,
                    draft.revision,
                    _dt(draft.updated_at),
                    _dt(draft.expires_at),
                    draft.model_dump_json(),
                    draft.id,
                    expected_revision,
                ),
            ).rowcount
            if changed != 1:
                raise ExecutionDraftFenceError("execution draft compare-and-swap failed")
            self._replace_draft_sources(connection, draft)
            self._replace_draft_guardrail_fields(connection, draft)
            self._draft_activity(connection, draft, kind, before.status, draft.status, summary)
            connection.commit()
        return draft

    def _list_drafts(self, status: DraftStatus | None, limit: int) -> list[ExecutionDraft]:
        sql = "SELECT data_json FROM execution_drafts"
        params: list[object] = []
        if status is not None:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [ExecutionDraft.model_validate_json(row["data_json"]) for row in rows]

    def _draft_activities(
        self,
        draft_id: str,
        profile_scope: ProfileScope,
        limit: int,
    ) -> list[ExecutionDraftActivity]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM execution_draft_activity
                WHERE draft_id=? ORDER BY id DESC LIMIT ?
                """,
                (draft_id, limit),
            ).fetchall()
        label = profile_scope.label()
        return [
            ExecutionDraftActivity.model_validate({**dict(row), "profile_label": label})
            for row in rows
        ]

    def _get_confirmation(self, confirmation_id: str) -> ConfirmationRef | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data_json FROM execution_contract_confirmations WHERE id=?",
                (confirmation_id,),
            ).fetchone()
        return ConfirmationRef.model_validate_json(row["data_json"]) if row is not None else None

    def _attach_contract(
        self,
        contract: ExecutionContract,
        draft_id: str,
        expected_revision: int,
        now: datetime,
    ) -> ExecutionDraft:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            draft_row = connection.execute(
                "SELECT data_json, revision FROM execution_drafts WHERE id=?",
                (draft_id,),
            ).fetchone()
            if draft_row is None:
                raise ExecutionNotFoundError(f"execution draft not found: {draft_id}")
            draft = ExecutionDraft.model_validate_json(draft_row["data_json"])
            existing = connection.execute(
                "SELECT data_json FROM execution_contracts WHERE id=? OR digest=?",
                (contract.id, contract.digest),
            ).fetchone()
            if existing is not None:
                found = ExecutionContract.model_validate_json(existing["data_json"])
                if found != contract:
                    raise ExecutionStoreError("immutable execution contract collision")
            if draft.contract_id is not None:
                if draft.contract_id != contract.id or existing is None:
                    raise ExecutionDraftFenceError("draft is bound to another contract")
                connection.commit()
                return draft
            if int(draft_row["revision"]) != expected_revision:
                raise ExecutionDraftFenceError(
                    f"stale execution draft revision: expected {expected_revision}, "
                    f"found {draft_row['revision']}"
                )
            if draft.status != "ready" or draft.target != "ad_hoc_background":
                raise ExecutionDraftFenceError("only a ready ad hoc draft can bind a contract")
            if (
                contract.task_id != draft.task_id
                or contract.task_revision != draft.task_revision
                or contract.goal != draft.goal
                or contract.principal_id != draft.principal_id
                or contract.source_conversation_id != draft.conversation_id
                or contract.source_message_ids
                != tuple(source.message_id for source in draft.sources)
                or {item.id for item in contract.capabilities} != set(draft.requested_capabilities)
                or contract.guardrails != draft.guardrails
            ):
                raise ExecutionDraftFenceError("execution contract differs from its draft")
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO execution_contracts
                        (id, digest, task_id, created_at, expires_at, data_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        contract.id,
                        contract.digest,
                        contract.task_id,
                        _dt(contract.created_at),
                        _dt(contract.expires_at) if contract.expires_at is not None else None,
                        contract.model_dump_json(),
                    ),
                )
            attached = draft.model_copy(
                update={
                    "contract_id": contract.id,
                    "revision": draft.revision + 1,
                    "updated_at": now,
                }
            )
            changed = connection.execute(
                """
                UPDATE execution_drafts
                SET revision=?, updated_at=?, data_json=?
                WHERE id=? AND revision=?
                """,
                (
                    attached.revision,
                    _dt(attached.updated_at),
                    attached.model_dump_json(),
                    attached.id,
                    expected_revision,
                ),
            ).rowcount
            if changed != 1:
                raise ExecutionDraftFenceError("execution draft compare-and-swap failed")
            self._draft_activity(
                connection,
                attached,
                "ready",
                draft.status,
                attached.status,
                f"Immutable execution contract compiled: {contract.id}",
            )
            connection.commit()
        return attached

    def _get_contract(self, contract_id_or_digest: str) -> ExecutionContract | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data_json FROM execution_contracts WHERE id=? OR digest=?",
                (contract_id_or_digest, contract_id_or_digest),
            ).fetchone()
        return ExecutionContract.model_validate_json(row["data_json"]) if row is not None else None

    def _list_contracts(self, limit: int) -> list[ExecutionContract]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT data_json FROM execution_contracts "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [ExecutionContract.model_validate_json(row["data_json"]) for row in rows]

    def _request_for_contract(self, contract_id: str) -> ExecutionRequest | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM execution_requests WHERE contract_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (contract_id,),
            ).fetchone()
        return _row(row) if row is not None else None

    def _replace_draft_sources(self, connection: sqlite3.Connection, draft: ExecutionDraft) -> None:
        connection.execute("DELETE FROM execution_draft_sources WHERE draft_id=?", (draft.id,))
        connection.executemany(
            """
            INSERT INTO execution_draft_sources (draft_id, message_id, source_json)
            VALUES (?, ?, ?)
            """,
            [(draft.id, source.message_id, source.model_dump_json()) for source in draft.sources],
        )

    def _replace_draft_guardrail_fields(
        self, connection: sqlite3.Connection, draft: ExecutionDraft
    ) -> None:
        connection.execute(
            "DELETE FROM execution_draft_guardrail_fields WHERE draft_id=?", (draft.id,)
        )
        connection.executemany(
            """
            INSERT INTO execution_draft_guardrail_fields
                (draft_id, capability_id, field_name, source_message_id, field_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    draft.id,
                    item.capability_id,
                    item.field,
                    item.source_message_id,
                    item.model_dump_json(),
                )
                for item in draft.collected_guardrail_fields
            ],
        )

    def _draft_activity(
        self,
        connection: sqlite3.Connection,
        draft: ExecutionDraft,
        kind: DraftActivityKind,
        from_status: DraftStatus | None,
        to_status: DraftStatus,
        summary: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO execution_draft_activity
                (draft_id, kind, from_status, to_status, revision, summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft.id,
                kind,
                from_status,
                to_status,
                draft.revision,
                summary[:2_000],
                _dt(datetime.now(UTC)),
            ),
        )

    def _get(self, request_id: str) -> ExecutionRequest | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM execution_requests WHERE id = ?", (request_id,)
            ).fetchone()
        return _row(row) if row is not None else None

    def _park_browser_approval(
        self,
        approval: ParkedBrowserApproval,
        token: str,
        fence: int,
    ) -> ParkedBrowserApproval:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = self._fenced(
                connection,
                approval.request_id,
                token,
                fence,
                required="running",
            )
            if before.kind != "ad_hoc" or before.run_id != approval.run_id:
                raise BrowserApprovalError(
                    "parked browser approval requires its gateway-owned ad hoc run"
                )
            if before.source_conversation_id != approval.conversation_id:
                raise BrowserApprovalError("parked approval conversation differs from execution")
            if approval.principal_id.strip() == "":
                raise BrowserApprovalError("parked approval principal cannot be blank")
            if before.expires_at is not None and approval.expires_at > before.expires_at:
                raise BrowserApprovalError("parked approval outlives its execution contract")
            if approval.state != "pending" or approval.revision != 1:
                raise BrowserApprovalError("new parked browser approval must be pending revision 1")
            expected = (
                "awaiting_transaction_approval"
                if isinstance(approval, ParkedBrowserTransaction)
                else "awaiting_protected_approval"
            )
            logical_key = approval.logical_effect_key
            prior_logical = connection.execute(
                """SELECT state FROM execution_browser_approvals
                WHERE logical_effect_key=? AND state IN ('pending','approved','consumed')
                LIMIT 1""",
                (logical_key,),
            ).fetchone()
            if prior_logical is not None:
                raise BrowserApprovalError(
                    "logical browser effect already has active or consumed approval evidence"
                )
            try:
                connection.execute(
                    """INSERT INTO execution_browser_approvals (
                        id, request_id, kind, state, revision, logical_effect_key,
                        expires_at, data_json
                    ) VALUES (?, ?, ?, 'pending', 1, ?, ?, ?)""",
                    (
                        approval.id,
                        approval.request_id,
                        approval.kind,
                        logical_key,
                        _dt(approval.expires_at),
                        _APPROVAL_ADAPTER.dump_json(approval).decode("utf-8"),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise BrowserApprovalError(
                    "execution already has an active or consumed browser approval"
                ) from exc
            connection.execute(
                "UPDATE execution_requests SET status=? WHERE id=?",
                (expected, approval.request_id),
            )
            current = self._required(connection, approval.request_id)
            self._activity(
                connection,
                current,
                "approval_requested",
                before.status,
                expected,
                f"Exact {approval.kind.replace('_', ' ')} approval requested",
            )
            connection.commit()
        return approval

    def _get_browser_approval(self, approval_id: str) -> ParkedBrowserApproval | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data_json FROM execution_browser_approvals WHERE id=?",
                (approval_id,),
            ).fetchone()
        return _APPROVAL_ADAPTER.validate_json(row["data_json"]) if row is not None else None

    def _browser_approvals_for_request(self, request_id: str) -> list[ParkedBrowserApproval]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT data_json FROM execution_browser_approvals
                WHERE request_id=? ORDER BY rowid""",
                (request_id,),
            ).fetchall()
        return [_APPROVAL_ADAPTER.validate_json(row["data_json"]) for row in rows]

    def _decide_browser_approval(
        self,
        approval_id: str,
        approve: bool,
        principal_id: str,
        conversation_id: str,
        source_message_id: str,
        supplied_challenge_digest: str,
        now: datetime,
    ) -> ParkedBrowserApproval:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            approval = self._required_browser_approval(connection, approval_id)
            request = self._required(connection, approval.request_id)
            if request.status not in _PARKED:
                raise BrowserApprovalError("execution is no longer awaiting this approval")
            if approval.state != "pending":
                raise BrowserApprovalError("browser approval challenge was already consumed")
            if approval.expires_at <= now:
                expired = approval.model_copy(
                    update={
                        "state": "expired",
                        "revision": approval.revision + 1,
                        "decided_at": now,
                    }
                )
                self._write_browser_approval(connection, expired)
                connection.commit()
                raise BrowserApprovalError("browser approval expired")
            if principal_id != approval.principal_id:
                raise BrowserApprovalError("browser approval belongs to another principal")
            if conversation_id != approval.conversation_id:
                raise BrowserApprovalError("browser approval belongs to another conversation")
            if supplied_challenge_digest != approval.challenge_digest:
                raise BrowserApprovalError("browser approval code does not match")
            state = "approved" if approve else "denied"
            decided = approval.model_copy(
                update={
                    "state": state,
                    "revision": approval.revision + 1,
                    "decided_at": now,
                    "decision_principal_id": principal_id,
                    "decision_source_message_id": source_message_id,
                }
            )
            self._write_browser_approval(connection, decided)
            connection.commit()
        return decided

    def _expire_browser_approvals(
        self,
        scope: ProfileScope,
        now: datetime,
    ) -> list[ParkedBrowserApproval]:
        expired: list[ParkedBrowserApproval] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT approvals.data_json, requests.profile_scope_json
                FROM execution_browser_approvals AS approvals
                JOIN execution_requests AS requests
                    ON requests.id=approvals.request_id
                WHERE approvals.state IN ('pending','approved')
                    AND approvals.expires_at<=?
                ORDER BY approvals.expires_at, approvals.id""",
                (_dt(now),),
            ).fetchall()
            for row in rows:
                request_scope = ProfileScope.model_validate_json(row["profile_scope_json"])
                if not scope.permits(request_scope.label()):
                    continue
                approval = _APPROVAL_ADAPTER.validate_json(row["data_json"])
                candidate = approval.model_copy(
                    update={
                        "state": "expired",
                        "revision": approval.revision + 1,
                        "decided_at": now,
                    }
                )
                self._write_browser_approval(connection, candidate)
                expired.append(candidate)
            connection.commit()
        return expired

    def _resume_browser_approval(
        self,
        approval_id: str,
        token: str,
        fence: int,
        consume: bool,
        now: datetime,
    ) -> ParkedBrowserApproval:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            approval = self._required_browser_approval(connection, approval_id)
            before = self._fenced(
                connection,
                approval.request_id,
                token,
                fence,
            )
            if before.status not in _PARKED:
                raise BrowserApprovalError("execution is no longer parked for approval")
            if approval.claim_fence != fence:
                raise ExecutionFenceError("browser approval belongs to another claim fence")
            resumed = approval
            if consume:
                if approval.state == "approved" and approval.expires_at > now:
                    resumed = approval.model_copy(
                        update={"state": "consumed", "revision": approval.revision + 1}
                    )
                    self._write_browser_approval(connection, resumed)
                elif approval.state == "approved":
                    resumed = approval.model_copy(
                        update={
                            "state": "expired",
                            "revision": approval.revision + 1,
                            "decided_at": now,
                        }
                    )
                    self._write_browser_approval(connection, resumed)
                elif approval.state != "expired":
                    raise BrowserApprovalError("only a current approved occurrence can be consumed")
            elif approval.state not in {"denied", "expired", "invalidated"}:
                raise BrowserApprovalError(
                    "pending or approved occurrence cannot resume unconsumed"
                )
            connection.execute(
                "UPDATE execution_requests SET status='running' WHERE id=?",
                (approval.request_id,),
            )
            current = self._required(connection, approval.request_id)
            self._activity(
                connection,
                current,
                "approval_resumed",
                before.status,
                "running",
                f"Exact {approval.kind.replace('_', ' ')} decision resumed its owner",
            )
            connection.commit()
        return resumed

    def _invalidate_browser_approval(
        self,
        approval_id: str,
        token: str,
        fence: int,
        reason: str,
        now: datetime,
    ) -> ParkedBrowserApproval:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            approval = self._required_browser_approval(connection, approval_id)
            before = self._fenced(
                connection,
                approval.request_id,
                token,
                fence,
            )
            if before.status not in _PARKED:
                raise BrowserApprovalError("execution is no longer parked for approval")
            if approval.claim_fence != fence:
                raise ExecutionFenceError("browser approval belongs to another claim fence")
            if approval.state not in {"pending", "approved"}:
                raise BrowserApprovalError("browser approval can no longer be invalidated")
            invalidated = approval.model_copy(
                update={
                    "state": "invalidated",
                    "revision": approval.revision + 1,
                    "decided_at": now,
                    "invalidation_reason": reason,
                    "decision_principal_id": None,
                    "decision_source_message_id": None,
                }
            )
            self._write_browser_approval(connection, invalidated)
            connection.execute(
                "UPDATE execution_requests SET status='running' WHERE id=?",
                (approval.request_id,),
            )
            current = self._required(connection, approval.request_id)
            self._activity(
                connection,
                current,
                "approval_resumed",
                before.status,
                "running",
                "Invalidated live browser approval occurrence",
            )
            connection.commit()
        return invalidated

    def _attest_browser_transaction(
        self,
        transaction_id: str,
        disposition: ExecutionResolutionDisposition,
        actor_principal_id: str,
        source_conversation_id: str,
        source_message_id: str,
        note: str,
        now: datetime,
    ) -> tuple[ExecutionRequest, BrowserTransactionAttestation]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            approval = self._required_browser_approval(connection, transaction_id)
            if not isinstance(approval, ParkedBrowserTransaction):
                raise BrowserApprovalError("only browser transactions can be reconciled")
            if approval.state != "consumed":
                raise BrowserApprovalError("only a consumed browser transaction can be reconciled")
            before = self._required(connection, approval.request_id)
            if before.status != "uncertain":
                raise BrowserApprovalError("only an uncertain execution can be reconciled")
            if source_conversation_id != approval.conversation_id:
                raise BrowserApprovalError("browser transaction belongs to another conversation")
            if actor_principal_id != approval.principal_id:
                raise BrowserApprovalError("browser transaction belongs to another principal")
            target: ExecutionStatus = (
                "succeeded" if disposition == "confirmed_completed" else "failed"
            )
            cursor = connection.execute(
                """INSERT INTO execution_browser_attestations (
                    transaction_id, request_id, disposition, actor_principal_id,
                    source_conversation_id, source_message_id, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    transaction_id,
                    approval.request_id,
                    disposition,
                    actor_principal_id,
                    source_conversation_id,
                    source_message_id,
                    note,
                    _dt(now),
                ),
            )
            connection.execute(
                "UPDATE execution_requests SET status=?, error=? WHERE id=?",
                (target, note, approval.request_id),
            )
            current = self._required(connection, approval.request_id)
            connection.execute(
                """INSERT INTO execution_resolutions (
                    request_id, disposition, actor, note, created_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (approval.request_id, disposition, actor_principal_id, note, _dt(now)),
            )
            self._activity(connection, current, "resolved", "uncertain", target, note)
            attestation_id = cursor.lastrowid
            if attestation_id is None:
                raise ExecutionStoreError("browser transaction attestation was not recorded")
            attestation = BrowserTransactionAttestation(
                id=attestation_id,
                transaction_id=transaction_id,
                request_id=approval.request_id,
                disposition=disposition,
                actor_principal_id=actor_principal_id,
                source_conversation_id=source_conversation_id,
                source_message_id=source_message_id,
                note=note,
                created_at=now,
            )
            connection.commit()
        return current, attestation

    def _browser_transaction_attestations(
        self, transaction_id: str
    ) -> list[BrowserTransactionAttestation]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM execution_browser_attestations
                WHERE transaction_id=? ORDER BY id""",
                (transaction_id,),
            ).fetchall()
        return [BrowserTransactionAttestation.model_validate(dict(row)) for row in rows]

    def _required_browser_approval(
        self,
        connection: sqlite3.Connection,
        approval_id: str,
    ) -> ParkedBrowserApproval:
        row = connection.execute(
            "SELECT data_json FROM execution_browser_approvals WHERE id=?",
            (approval_id,),
        ).fetchone()
        if row is None:
            raise ExecutionNotFoundError(f"browser approval not found: {approval_id}")
        return _APPROVAL_ADAPTER.validate_json(row["data_json"])

    @staticmethod
    def _write_browser_approval(
        connection: sqlite3.Connection,
        approval: ParkedBrowserApproval,
    ) -> None:
        cursor = connection.execute(
            """UPDATE execution_browser_approvals
            SET state=?, revision=?, expires_at=?, data_json=?
            WHERE id=?""",
            (
                approval.state,
                approval.revision,
                _dt(approval.expires_at),
                _APPROVAL_ADAPTER.dump_json(approval).decode("utf-8"),
                approval.id,
            ),
        )
        if cursor.rowcount != 1:
            raise ExecutionNotFoundError(f"browser approval not found: {approval.id}")

    def _active_browser_approval(
        self,
        connection: sqlite3.Connection,
        request_id: str,
    ) -> ParkedBrowserApproval | None:
        row = connection.execute(
            """SELECT data_json FROM execution_browser_approvals
            WHERE request_id=? AND state IN ('pending','approved')
            ORDER BY rowid DESC LIMIT 1""",
            (request_id,),
        ).fetchone()
        return _APPROVAL_ADAPTER.validate_json(row["data_json"]) if row is not None else None

    def _invalidate_active_browser_approval(
        self,
        connection: sqlite3.Connection,
        request_id: str,
        *,
        reason: str,
        now: datetime,
    ) -> ParkedBrowserApproval | None:
        approval = self._active_browser_approval(connection, request_id)
        if approval is None:
            return None
        invalidated = approval.model_copy(
            update={
                "state": "invalidated",
                "revision": approval.revision + 1,
                "decided_at": now,
                "invalidation_reason": reason,
                "decision_principal_id": None,
                "decision_source_message_id": None,
            }
        )
        self._write_browser_approval(connection, invalidated)
        return invalidated

    def _list(self, status: ExecutionStatus | None, limit: int) -> list[ExecutionRequest]:
        sql = "SELECT * FROM execution_requests"
        params: list[object] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            return [_row(row) for row in connection.execute(sql, params).fetchall()]

    def _list_for_notification_projection(self) -> list[ExecutionRequest]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM execution_requests
                WHERE status IN ('succeeded','failed','blocked','cancelled','uncertain')
                ORDER BY created_at ASC, id ASC
                """
            ).fetchall()
        return [_row(row) for row in rows]

    def _list_by_conversation(
        self,
        conversation_id: str,
        statuses: tuple[ExecutionStatus, ...],
        limit: int,
    ) -> list[ExecutionRequest]:
        params: list[object] = [conversation_id]
        sql = "SELECT * FROM execution_requests WHERE source_conversation_id = ?"
        if statuses:
            sql += f" AND status IN ({','.join('?' for _ in statuses)})"
            params.extend(statuses)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [_row(row) for row in rows]

    def _list_by_source_message(self, message_id: str, limit: int) -> list[ExecutionRequest]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM execution_requests
                WHERE source_message_id = ?
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (message_id, limit),
            ).fetchall()
        return [_row(row) for row in rows]

    def _protected_message_ids(self, scope: ProfileScope) -> tuple[str, ...]:
        with self._connect() as connection:
            draft_rows = connection.execute(
                """
                SELECT s.message_id, d.data_json
                FROM execution_draft_sources AS s
                JOIN execution_drafts AS d ON d.id = s.draft_id
                """
            ).fetchall()
            request_rows = connection.execute(
                """
                SELECT source_message_id, profile_scope_json FROM execution_requests
                WHERE source_message_id IS NOT NULL
                  AND status IN (
                      'queued','claimed','running','awaiting_protected_approval',
                      'awaiting_transaction_approval','cancel_requested',
                      'uncertain','blocked','failed'
                  )
                """
            ).fetchall()
        protected = {
            str(row["message_id"])
            for row in draft_rows
            if scope.permits(
                ExecutionDraft.model_validate_json(row["data_json"]).profile_scope.label()
            )
        }
        protected.update(
            str(row["source_message_id"])
            for row in request_rows
            if scope.permits(ProfileScope.model_validate_json(row["profile_scope_json"]).label())
        )
        return tuple(sorted(protected))

    def _protected_parent_request_ids(self, scope: ProfileScope) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT data_json
                FROM execution_contracts
                WHERE json_extract(data_json, '$.parent_request_id') IS NOT NULL
                """
            ).fetchall()
        contracts = [ExecutionContract.model_validate_json(row["data_json"]) for row in rows]
        return tuple(
            sorted(
                contract.parent_request_id
                for contract in contracts
                if contract.parent_request_id is not None
                and scope.permits(contract.profile_scope.label())
            )
        )

    def _claim(
        self,
        scope: ProfileScope,
        worker_id: str,
        limit: int,
        now: datetime,
    ) -> list[ExecutionRequest]:
        self._recover_expired(scope, now)
        now_text = _dt(now)
        expiry = _dt(now + timedelta(seconds=self.settings.claim_seconds))
        claimed: list[ExecutionRequest] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM execution_requests
                WHERE status = 'queued'
                  AND (not_before IS NULL OR not_before <= ?)
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY created_at, id
                """,
                (now_text, now_text),
            ).fetchall()
            for source in rows:
                source_request = _row(source)
                if not scope.permits(source_request.profile_scope.label()):
                    continue
                token = f"claim_{uuid4().hex}"
                fence = int(source["claim_fence"]) + 1
                connection.execute(
                    """
                    UPDATE execution_requests
                    SET status='claimed', claimed_by=?, claim_token=?, claim_fence=?,
                        claim_expires_at=?, error=NULL
                    WHERE id=? AND status='queued'
                    """,
                    (worker_id, token, fence, expiry, source["id"]),
                )
                current = connection.execute(
                    "SELECT * FROM execution_requests WHERE id=?", (source["id"],)
                ).fetchone()
                request = _row(current)
                self._activity(
                    connection,
                    request,
                    "claimed",
                    "queued",
                    "claimed",
                    f"Claimed by {worker_id}",
                )
                claimed.append(request)
                if len(claimed) == limit:
                    break
            connection.commit()
        return claimed

    def _transition_claimed(
        self,
        request_id: str,
        token: str,
        fence: int,
        target: ExecutionStatus,
        kind: ExecutionActivityKind,
        run_id: str | None,
        error: str | None,
    ) -> ExecutionRequest:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = self._fenced(connection, request_id, token, fence, required="claimed")
            if target == "running":
                updates = ("status='running', run_id=?", (run_id,))
            else:
                updates = (
                    "status=?, claimed_by=NULL, claim_token=NULL, claim_expires_at=NULL, error=?",
                    (target, error),
                )
            connection.execute(
                f"UPDATE execution_requests SET {updates[0]} WHERE id=?", (*updates[1], request_id)
            )
            current = self._required(connection, request_id)
            self._activity(connection, current, kind, before.status, target, kind.replace("_", " "))
            connection.commit()
            return current

    def _renew(self, request_id: str, token: str, fence: int, now: datetime) -> ExecutionRequest:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = self._fenced(connection, request_id, token, fence)
            expiry = now + timedelta(seconds=self.settings.claim_seconds)
            connection.execute(
                "UPDATE execution_requests SET claim_expires_at=? WHERE id=?",
                (_dt(expiry), request_id),
            )
            current = self._required(connection, request_id)
            self._activity(
                connection,
                current,
                "lease_renewed",
                before.status,
                before.status,
                "Execution claim renewed",
            )
            connection.commit()
            return current

    def _finish(
        self,
        request_id: str,
        token: str,
        fence: int,
        status: ExecutionStatus,
        error: str | None,
    ) -> ExecutionRequest:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = self._fenced(connection, request_id, token, fence)
            if before.status not in {"running", *_PARKED, "cancel_requested"}:
                raise ExecutionFenceError(
                    f"execution is {before.status}; expected active run or cancel_requested"
                )
            if before.status in _PARKED:
                approval = self._active_browser_approval(connection, request_id)
                if approval is None or approval.state in {"pending", "approved"}:
                    raise BrowserApprovalError(
                        "parked execution cannot finish before its approval is settled"
                    )
            if before.status == "cancel_requested" and status not in {"cancelled", "uncertain"}:
                raise ExecutionFenceError(
                    "a cancellation request can settle only as cancelled or uncertain"
                )
            connection.execute(
                """
                UPDATE execution_requests
                SET status=?, claimed_by=NULL, claim_token=NULL, claim_expires_at=NULL, error=?
                WHERE id=?
                """,
                (status, error[:2_000] if error else None, request_id),
            )
            current = self._required(connection, request_id)
            kind = cast(
                ExecutionActivityKind,
                {
                    "succeeded": "succeeded",
                    "failed": "failed",
                    "blocked": "blocked",
                    "cancelled": "cancelled",
                    "uncertain": "uncertain",
                }[status],
            )
            self._activity(
                connection, current, kind, before.status, status, error or f"Execution {status}"
            )
            connection.commit()
            return current

    def _cancel(self, request_id: str) -> ExecutionRequest:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = self._required(connection, request_id)
            if before.status in _TERMINAL:
                if before.status == "cancelled":
                    connection.commit()
                    return before
                raise ExecutionStoreError(f"cannot cancel execution in {before.status} state")
            target: ExecutionStatus
            kind: ExecutionActivityKind
            if before.status in {"running", *_PARKED}:
                target = "cancel_requested"
                kind = "cancel_requested"
                if before.status in _PARKED:
                    self._invalidate_active_browser_approval(
                        connection,
                        request_id,
                        reason="execution cancelled while awaiting approval",
                        now=datetime.now(UTC),
                    )
                connection.execute(
                    """
                    UPDATE execution_requests
                    SET status='cancel_requested', error='cancellation requested by user'
                    WHERE id=?
                    """,
                    (request_id,),
                )
            elif before.status == "cancel_requested":
                connection.commit()
                return before
            else:
                target = "cancelled"
                kind = "cancelled"
                connection.execute(
                    """
                    UPDATE execution_requests
                    SET status='cancelled', claimed_by=NULL, claim_token=NULL,
                        claim_expires_at=NULL, error='cancelled by user'
                    WHERE id=?
                    """,
                    (request_id,),
                )
            current = self._required(connection, request_id)
            self._activity(
                connection,
                current,
                kind,
                before.status,
                target,
                (
                    "Cancellation requested by user"
                    if target == "cancel_requested"
                    else "Cancelled by user"
                ),
            )
            connection.commit()
            return current

    def _retry(self, request_id: str, created_at: datetime) -> ExecutionRequest:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            original = self._required(connection, request_id)
            if not is_retryable_execution_status(original.status):
                if original.status == "uncertain":
                    raise ExecutionStoreError(
                        "uncertain execution must be resolved before it can be retried"
                    )
                raise ExecutionStoreError("execution status is not retryable")
            child = original.model_copy(
                update={
                    "id": f"execution_{uuid4().hex}",
                    "status": "queued",
                    "request_key": f"{original.request_key}:retry:{uuid4().hex}",
                    "parent_request_id": original.id,
                    "created_at": created_at,
                    "not_before": None,
                    "claimed_by": None,
                    "claim_token": None,
                    "claim_expires_at": None,
                    "run_id": None,
                    "error": None,
                }
            )
            connection.execute(
                f"INSERT INTO execution_requests ({','.join(_COLUMNS)}) "
                f"VALUES ({','.join('?' for _ in _COLUMNS)})",
                _values(child),
            )
            self._activity(
                connection, child, "submitted", None, "queued", f"Retry of {original.id}"
            )
            self._activity(
                connection,
                original,
                "retried",
                original.status,
                original.status,
                f"Retried as {child.id}",
            )
            connection.commit()
            return child

    def _resolve(
        self,
        request_id: str,
        disposition: ExecutionResolutionDisposition,
        actor: str,
        note: str,
        created_at: datetime,
    ) -> ExecutionRequest:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = self._required(connection, request_id)
            if before.status != "uncertain":
                raise ExecutionStoreError("only uncertain executions can be resolved")
            target: ExecutionStatus = (
                "succeeded" if disposition == "confirmed_completed" else "failed"
            )
            connection.execute(
                "UPDATE execution_requests SET status=?, error=? WHERE id=?",
                (target, note[:2_000], request_id),
            )
            connection.execute(
                """
                INSERT INTO execution_resolutions
                    (request_id, disposition, actor, note, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (request_id, disposition, actor[:200], note[:2_000], _dt(created_at)),
            )
            current = self._required(connection, request_id)
            self._activity(connection, current, "resolved", "uncertain", target, note)
            connection.commit()
            return current

    def _recover_expired(
        self,
        scope: ProfileScope,
        now: datetime,
    ) -> list[ExecutionRequest]:
        recovered: list[ExecutionRequest] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM execution_requests
                WHERE status IN (
                    'claimed','running','awaiting_protected_approval',
                    'awaiting_transaction_approval','cancel_requested'
                )
                  AND claim_expires_at <= ?
                """,
                (_dt(now),),
            ).fetchall()
            for row in rows:
                before = _row(row)
                if not scope.permits(before.profile_scope.label()):
                    continue
                target: ExecutionStatus
                if before.status == "claimed":
                    target = "queued"
                elif before.status in _PARKED:
                    target = "blocked"
                    self._invalidate_active_browser_approval(
                        connection,
                        before.id,
                        reason="browser owner lease expired while awaiting approval",
                        now=now,
                    )
                else:
                    target = "uncertain"
                kind: ExecutionActivityKind = "reclaimed" if target == "queued" else "uncertain"
                if target == "blocked":
                    kind = "blocked"
                error = (
                    None
                    if target == "queued"
                    else (
                        "cancellation request was not settled before the worker lease expired"
                        if before.status == "cancel_requested"
                        else (
                            "live browser approval invalidated after owner lease expired"
                            if before.status in _PARKED
                            else "worker lease expired after run started"
                        )
                    )
                )
                connection.execute(
                    """
                    UPDATE execution_requests
                    SET status=?, claimed_by=NULL, claim_token=NULL,
                        claim_expires_at=NULL, error=?
                    WHERE id=?
                    """,
                    (target, error, before.id),
                )
                current = self._required(connection, before.id)
                self._activity(
                    connection,
                    current,
                    kind,
                    before.status,
                    target,
                    error or "Expired pre-run claim safely requeued",
                )
                recovered.append(current)
            connection.commit()
        return recovered

    def _counts(self, scope: ProfileScope) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM execution_requests").fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            request = _row(row)
            if scope.permits(request.profile_scope.label()):
                counts[request.status] = counts.get(request.status, 0) + 1
        return counts

    def _prunable(self, scope: ProfileScope, keep: int, before: datetime) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM execution_requests
                WHERE status IN ('succeeded','cancelled') AND created_at < ?
                ORDER BY created_at DESC, id DESC
                """,
                (_dt(before),),
            ).fetchall()
        permitted = [
            request for row in rows if scope.permits((request := _row(row)).profile_scope.label())
        ]
        return sorted(request.id for request in permitted[keep:])

    def _prune(self, scope: ProfileScope, request_ids: tuple[str, ...]) -> int:
        if not request_ids:
            return 0
        removed = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for request_id in request_ids:
                row = connection.execute(
                    "SELECT * FROM execution_requests WHERE id=?", (request_id,)
                ).fetchone()
                if row is None:
                    continue
                request = _row(row)
                _require_profile_access(
                    scope,
                    request.profile_scope,
                    "execution request",
                    request_id,
                )
                if request.status not in {"succeeded", "cancelled"}:
                    continue
                connection.execute(
                    "DELETE FROM execution_activities WHERE request_id=?", (request_id,)
                )
                connection.execute(
                    "DELETE FROM execution_resolutions WHERE request_id=?", (request_id,)
                )
                connection.execute(
                    "DELETE FROM execution_browser_attestations WHERE request_id=?", (request_id,)
                )
                connection.execute(
                    "DELETE FROM execution_browser_approvals WHERE request_id=?", (request_id,)
                )
                connection.execute("DELETE FROM execution_requests WHERE id=?", (request_id,))
                removed += 1
            connection.commit()
        return removed

    def _find_by_task(self, task_id: str, limit: int) -> list[ExecutionRequest]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM execution_requests
                WHERE task_id=? ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (task_id, limit),
            ).fetchall()
        return [_row(row) for row in rows]

    def _activities(
        self,
        request_id: str,
        profile_scope: ProfileScope,
        limit: int,
    ) -> list[ExecutionActivity]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM execution_activities
                WHERE request_id=? ORDER BY id DESC LIMIT ?
                """,
                (request_id, limit),
            ).fetchall()
        label = profile_scope.label()
        return [
            ExecutionActivity.model_validate({**dict(row), "profile_label": label}) for row in rows
        ]

    def _resolutions(
        self,
        request_id: str,
        profile_scope: ProfileScope,
    ) -> list[ExecutionResolution]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM execution_resolutions WHERE request_id=? ORDER BY id",
                (request_id,),
            ).fetchall()
        label = profile_scope.label()
        return [
            ExecutionResolution.model_validate({**dict(row), "profile_label": label})
            for row in rows
        ]

    def _fenced(
        self,
        connection: sqlite3.Connection,
        request_id: str,
        token: str,
        fence: int,
        *,
        required: ExecutionStatus | None = None,
    ) -> ExecutionRequest:
        current = self._required(connection, request_id)
        if required is not None and current.status != required:
            raise ExecutionFenceError(f"execution is {current.status}; expected {required}")
        if current.status not in _ACTIVE:
            raise ExecutionFenceError("execution is not actively claimed")
        if current.claim_token != token or current.claim_fence != fence:
            raise ExecutionFenceError("stale execution claim")
        return current

    def _required(self, connection: sqlite3.Connection, request_id: str) -> ExecutionRequest:
        row = connection.execute(
            "SELECT * FROM execution_requests WHERE id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise ExecutionNotFoundError(f"execution request not found: {request_id}")
        return _row(row)

    def _activity(
        self,
        connection: sqlite3.Connection,
        request: ExecutionRequest,
        kind: ExecutionActivityKind,
        from_status: ExecutionStatus | None,
        to_status: ExecutionStatus,
        summary: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO execution_activities
                (request_id, kind, from_status, to_status, worker_id, summary, fence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request.id,
                kind,
                from_status,
                to_status,
                request.claimed_by,
                summary[:2_000],
                request.claim_fence,
                _dt(datetime.now(UTC)),
            ),
        )


_COLUMNS = (
    "id",
    "kind",
    "status",
    "named_job",
    "job_digest",
    "project_root_ref",
    "goal",
    "contract_id",
    "contract_digest",
    "task_id",
    "task_revision",
    "profile_scope_json",
    "source_conversation_id",
    "source_message_id",
    "grant_id",
    "notification_route",
    "request_key",
    "parent_request_id",
    "created_at",
    "not_before",
    "expires_at",
    "claimed_by",
    "claim_token",
    "claim_fence",
    "claim_expires_at",
    "run_id",
    "error",
)


def _values(request: ExecutionRequest) -> tuple[Any, ...]:
    values = request.model_dump(mode="python")
    values["profile_scope_json"] = request.profile_scope.model_dump_json()
    return tuple(
        _dt(values[name]) if isinstance(values[name], datetime) else values[name]
        for name in _COLUMNS
    )


def _permitted[ScopedT: _ProfileScoped](
    scope: ProfileScope,
    values: Sequence[ScopedT],
) -> list[ScopedT]:
    return [value for value in values if scope.permits(value.profile_scope.label())]


def _require_profile_access(
    caller: ProfileScope,
    stored: ProfileScope,
    kind: str,
    identifier: str,
) -> None:
    if not caller.permits(stored.label()):
        raise ExecutionNotFoundError(f"{kind} not found: {identifier}")


def _same_submission(existing: ExecutionRequest, proposed: ExecutionRequest) -> bool:
    immutable = (
        "kind",
        "named_job",
        "job_digest",
        "project_root_ref",
        "goal",
        "contract_id",
        "contract_digest",
        "task_id",
        "task_revision",
        "profile_scope",
        "source_conversation_id",
        "source_message_id",
        "grant_id",
        "notification_route",
        "request_key",
        "parent_request_id",
        "not_before",
        "expires_at",
    )
    return all(getattr(existing, name) == getattr(proposed, name) for name in immutable)


def _row(row: sqlite3.Row) -> ExecutionRequest:
    values = dict(row)
    values["profile_scope"] = json.loads(values.pop("profile_scope_json"))
    for name in ("created_at", "not_before", "expires_at", "claim_expires_at"):
        if values[name] is not None:
            values[name] = datetime.fromisoformat(values[name])
    return ExecutionRequest.model_validate(values)


def _dt(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


_SCHEMA = """
CREATE TABLE execution_requests (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    named_job TEXT,
    job_digest TEXT,
    project_root_ref TEXT,
    goal TEXT,
    contract_id TEXT,
    contract_digest TEXT,
    task_id TEXT,
    task_revision INTEGER,
    profile_scope_json TEXT NOT NULL,
    source_conversation_id TEXT,
    source_message_id TEXT,
    grant_id TEXT,
    notification_route TEXT NOT NULL,
    request_key TEXT NOT NULL UNIQUE,
    parent_request_id TEXT REFERENCES execution_requests(id),
    created_at TEXT NOT NULL,
    not_before TEXT,
    expires_at TEXT,
    claimed_by TEXT,
    claim_token TEXT,
    claim_fence INTEGER NOT NULL DEFAULT 0,
    claim_expires_at TEXT,
    run_id TEXT,
    error TEXT
);
CREATE INDEX execution_queue_idx
ON execution_requests(status, not_before, created_at);
CREATE TABLE execution_activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL REFERENCES execution_requests(id),
    kind TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    worker_id TEXT,
    summary TEXT NOT NULL,
    fence INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE execution_resolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL REFERENCES execution_requests(id),
    disposition TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE execution_drafts (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    data_json TEXT NOT NULL
);
CREATE INDEX execution_drafts_open_idx
ON execution_drafts(conversation_id, task_id, status, updated_at);
CREATE TABLE execution_draft_sources (
    draft_id TEXT NOT NULL REFERENCES execution_drafts(id),
    message_id TEXT NOT NULL,
    source_json TEXT NOT NULL,
    PRIMARY KEY (draft_id, message_id)
);
CREATE TABLE execution_draft_guardrail_fields (
    draft_id TEXT NOT NULL REFERENCES execution_drafts(id),
    capability_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    field_json TEXT NOT NULL,
    PRIMARY KEY (draft_id, capability_id, field_name)
);
CREATE TABLE execution_draft_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id TEXT NOT NULL REFERENCES execution_drafts(id),
    kind TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    revision INTEGER NOT NULL,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE execution_contracts (
    id TEXT PRIMARY KEY,
    digest TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    data_json TEXT NOT NULL
);
CREATE TABLE execution_contract_confirmations (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES execution_drafts(id),
    draft_revision INTEGER NOT NULL,
    summary_digest TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    data_json TEXT NOT NULL
);
CREATE TABLE execution_browser_approvals (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES execution_requests(id),
    kind TEXT NOT NULL CHECK(kind IN ('browser_transaction','protected_destination')),
    state TEXT NOT NULL CHECK(state IN
        ('pending','approved','denied','expired','invalidated','consumed')),
    revision INTEGER NOT NULL,
    logical_effect_key TEXT,
    expires_at TEXT NOT NULL,
    data_json TEXT NOT NULL
);
CREATE UNIQUE INDEX execution_browser_active_approval_idx
ON execution_browser_approvals(request_id)
WHERE state IN ('pending','approved');
CREATE UNIQUE INDEX execution_browser_consumed_effect_idx
ON execution_browser_approvals(logical_effect_key)
WHERE logical_effect_key IS NOT NULL AND state='consumed';
CREATE TABLE execution_browser_attestations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id TEXT NOT NULL REFERENCES execution_browser_approvals(id),
    request_id TEXT NOT NULL REFERENCES execution_requests(id),
    disposition TEXT NOT NULL CHECK(disposition IN
        ('confirmed_completed','confirmed_not_completed')),
    actor_principal_id TEXT NOT NULL,
    source_conversation_id TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


_BROWSER_APPROVAL_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE execution_browser_approvals (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES execution_requests(id),
    kind TEXT NOT NULL CHECK(kind IN ('browser_transaction','protected_destination')),
    state TEXT NOT NULL CHECK(state IN
        ('pending','approved','denied','expired','invalidated','consumed')),
    revision INTEGER NOT NULL,
    logical_effect_key TEXT,
    expires_at TEXT NOT NULL,
    data_json TEXT NOT NULL
);
CREATE UNIQUE INDEX execution_browser_active_approval_idx
ON execution_browser_approvals(request_id)
WHERE state IN ('pending','approved');
CREATE UNIQUE INDEX execution_browser_consumed_effect_idx
ON execution_browser_approvals(logical_effect_key)
WHERE logical_effect_key IS NOT NULL AND state='consumed';
CREATE TABLE execution_browser_attestations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id TEXT NOT NULL REFERENCES execution_browser_approvals(id),
    request_id TEXT NOT NULL REFERENCES execution_requests(id),
    disposition TEXT NOT NULL CHECK(disposition IN
        ('confirmed_completed','confirmed_not_completed')),
    actor_principal_id TEXT NOT NULL,
    source_conversation_id TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);
COMMIT;
"""


_EVIDENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_drafts (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS execution_drafts_open_idx
ON execution_drafts(conversation_id, task_id, status, updated_at);
CREATE TABLE IF NOT EXISTS execution_draft_sources (
    draft_id TEXT NOT NULL REFERENCES execution_drafts(id),
    message_id TEXT NOT NULL,
    source_json TEXT NOT NULL,
    PRIMARY KEY (draft_id, message_id)
);
CREATE TABLE IF NOT EXISTS execution_draft_guardrail_fields (
    draft_id TEXT NOT NULL REFERENCES execution_drafts(id),
    capability_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    field_json TEXT NOT NULL,
    PRIMARY KEY (draft_id, capability_id, field_name)
);
CREATE TABLE IF NOT EXISTS execution_draft_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id TEXT NOT NULL REFERENCES execution_drafts(id),
    kind TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    revision INTEGER NOT NULL,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS execution_contracts (
    id TEXT PRIMARY KEY,
    digest TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    data_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS execution_contract_confirmations (
    id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL REFERENCES execution_drafts(id),
    draft_revision INTEGER NOT NULL,
    summary_digest TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    data_json TEXT NOT NULL
);
"""
