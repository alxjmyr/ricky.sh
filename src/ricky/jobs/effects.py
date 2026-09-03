"""Deterministic external-effect identity, receipts, and guarded dispatch."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from pydantic import BaseModel

from ricky.jobs.store import JobActionConflictError, JobEffectBudgetError, JobRunStore
from ricky.profiles import ProfileScope
from ricky.tools.base import (
    EffectActionBinder,
    EffectDisposition,
    EffectIdentity,
    EffectIdentityProvider,
    EffectReceipt,
    PreparedEffect,
    PreparedEffectAborter,
    PreparedEffectProvider,
    Tool,
    ToolContext,
    ToolResult,
)

__all__ = [
    "EffectIdentity",
    "EffectIdentityProvider",
    "GuardedEffectTool",
    "is_guardable",
]


class GuardedEffectTool:
    """Reserve an external action and conservatively resolve every dispatch path."""

    def __init__(
        self,
        tool: Tool,
        *,
        store: JobRunStore,
        job_name: str,
        run_id: str,
        profile_scope: ProfileScope,
        effect_budget: int,
    ) -> None:
        self._tool = tool
        self._store = store
        self._job_name = job_name
        self._run_id = run_id
        self._profile_scope = profile_scope
        self._effect_budget = effect_budget
        self.name = tool.name
        self.description = tool.description
        self.Params = tool.Params
        self.risk = tool.risk
        declared = cast(Any, tool)
        self.capability_id = declared.capability_id
        self.effect_kind = declared.effect_kind
        self.unattended = declared.unattended
        self.state_guard_id = declared.state_guard_id
        self.review_mode = getattr(declared, "review_mode", "policy")

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        """Expose the wrapped deterministic identity to workflow journaling."""

        if not isinstance(self._tool, EffectIdentityProvider):
            raise ValueError(f"unguarded recurring mutation denied: {self.name}")
        return EffectIdentity.model_validate(self._tool.effect_identity(args, ctx))

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        if not isinstance(self._tool, EffectIdentityProvider):
            return ToolResult(
                content=f"unguarded recurring mutation denied: {self.name}",
                is_error=True,
                effect_receipt=EffectReceipt(
                    disposition="not_performed",
                    attempt_reason="denied",
                ),
            )
        args = params.model_dump(mode="python")
        prepared: PreparedEffect | None = None
        try:
            if isinstance(self._tool, PreparedEffectProvider):
                prepared = await self._tool.prepare_effect(args, ctx)
                identity = EffectIdentity.model_validate(prepared.identity)
            else:
                identity = EffectIdentity.model_validate(self._tool.effect_identity(args, ctx))
        except Exception as exc:  # deterministic preflight failed before reservation.
            return ToolResult(
                content=f"{self.name} preflight failed: {exc}",
                is_error=True,
                effect_receipt=EffectReceipt(
                    disposition="not_performed",
                    attempt_reason="invalid_preflight",
                ),
            )
        reservation = asyncio.create_task(
            self._store.reserve_action(
                job_name=self._job_name,
                run_id=self._run_id,
                scope=self._profile_scope,
                identity=identity,
                effect_budget=self._effect_budget,
            )
        )
        try:
            action = await asyncio.shield(reservation)
        except asyncio.CancelledError as cancelled:
            # The store uses a worker thread, so cancellation cannot prove that
            # reservation stopped. Observe it to completion and record the
            # strongest known outcome: dispatch never began.
            try:
                action = await reservation
            except BaseException as reservation_error:
                await _abort_prepared(self._tool, prepared, ctx, "effect reservation was cancelled")
                raise cancelled from reservation_error
            await self._resolve(action.id, "not_performed", provider_reference=None)
            await _abort_prepared(self._tool, prepared, ctx, "effect reservation was cancelled")
            raise
        except (JobActionConflictError, JobEffectBudgetError) as exc:
            await _abort_prepared(self._tool, prepared, ctx, "effect reservation was denied")
            return ToolResult(
                content=f"{self.name} denied before dispatch: {exc}",
                is_error=True,
                effect_receipt=EffectReceipt(
                    disposition="not_performed",
                    attempt_reason="denied",
                ),
            )
        except BaseException:
            await _abort_prepared(self._tool, prepared, ctx, "effect reservation failed")
            raise
        try:
            if isinstance(self._tool, EffectActionBinder):
                self._tool.bind_effect_action(action.id, identity.action_key)
        except BaseException:
            await self._resolve(action.id, "not_performed", provider_reference=None)
            await _abort_prepared(
                self._tool, prepared, ctx, "effect evidence binding failed before dispatch"
            )
            raise
        try:
            if prepared is not None:
                assert isinstance(self._tool, PreparedEffectProvider)
                result = await self._tool.run_prepared(params, prepared, ctx)
            else:
                result = await self._tool.run(params, ctx)
        except BaseException:
            await self._resolve_bound(
                action.id,
                "in_doubt",
                provider_reference=None,
            )
            raise
        receipt = result.effect_receipt
        if receipt is None:
            receipt = EffectReceipt(disposition="in_doubt")
            result = result.model_copy(
                update={
                    "content": (
                        f"{self.name} violated its external-effect contract after reservation: "
                        "no receipt was returned"
                    ),
                    "is_error": True,
                    "effect_receipt": receipt,
                }
            )
        await self._resolve_bound(
            action.id,
            receipt.disposition,
            provider_reference=receipt.provider_reference,
        )
        receipt = receipt.model_copy(update={"action_id": action.id})
        return result.model_copy(
            update={
                "content": f"{result.content}\njob_action_id: {action.id}",
                "effect_receipt": receipt,
            }
        )

    async def _resolve(
        self,
        action_id: str,
        disposition: EffectDisposition,
        *,
        provider_reference: str | None,
    ) -> None:
        resolution = asyncio.create_task(
            self._store.resolve_action(
                action_id,
                disposition,
                scope=self._profile_scope,
                provider_reference=provider_reference,
            )
        )
        try:
            await asyncio.shield(resolution)
        except asyncio.CancelledError:
            # A provider outcome must not be left behind a still-running store
            # thread. Make the durable state observable before propagating.
            await resolution
            raise

    async def _resolve_bound(
        self,
        action_id: str,
        disposition: EffectDisposition,
        *,
        provider_reference: str | None,
    ) -> None:
        async def operation() -> None:
            await self._store.resolve_action(
                action_id,
                disposition,
                scope=self._profile_scope,
                provider_reference=provider_reference,
            )
            await _settle_bound_action(self._tool, action_id)

        finalization = asyncio.create_task(operation())
        try:
            await asyncio.shield(finalization)
        except asyncio.CancelledError:
            await finalization
            raise

    def normalize_permission_args(
        self, args: dict[str, object], ctx: ToolContext
    ) -> dict[str, object]:
        hook = getattr(self._tool, "normalize_permission_args", None)
        return hook(args, ctx) if hook is not None else args

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        hook = getattr(self._tool, "summarize_permission", None)
        return hook(args, ctx) if hook is not None else f"recurring effect: {self.name}"

    def __getattr__(self, name: str) -> Any:
        """Preserve optional callable-contract declarations from the wrapped tool."""

        return getattr(self._tool, name)


def is_guardable(tool: Tool) -> bool:
    return isinstance(tool, EffectIdentityProvider)


async def _abort_prepared(
    tool: Tool,
    prepared: PreparedEffect | None,
    ctx: ToolContext,
    reason: str,
) -> None:
    if prepared is None or not isinstance(tool, PreparedEffectAborter):
        return
    operation = asyncio.create_task(tool.abort_prepared(prepared, ctx, reason=reason))
    try:
        await asyncio.shield(operation)
    except asyncio.CancelledError:
        await operation
        raise


async def _settle_bound_action(tool: Tool, action_id: str) -> None:
    if not isinstance(tool, EffectActionBinder):
        return
    operation = asyncio.create_task(tool.settle_effect_action(action_id))
    try:
        await asyncio.shield(operation)
    except asyncio.CancelledError:
        await operation
        raise
