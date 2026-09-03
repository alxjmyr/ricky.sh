"""Runtime-only projection and policy hooks for parked browser effects."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import uuid4

from ricky.browser.authority import browser_money_minor_units
from ricky.browser.tools import (
    PreparedBrowserCommit,
    PreparedBrowserCoordinateCommit,
)
from ricky.browser.types import BrowserFinancialTransactionEnvelope
from ricky.executions.browser import (
    BrowserApprovalDraft,
    BrowserAttachmentPin,
    BrowserExecutionBudget,
    BrowserLiveBinding,
    BrowserProtectedUseEvidence,
    BrowserResourceKind,
    BrowserTransactionApprovalDraft,
    CoordinateFallbackBinding,
    ParkedBrowserApproval,
    ParkedBrowserTransaction,
    ProtectedDestinationApprovalDraft,
)
from ricky.executions.parked import ParkedPreparedEffectTool
from ricky.executions.store import BrowserApprovalError, ExecutionStore, ExecutionStoreError
from ricky.jobs.browser_store import BrowserRunLedger
from ricky.profiles import ProfileResourceRef, ProfileScope
from ricky.protected_values import (
    DestinationApprovalRequest,
    DestinationApprovalResponse,
    ProtectedCommitRecord,
    ProtectedCommitRequest,
    ProtectedValueBroker,
)
from ricky.tools import PreparedEffect, Tool, ToolContext


@dataclass(frozen=True)
class BackgroundBrowserApprovalContext:
    """Trusted execution identities used to project exact live approvals."""

    request_id: str
    task_id: str
    contract_digest: str
    run_id: str
    attempt_id: str
    claim_token: str
    claim_fence: int
    principal_id: str
    conversation_id: str
    source_message_id: str
    approval_ttl_seconds: int
    profile_scope: ProfileScope
    owner_token: str
    provider: str
    resource: ProfileResourceRef | None
    resource_kind: BrowserResourceKind
    resource_configuration_digest: str | None
    budget_ceiling: BrowserExecutionBudget
    attachments: tuple[BrowserAttachmentPin, ...] = ()


class BackgroundBrowserApprovalCoordinator:
    """Bind parked approvals, live attempt state, and protected commit policy."""

    def __init__(
        self,
        *,
        context: BackgroundBrowserApprovalContext,
        store: ExecutionStore,
        ledger: BrowserRunLedger,
        notifier,
        protected_values: ProtectedValueBroker | None,
    ) -> None:
        self.context = context
        self.store = store
        self.ledger = ledger
        self.notifier = notifier
        self.protected_values = protected_values
        self._transaction_sequence = 0

    def wrap(self, tool: Tool) -> Tool:
        """Park only final transaction commit tools before shared effect reservation."""

        if tool.name not in {"browser_commit", "browser_coordinate_commit"}:
            return tool

        async def revalidate(
            approval: ParkedBrowserApproval,
            prepared: PreparedEffect,
            ctx: ToolContext,
        ) -> bool:
            if not isinstance(approval, ParkedBrowserTransaction):
                return False
            hook = getattr(tool, "revalidate_prepared", None)
            if hook is None:
                return False
            return bool(await hook(prepared, ctx))

        return cast(
            Tool,
            ParkedPreparedEffectTool(
                tool,
                store=self.store,
                scope=self.context.profile_scope,
                claim_token=self.context.claim_token,
                claim_fence=self.context.claim_fence,
                projector=self.project,
                notifier=self.notifier,
                revalidator=revalidate,
                park_reserver=self.reserve_park,
                park_releaser=self.release_park,
                dispatch_authorizer=self.authorize_protected_commit,
                dispatch_finalizer=self.finalize_protected_commit,
            ),
        )

    def project(
        self,
        prepared: PreparedEffect,
        _args: dict[str, object],
        _ctx: ToolContext,
    ) -> BrowserApprovalDraft:
        if not isinstance(
            prepared,
            (PreparedBrowserCommit, PreparedBrowserCoordinateCommit),
        ):
            raise ValueError("parked browser approval requires a prepared commit")
        now = datetime.now(UTC)
        identity = prepared.identity
        prepared_digest = _digest(identity.model_dump(mode="json"))
        protected = tuple(
            BrowserProtectedUseEvidence(
                resource=item.resource,
                revision=item.revision,
                field=item.field,
            )
            for item in prepared.prepared.protected_uses
        )
        attachment_bindings = prepared.prepared.attachments
        pins = {item.id: item for item in self.context.attachments}
        try:
            attachments = tuple(
                pins[item.id]
                for item in attachment_bindings
                if (
                    pins[item.id].sha256 == item.sha256
                    and pins[item.id].byte_count == item.byte_count
                )
            )
        except KeyError as exc:
            raise ValueError("prepared upload differs from the execution attachment pins") from exc
        if len(attachments) != len(attachment_bindings):
            raise ValueError("prepared upload differs from the execution attachment pins")
        coordinate = (
            _coordinate_binding(prepared)
            if isinstance(prepared, PreparedBrowserCoordinateCommit)
            else None
        )
        binding = _live_binding(prepared, prepared_digest, self.context)
        review_digest = _digest(
            {
                "permission_summary": prepared.permission_summary,
                "binding": binding.model_dump(mode="json"),
                "target_mode": "coordinate" if coordinate is not None else "semantic",
                "coordinate": (
                    coordinate.model_dump(mode="json") if coordinate is not None else None
                ),
                "envelope": prepared.envelope.model_dump(mode="json"),
                "attachments": [item.model_dump(mode="json") for item in attachments],
                "protected_uses": [item.model_dump(mode="json") for item in protected],
            }
        )
        sequence = self._transaction_sequence + 1
        logical_id, logical_effect_key = _logical_transaction_identity(
            task_id=self.context.task_id,
            contract_digest=self.context.contract_digest,
            sequence=sequence,
            operation=identity.operation,
            envelope_digest=prepared.envelope_sha256,
        )
        draft = BrowserTransactionApprovalDraft(
            id=f"browser_transaction_{uuid4().hex}",
            request_id=self.context.request_id,
            run_id=self.context.run_id,
            attempt_id=self.context.attempt_id,
            claim_fence=self.context.claim_fence,
            prepared_effect_digest=prepared_digest,
            logical_effect_key=logical_effect_key,
            review_digest=review_digest,
            binding=binding,
            principal_id=self.context.principal_id,
            conversation_id=self.context.conversation_id,
            proposal_source_message_id=self.context.source_message_id,
            created_at=now,
            expires_at=now + timedelta(seconds=self.context.approval_ttl_seconds),
            logical_transaction_id=logical_id,
            target_mode="coordinate" if coordinate is not None else "semantic",
            envelope=prepared.envelope,
            envelope_digest=prepared.envelope_sha256,
            coordinate=coordinate,
            attachments=attachments,
            protected_uses=protected,
        )
        self._transaction_sequence = sequence
        return draft

    async def reserve_park(self, _draft: BrowserApprovalDraft) -> None:
        await self.ledger.reserve_budget(
            self.context.attempt_id,
            "parked_browsers",
            scope=self.context.profile_scope,
            owner_token=self.context.owner_token,
            claim_fence=self.context.claim_fence,
        )
        try:
            await self.ledger.transition(
                self.context.attempt_id,
                scope=self.context.profile_scope,
                owner_token=self.context.owner_token,
                claim_fence=self.context.claim_fence,
                status="parked",
            )
        except BaseException:
            await self.ledger.release_live_budget(
                self.context.attempt_id,
                "parked_browsers",
                scope=self.context.profile_scope,
                owner_token=self.context.owner_token,
                claim_fence=self.context.claim_fence,
            )
            raise

    async def release_park(self, _approval: BrowserApprovalDraft | ParkedBrowserApproval) -> None:
        transition_error: BaseException | None = None
        try:
            await self.ledger.transition(
                self.context.attempt_id,
                scope=self.context.profile_scope,
                owner_token=self.context.owner_token,
                claim_fence=self.context.claim_fence,
                status="running",
            )
        except BaseException as exc:
            transition_error = exc
        await self.ledger.release_live_budget(
            self.context.attempt_id,
            "parked_browsers",
            scope=self.context.profile_scope,
            owner_token=self.context.owner_token,
            claim_fence=self.context.claim_fence,
        )
        if transition_error is not None:
            raise transition_error

    async def approve_destination(
        self,
        request: DestinationApprovalRequest,
    ) -> DestinationApprovalResponse:
        """Park and consume one exact execution-local protected destination approval."""

        if request.execution_mode != "unattended":
            return DestinationApprovalResponse(decision="deny")
        if request.binding is None:
            return DestinationApprovalResponse(decision="deny")
        now = datetime.now(UTC)
        payload = request.model_dump(mode="json")
        prepared_digest = _digest(payload)
        logical_key = _digest(
            {
                "execution": self.context.request_id,
                "occurrence": request.occurrence,
                "resource": request.ref.qualified,
                "revision": request.revision,
                "field": request.field,
                "origins": [request.top_level_origin, request.frame_origin],
            }
        )
        draft = ProtectedDestinationApprovalDraft(
            id=f"browser_destination_{uuid4().hex}",
            request_id=self.context.request_id,
            run_id=self.context.run_id,
            attempt_id=self.context.attempt_id,
            claim_fence=self.context.claim_fence,
            prepared_effect_digest=prepared_digest,
            logical_effect_key=logical_key,
            review_digest=_digest(
                {
                    "label": request.label,
                    "resource": request.ref.qualified,
                    "revision": request.revision,
                    "field": request.field,
                    "top_level_origin": request.top_level_origin,
                    "frame_origin": request.frame_origin,
                    "binding": request.binding.model_dump(mode="json"),
                }
            ),
            binding=BrowserLiveBinding(
                occurrence_digest=_digest(request.occurrence),
                page_generation=request.binding.generation,
                snapshot_digest=_digest(request.binding.observation_id),
                target_digest=_digest(
                    {
                        "occurrence": request.occurrence,
                        "target": request.binding.target_id,
                        "field": request.field,
                    }
                ),
                target_description=f"protected field {request.field}",
                top_level_origin=request.top_level_origin,
                target_frame_origin=request.frame_origin,
            ),
            principal_id=self.context.principal_id,
            conversation_id=self.context.conversation_id,
            proposal_source_message_id=self.context.source_message_id,
            created_at=now,
            expires_at=now + timedelta(seconds=self.context.approval_ttl_seconds),
            protected_use=BrowserProtectedUseEvidence(
                resource=request.ref,
                revision=request.revision,
                field=request.field,
            ),
        )
        await _join_on_cancel(self.reserve_park(draft))
        approval: ParkedBrowserApproval | None = None
        try:
            try:
                challenge = await self.store.park_browser_approval(
                    draft,
                    scope=self.context.profile_scope,
                    token=self.context.claim_token,
                    fence=self.context.claim_fence,
                )
            except BaseException:
                with suppress(ExecutionStoreError):
                    approval = await self.store.get_browser_approval(
                        draft.id,
                        scope=self.context.profile_scope,
                    )
                if approval is not None:
                    await self._invalidate_destination(
                        approval.id,
                        "protected destination parking was interrupted",
                    )
                raise
            approval = challenge.approval
            try:
                await self.notifier(challenge)
            except BaseException:
                await self._invalidate_destination(
                    approval.id,
                    "protected destination notification failed",
                )
                raise
            try:
                approval = await self.store.wait_for_browser_approval(
                    approval.id,
                    scope=self.context.profile_scope,
                )
            except BaseException:
                await self._invalidate_destination(
                    approval.id,
                    "protected destination wait was interrupted",
                )
                raise
            if approval.state != "approved":
                if approval.state != "pending":
                    await self.store.resume_browser_approval(
                        approval.id,
                        scope=self.context.profile_scope,
                        token=self.context.claim_token,
                        fence=self.context.claim_fence,
                        consume=False,
                    )
                return DestinationApprovalResponse(decision="deny")
            approval = await self.store.resume_browser_approval(
                approval.id,
                scope=self.context.profile_scope,
                token=self.context.claim_token,
                fence=self.context.claim_fence,
                consume=True,
            )
            return DestinationApprovalResponse(
                decision="allow_once" if approval.state == "consumed" else "deny"
            )
        finally:
            await _join_on_cancel(self.release_park(approval or draft))

    async def _invalidate_destination(self, approval_id: str, reason: str) -> None:
        with suppress(BrowserApprovalError, ExecutionStoreError):
            await self.store.invalidate_browser_approval(
                approval_id,
                scope=self.context.profile_scope,
                token=self.context.claim_token,
                fence=self.context.claim_fence,
                reason=reason,
            )

    async def authorize_protected_commit(
        self,
        approval: ParkedBrowserApproval,
        prepared: PreparedEffect,
    ) -> object | None:
        if not isinstance(approval, ParkedBrowserTransaction) or not isinstance(
            prepared,
            (PreparedBrowserCommit, PreparedBrowserCoordinateCommit),
        ):
            raise ValueError("protected commit authorization has another occurrence")
        if not approval.protected_uses:
            return None
        if self.protected_values is None:
            raise ValueError("protected-value broker is unavailable")
        envelope = prepared.envelope
        records: list[ProtectedCommitRecord] = []
        try:
            for request in _protected_commit_requests(
                execution_id=self.context.request_id,
                approval=approval,
                envelope=envelope,
            ):
                records.append(await self.protected_values.reserve_commit(request))
        except BaseException:
            for record in records:
                await self.protected_values.finalize_commit(
                    record,
                    disposition="not_performed",
                )
            raise
        return tuple(records) or None

    async def finalize_protected_commit(self, authorization: object, disposition: str) -> None:
        if self.protected_values is None or not isinstance(authorization, tuple):
            return
        final = cast(
            Literal["performed", "not_performed", "in_doubt"],
            disposition
            if disposition in {"performed", "not_performed", "in_doubt"}
            else "in_doubt",
        )
        for record in authorization:
            if not isinstance(record, ProtectedCommitRecord):
                raise TypeError("invalid protected commit authorization")
            await self.protected_values.finalize_commit(
                record,
                disposition=final,
            )


def _protected_commit_requests(
    *,
    execution_id: str,
    approval: ParkedBrowserTransaction,
    envelope,
) -> tuple[ProtectedCommitRequest, ...]:
    """Bind every materialized protected resource to the final commit policy."""

    selected = approval.protected_uses
    amount_minor: int | None = None
    currency: str | None = None
    if isinstance(envelope, BrowserFinancialTransactionEnvelope):
        if envelope.source.kind == "protected_value" and not any(
            item.resource == envelope.source.protected_value for item in selected
        ):
            raise ValueError("approved protected funding source lacks live fill evidence")
        amount_minor = browser_money_minor_units(
            envelope.total.amount,
            envelope.total.currency,
        )
        currency = envelope.total.currency
    grouped: dict[tuple[str, int], list[str]] = {}
    by_key: dict[tuple[str, int], ProfileResourceRef] = {}
    for item in selected:
        key = (item.resource.qualified, item.revision)
        by_key[key] = item.resource
        grouped.setdefault(key, []).append(item.field)
    return tuple(
        ProtectedCommitRequest(
            execution_id=execution_id,
            ref=by_key[key],
            revision=key[1],
            fields=tuple(sorted(set(grouped[key]))),
            logical_effect_key=approval.logical_effect_key,
            envelope_sha256=approval.envelope_digest,
            envelope_kind=approval.envelope.kind,
            amount_minor=amount_minor,
            currency=currency,
        )
        for key in sorted(grouped)
    )


def _logical_transaction_identity(
    *,
    task_id: str,
    contract_digest: str,
    sequence: int,
    operation: str,
    envelope_digest: str,
) -> tuple[str, str]:
    """Issue a restart-stable transaction id and exact no-replay effect key.

    The sequence is local to one execution occurrence. Explicit execution retries
    start at one again, so the same task and pinned contract cannot manufacture a
    fresh identity merely by reopening Chromium. A second intentional commit in
    the same live execution receives the next sequence value.
    """

    if sequence < 1:
        raise ValueError("logical browser transaction sequence must be positive")
    if len(contract_digest) != 64 or any(
        character not in "0123456789abcdef" for character in contract_digest
    ):
        raise ValueError("logical browser transaction requires a contract digest")
    logical_digest = _digest(
        {
            "version": 1,
            "task_id": task_id,
            "contract_digest": contract_digest,
            "sequence": sequence,
        }
    )
    logical_id = f"browser_logical_{logical_digest[:32]}"
    return logical_id, _digest(
        {
            "version": 1,
            "task_id": task_id,
            "contract_digest": contract_digest,
            "logical_transaction_id": logical_id,
            "operation": operation,
            "envelope_digest": envelope_digest,
        }
    )


def _live_binding(
    prepared: PreparedBrowserCommit | PreparedBrowserCoordinateCommit,
    occurrence_digest: str,
    context: BackgroundBrowserApprovalContext,
) -> BrowserLiveBinding:
    transaction = prepared.transaction
    if isinstance(prepared, PreparedBrowserCommit):
        frozen = prepared.prepared
        snapshot_id = frozen.target.snapshot_id
    else:
        frozen = prepared.prepared
        snapshot_id = frozen.target.screenshot_id
    target = frozen.preflight.target
    if context.resource_kind == "persistent":
        if context.resource is None or frozen.context.resource != context.resource:
            raise ValueError("prepared browser resource differs from the execution resource pin")
        review_resource = context.resource
    else:
        if context.resource is not None or context.resource_configuration_digest is not None:
            raise ValueError("ephemeral browser execution carries persistent resource facts")
        review_resource = None
    description = json.dumps(
        target.provider_descriptor().model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )[:1_000]
    return BrowserLiveBinding(
        occurrence_digest=occurrence_digest,
        resource=review_resource,
        resource_digest=_digest(frozen.context.resource.qualified),
        resource_kind=context.resource_kind,
        resource_configuration_digest=context.resource_configuration_digest,
        provider=context.provider,
        session_digest=_digest(frozen.target.session_id),
        page_digest=_digest(frozen.target.page_id),
        budget_ceiling=context.budget_ceiling,
        page_generation=frozen.context.navigation_generation,
        snapshot_digest=_digest(snapshot_id),
        target_digest=_digest(
            {
                "public": target.provider_descriptor().model_dump(mode="json"),
                "frame": target.frame_key,
            }
        ),
        target_description=description or "browser target",
        top_level_origin=transaction.top_level_origin,
        target_frame_origin=transaction.target_frame_origin,
        destination_projections=transaction.effective_destinations,
    )


def _coordinate_binding(
    prepared: PreparedBrowserCoordinateCommit,
) -> CoordinateFallbackBinding:
    frozen = prepared.prepared
    fallback = frozen.fallback
    if fallback is None:
        raise ValueError("background coordinate commit lacks fallback evidence")
    reason = cast(
        Literal[
            "no_semantic_target",
            "position_sensitive_surface",
            "semantic_preflight_not_performed",
        ],
        {
            "no_supported_semantic_target": "no_semantic_target",
            "custom_rendered_target": "position_sensitive_surface",
            "semantic_preflight_not_dispatched": "semantic_preflight_not_performed",
        }[fallback.reason],
    )
    target_digest = _digest(
        {
            "public": frozen.preflight.target.provider_descriptor().model_dump(mode="json"),
            "frame": frozen.preflight.target.frame_key,
        }
    )
    return CoordinateFallbackBinding(
        reason=reason,
        semantic_resolution_digest=_digest(fallback.model_dump(mode="json")),
        equivalent_semantic_target=None,
        masked_image_digest=frozen.context.masked_base_sha256,
        visual_snapshot_digest=_digest(frozen.target.screenshot_id),
        x=frozen.context.css_x,
        y=frozen.context.css_y,
        viewport_width=frozen.viewport.width,
        viewport_height=frozen.viewport.height,
        scroll_x=frozen.viewport.scroll_x,
        scroll_y=frozen.viewport.scroll_y,
        coordinate_scale=(frozen.context.image_width / frozen.viewport.width),
        nested_hit_target_digest=target_digest,
    )


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def _join_on_cancel[T](operation: Awaitable[T]) -> T:
    task = asyncio.ensure_future(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.shield(task)
        raise
