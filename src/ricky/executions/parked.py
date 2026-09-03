"""Background-only prepared-effect parking before durable effect reservation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel

from ricky.executions.browser import (
    BrowserApprovalDraft,
    BrowserTransactionChallenge,
    ParkedBrowserApproval,
)
from ricky.executions.store import (
    BrowserApprovalError,
    ExecutionStore,
    ExecutionStoreError,
)
from ricky.profiles import ProfileScope
from ricky.tools.base import (
    EffectIdentity,
    EffectReceipt,
    PreparedEffect,
    PreparedEffectProvider,
    Tool,
    ToolContext,
    ToolResult,
)

ApprovalProjector = Callable[
    [PreparedEffect, dict[str, object], ToolContext],
    BrowserApprovalDraft,
]
ApprovalNotifier = Callable[[BrowserTransactionChallenge], Awaitable[None]]
ApprovalRevalidator = Callable[
    [ParkedBrowserApproval, PreparedEffect, ToolContext], Awaitable[bool]
]
ApprovalParkReserver = Callable[[BrowserApprovalDraft], Awaitable[None]]
ApprovalParkReleaser = Callable[[BrowserApprovalDraft | ParkedBrowserApproval], Awaitable[None]]
ApprovalDispatchAuthorizer = Callable[
    [ParkedBrowserApproval, PreparedEffect], Awaitable[object | None]
]
ApprovalDispatchFinalizer = Callable[[object, str], Awaitable[None]]


@dataclass(frozen=True)
class ParkedApprovedEffect:
    """In-process approved payload returned to the ordinary shared effect wrapper."""

    tool_name: str
    identity: EffectIdentity
    permission_summary: str | None
    inner: PreparedEffect
    approval: ParkedBrowserApproval


class ParkedPreparedEffectTool:
    """Turn synchronous fresh review into exact durable background approval.

    The execution runtime constructs this wrapper only for an approved
    background tool occurrence. It is a real ``PreparedEffectProvider``:
    ``prepare_effect`` performs raw preparation, durable parking, notification,
    waiting, and live revalidation. It then returns a wrapper prepared object
    whose action key is the harness-issued stable logical key. The existing
    delegated or guarded effect wrapper sees that identity and reserves it only
    after approval. ``run_prepared`` consumes the exact approval immediately
    before delegating once to the underlying prepared dispatch.

    Foreground instances remain unchanged with ``review_mode='fresh'``. This
    background wrapper alone declares ``review_mode='policy'`` so AgentLoop
    does not demand a synchronous responder before the durable path.
    """

    review_mode = "policy"

    def __init__(
        self,
        tool: Tool,
        *,
        store: ExecutionStore,
        scope: ProfileScope,
        claim_token: str,
        claim_fence: int,
        projector: ApprovalProjector,
        notifier: ApprovalNotifier,
        revalidator: ApprovalRevalidator,
        park_reserver: ApprovalParkReserver,
        park_releaser: ApprovalParkReleaser,
        dispatch_authorizer: ApprovalDispatchAuthorizer | None = None,
        dispatch_finalizer: ApprovalDispatchFinalizer | None = None,
    ) -> None:
        if not isinstance(tool, PreparedEffectProvider):
            raise TypeError("parked background approval requires a prepared-effect tool")
        self._tool = tool
        self._provider = cast(PreparedEffectProvider, tool)
        self._store = store
        self._scope = scope
        self._claim_token = claim_token
        self._claim_fence = claim_fence
        self._projector = projector
        self._notifier = notifier
        self._revalidator = revalidator
        self._park_reserver = park_reserver
        self._park_releaser = park_releaser
        self._dispatch_authorizer = dispatch_authorizer
        self._dispatch_finalizer = dispatch_finalizer
        self._released_park_keys: set[str] = set()
        self._release_lock = asyncio.Lock()
        self.name = tool.name
        self.description = tool.description
        self.Params = tool.Params
        self.risk = tool.risk
        declared = cast(Any, tool)
        self.capability_id = declared.capability_id
        self.effect_kind = declared.effect_kind
        self.unattended = declared.unattended
        self.state_guard_id = declared.state_guard_id

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        """Expose preflight compatibility; shared wrappers use prepared identity."""

        return EffectIdentity.model_validate(cast(Any, self._tool).effect_identity(args, ctx))

    async def prepare_effect(
        self,
        args: dict[str, object],
        ctx: ToolContext,
    ) -> ParkedApprovedEffect:
        inner = await self._provider.prepare_effect(args, ctx)
        draft = self._projector(inner, args, ctx)
        try:
            await self._reserve_park(draft)
        except asyncio.CancelledError:
            await self._release_park(draft)
            raise
        try:
            challenge = await self._store.park_browser_approval(
                draft,
                scope=self._scope,
                token=self._claim_token,
                fence=self._claim_fence,
            )
        except BaseException:
            # ExecutionStore joins its SQLite worker before propagating cancellation,
            # so the exact draft id tells us whether parking committed.
            approval: ParkedBrowserApproval | None = None
            with suppress(ExecutionStoreError):
                approval = await self._store.get_browser_approval(draft.id, scope=self._scope)
            if approval is not None:
                await self._invalidate(approval.id, "approval parking was interrupted")
            await self._release_park(approval or draft)
            raise
        try:
            await self._notifier(challenge)
        except BaseException:
            await self._invalidate(challenge.approval.id, "approval notification failed")
            await self._release_park(challenge.approval)
            raise
        try:
            decision = await self._store.wait_for_browser_approval(
                challenge.approval.id,
                scope=self._scope,
            )
        except BaseException:
            await self._invalidate(challenge.approval.id, "approval wait was interrupted")
            await self._release_park(challenge.approval)
            raise
        if decision.state != "approved":
            await self._resume_unconsumed(decision)
            await self._release_park(decision)
            raise ValueError(
                {
                    "denied": "browser approval denied",
                    "expired": "browser approval expired",
                    "invalidated": "live browser approval occurrence invalidated",
                    "pending": "execution cancellation interrupted browser approval",
                }.get(decision.state, f"browser approval ended as {decision.state}")
            )
        try:
            revalidated = await self._revalidator(decision, inner, ctx)
        except BaseException:
            await self._invalidate(decision.id, "live browser revalidation was interrupted")
            await self._release_park(decision)
            raise
        if not revalidated:
            await self._invalidate(decision.id, "live browser binding changed after approval")
            await self._release_park(decision)
            raise ValueError("approved live browser occurrence changed before effect reservation")
        original = EffectIdentity.model_validate(inner.identity)
        identity = original.model_copy(
            update={
                "occurrence": decision.id,
                "action_key": decision.logical_effect_key,
                "summary": f"Approved exact background {decision.kind.replace('_', ' ')}",
            }
        )
        return ParkedApprovedEffect(
            tool_name=self.name,
            identity=identity,
            permission_summary=inner.permission_summary,
            inner=inner,
            approval=decision,
        )

    async def run_prepared(
        self,
        params: BaseModel,
        prepared: PreparedEffect,
        ctx: ToolContext,
    ) -> ToolResult:
        if not isinstance(prepared, ParkedApprovedEffect) or prepared.tool_name != self.name:
            return _not_performed("prepared effect does not belong to this parked tool")
        try:
            consumed = await self._store.resume_browser_approval(
                prepared.approval.id,
                scope=self._scope,
                token=self._claim_token,
                fence=self._claim_fence,
                consume=True,
            )
        except ExecutionStoreError as exc:
            await self._release_park(prepared.approval)
            return _not_performed(f"approved browser occurrence could not be consumed: {exc}")
        except BaseException:
            await self._release_park(prepared.approval)
            raise
        try:
            await self._release_park(prepared.approval)
        except Exception as exc:
            return _not_performed(f"approved browser park could not be released: {exc}")
        if consumed.state != "consumed":
            return _not_performed("approved browser occurrence expired before reservation")
        authorization: object | None = None
        if self._dispatch_authorizer is not None:
            try:
                authorization = await self._dispatch_authorizer(
                    consumed,
                    prepared.inner,
                )
            except Exception as exc:
                return _not_performed(f"approved browser occurrence failed protected policy: {exc}")
        try:
            result = await self._provider.run_prepared(params, prepared.inner, ctx)
        except BaseException:
            if authorization is not None:
                await self._finalize_dispatch(authorization, "in_doubt")
            raise
        if authorization is not None:
            receipt = result.effect_receipt
            disposition = receipt.disposition if receipt is not None else "in_doubt"
            await self._finalize_dispatch(authorization, disposition)
        return result

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        """Fallback for direct dispatchers; shared runners use prepare/run_prepared."""

        prepared = await self.prepare_effect(params.model_dump(mode="python"), ctx)
        return await self.run_prepared(params, prepared, ctx)

    async def abort_prepared(
        self,
        prepared: PreparedEffect,
        ctx: ToolContext,
        *,
        reason: str,
    ) -> None:
        """Invalidate and release an approved park when outer reservation cannot proceed."""

        del ctx
        if not isinstance(prepared, ParkedApprovedEffect) or prepared.tool_name != self.name:
            return
        await self._invalidate(prepared.approval.id, reason[:2_000])
        await self._release_park(prepared.approval)

    def bind_effect_action(self, action_id: str, action_key: str) -> None:
        hook = getattr(self._tool, "bind_effect_action", None)
        if hook is not None:
            hook(action_id, action_key)

    async def settle_effect_action(self, action_id: str) -> None:
        hook = getattr(self._tool, "settle_effect_action", None)
        if hook is not None:
            await hook(action_id)

    async def _invalidate(self, approval_id: str, reason: str) -> None:
        try:
            await self._store.invalidate_browser_approval(
                approval_id,
                scope=self._scope,
                token=self._claim_token,
                fence=self._claim_fence,
                reason=reason,
            )
        except BrowserApprovalError:
            # Maintenance can expire an approved record between the live
            # recheck and invalidation. Its owner must still resume the fenced
            # execution instead of leaving a settled occurrence parked.
            with suppress(ExecutionStoreError):
                current = await self._store.get_browser_approval(
                    approval_id,
                    scope=self._scope,
                )
                if current.state in {"denied", "expired", "invalidated"}:
                    await self._store.resume_browser_approval(
                        approval_id,
                        scope=self._scope,
                        token=self._claim_token,
                        fence=self._claim_fence,
                        consume=False,
                    )

    async def _resume_unconsumed(self, approval: ParkedBrowserApproval) -> None:
        if approval.state == "pending":
            return
        try:
            await self._store.resume_browser_approval(
                approval.id,
                scope=self._scope,
                token=self._claim_token,
                fence=self._claim_fence,
                consume=False,
            )
        except BrowserApprovalError:
            return

    async def _reserve_park(self, draft: BrowserApprovalDraft) -> None:
        operation = asyncio.ensure_future(self._park_reserver(draft))
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            await operation
            raise

    async def _release_park(
        self,
        approval: BrowserApprovalDraft | ParkedBrowserApproval,
    ) -> None:
        key = approval.id
        async with self._release_lock:
            if key in self._released_park_keys:
                return
            operation = asyncio.ensure_future(self._park_releaser(approval))
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                await operation
                self._released_park_keys.add(key)
                raise
            self._released_park_keys.add(key)

    def normalize_permission_args(
        self, args: dict[str, object], ctx: ToolContext
    ) -> dict[str, object]:
        hook = getattr(self._tool, "normalize_permission_args", None)
        return hook(args, ctx) if hook is not None else args

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        hook = getattr(self._tool, "summarize_permission", None)
        return hook(args, ctx) if hook is not None else f"background effect: {self.name}"

    async def _finalize_dispatch(self, authorization: object, disposition: str) -> None:
        if self._dispatch_finalizer is None:
            return
        operation = asyncio.ensure_future(self._dispatch_finalizer(authorization, disposition))
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            await operation
            raise


def _not_performed(content: str) -> ToolResult:
    return ToolResult(
        content=content,
        is_error=True,
        effect_receipt=EffectReceipt(
            disposition="not_performed",
            attempt_reason="invalid_preflight",
        ),
    )
