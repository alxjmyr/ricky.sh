"""Background enforcement for one active delegation grant.

Four independent checks must all allow a delegated effect call:

``ExecutionContract`` (the tool is technically reachable), ``DelegationGrant``
(the user authorized this action for this task), ``Policy`` (the owner ceiling
did not deny it), and the effect guard (this exact action has not already been
reserved, performed, or left in doubt).
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel

from ricky.authority.registry import AuthorityRegistry
from ricky.authority.store import AuthorityStore, GrantStateError
from ricky.authority.types import DelegationGrant
from ricky.jobs.store import (
    GrantAuthorityError,
    JobActionConflictError,
    JobEffectBudgetError,
    JobRunStore,
)
from ricky.tools.base import (
    EffectActionBinder,
    EffectDisposition,
    EffectIdentity,
    EffectReceipt,
    PreparedEffect,
    PreparedEffectAborter,
    PreparedEffectProvider,
    Tool,
    ToolContext,
    ToolResult,
)


class DelegatedAuthorityError(RuntimeError):
    """A delegated run cannot be composed safely."""


@dataclass(frozen=True)
class DelegatedRun:
    """Everything one background worker needs to enforce one grant."""

    grant: DelegationGrant
    registry: AuthorityRegistry
    authority: AuthorityStore

    def tool_names(self) -> frozenset[str]:
        """The exact effect tools this grant's scopes authorize."""

        names: set[str] = set()
        for scope in self.grant.scopes:
            evaluator = self.registry.require(scope.capability)
            selected = _selected_tools(scope.constraints)
            if selected is None:
                names |= evaluator.tools
                continue
            selectable = evaluator.tools | getattr(evaluator, "scope_only_tools", frozenset())
            if not selected or not selected <= selectable:
                raise DelegatedAuthorityError(
                    f"authority scope selects invalid tools for {scope.capability}"
                )
            # Scope-only tools are contract-bound runtime setup operations, not
            # external effects. They remain subject to the ordinary execution
            # tool policy but must not be wrapped in delegated effect handling.
            names |= selected & evaluator.tools
        return frozenset(names)


def _selected_tools(constraints: object) -> frozenset[str] | None:
    """Read the canonical exact tool subset from a capability-owned scope."""

    if not isinstance(constraints, dict) or "allowed_tools" not in constraints:
        return None
    raw = constraints["allowed_tools"]
    if not isinstance(raw, list) or any(not isinstance(name, str) for name in raw):
        raise DelegatedAuthorityError("authority allowed_tools constraint is malformed")
    if len(raw) != len(set(raw)):
        raise DelegatedAuthorityError("authority allowed_tools constraint contains duplicates")
    return frozenset(raw)


def build_delegated_tools(
    tools: list[Tool],
    delegation: DelegatedRun,
    *,
    jobs: JobRunStore,
    run_id: str,
    effect_budget: int,
) -> list[Tool]:
    """Wrap every delegated effect tool; leave every other tool untouched.

    A tool named by the grant but absent from the resolved runtime is a
    composition error, not a silent downgrade.
    """

    delegated = delegation.tool_names()
    present = {tool.name for tool in tools}
    missing = sorted(delegated - present)
    if missing:
        raise DelegatedAuthorityError(
            f"grant authorizes tools the runtime does not expose: {', '.join(missing)}"
        )
    wrapped: list[Tool] = []
    for tool in tools:
        if tool.name not in delegated:
            wrapped.append(tool)
            continue
        if getattr(tool, "unattended", "forbidden") == "forbidden":
            raise DelegatedAuthorityError(
                f"tool is never available to a delegated run: {tool.name}"
            )
        wrapped.append(
            cast(
                Tool,
                DelegatedEffectTool(
                    tool,
                    grant=delegation.grant,
                    registry=delegation.registry,
                    authority=delegation.authority,
                    jobs=jobs,
                    run_id=run_id,
                    effect_budget=effect_budget,
                ),
            )
        )
    return wrapped


class DelegatedEffectTool:
    """Evaluate, reserve, dispatch, and conservatively resolve one delegated effect."""

    def __init__(
        self,
        tool: Tool,
        *,
        grant: DelegationGrant,
        registry: AuthorityRegistry,
        authority: AuthorityStore,
        jobs: JobRunStore,
        run_id: str,
        effect_budget: int,
    ) -> None:
        self._tool = tool
        self._grant = grant
        self._registry = registry
        self._authority = authority
        self._jobs = jobs
        self._run_id = run_id
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

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        evaluator = self._registry.for_tool(self.name)
        if evaluator is None:
            return await self._deny("effect tool has no authority evaluator", capability=None)
        scope = self._grant.scope_for(evaluator.capability)
        if scope is None:
            return await self._deny(
                "the grant carries no scope for this capability", capability=evaluator.capability
            )

        args = params.model_dump(mode="python")
        try:
            verdict = evaluator.evaluate_call(scope, self.name, args)
        except ValueError as exc:
            return await self._deny(f"scope evaluation failed: {exc}", capability=scope.capability)
        if not verdict.allowed:
            return await self._deny(verdict.reason, capability=scope.capability)

        prepared: PreparedEffect | None = None
        try:
            if isinstance(self._tool, PreparedEffectProvider):
                prepared = await self._tool.prepare_effect(args, ctx)
                identity = EffectIdentity.model_validate(prepared.identity)
            else:
                identity = EffectIdentity.model_validate(
                    evaluator.effect_identity(scope, self.name, args)
                )
        except (TypeError, ValueError, OSError) as exc:
            return ToolResult(
                content=f"{self.name} preflight failed: {exc}",
                is_error=True,
                effect_receipt=EffectReceipt(
                    disposition="not_performed",
                    attempt_reason="invalid_preflight",
                ),
            )
        reservation = asyncio.create_task(
            self._jobs.reserve_grant_action(
                grant_id=self._grant.id,
                scope=self._grant.profile_scope,
                namespace=self._grant.effect_namespace(),
                task_id=self._grant.task_id,
                run_id=self._run_id,
                identity=identity,
                effect_budget=self._effect_budget,
                amount_minor=verdict.amount_minor,
                currency=verdict.currency,
            )
        )
        try:
            action = await asyncio.shield(reservation)
        except asyncio.CancelledError as cancelled:
            try:
                action = await reservation
            except BaseException as reservation_error:
                await _abort_prepared(
                    self._tool, prepared, ctx, "authority reservation was cancelled"
                )
                raise cancelled from reservation_error
            await self._finalize_owned(
                action_id=action.id,
                capability=scope.capability,
                disposition="not_performed",
                provider_reference=None,
                consume=False,
                summary=f"{self.name} was cancelled before provider dispatch",
            )
            await _abort_prepared(self._tool, prepared, ctx, "authority reservation was cancelled")
            raise
        except (GrantAuthorityError, JobActionConflictError, JobEffectBudgetError) as exc:
            await _abort_prepared(self._tool, prepared, ctx, "authority reservation was denied")
            return await self._deny(str(exc), capability=scope.capability)
        except BaseException:
            await _abort_prepared(self._tool, prepared, ctx, "authority reservation failed")
            raise

        try:
            if isinstance(self._tool, EffectActionBinder):
                self._tool.bind_effect_action(action.id, identity.action_key)
        except BaseException:
            await self._finalize_owned(
                action_id=action.id,
                capability=scope.capability,
                disposition="not_performed",
                provider_reference=None,
                consume=False,
                summary=f"{self.name} failed evidence binding before provider dispatch",
            )
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
            await self._finalize_bound_owned(
                action_id=action.id,
                capability=scope.capability,
                disposition="in_doubt",
                provider_reference=None,
                consume=True,
                summary=f"{self.name} became ambiguous before returning a result",
            )
            raise

        receipt = evaluator.receipt(scope, result)
        consumes = evaluator.consumes_grant(scope, receipt)
        await self._finalize_bound_owned(
            action_id=action.id,
            capability=scope.capability,
            disposition=receipt.disposition,
            provider_reference=receipt.provider_reference,
            consume=consumes,
            summary=f"{self.name} resolved {receipt.disposition}",
        )
        return self._annotate(result, receipt, action.id)

    async def _finalize_owned(
        self,
        *,
        action_id: str,
        capability: str,
        disposition: EffectDisposition,
        provider_reference: str | None,
        consume: bool,
        summary: str,
    ) -> None:
        """Join the complete ordered cross-store finalization before cancellation escapes."""

        finalization = asyncio.create_task(
            self._finalize(
                action_id=action_id,
                capability=capability,
                disposition=disposition,
                provider_reference=provider_reference,
                consume=consume,
                summary=summary,
            )
        )
        try:
            await asyncio.shield(finalization)
        except asyncio.CancelledError:
            await finalization
            raise

    async def _finalize(
        self,
        *,
        action_id: str,
        capability: str,
        disposition: EffectDisposition,
        provider_reference: str | None,
        consume: bool,
        summary: str,
    ) -> None:
        # Strongest external-effect evidence is committed first. Later failures
        # propagate so recovery sees a partial finalization instead of success.
        await self._jobs.resolve_action(
            action_id,
            disposition,
            scope=self._grant.profile_scope,
            provider_reference=provider_reference,
        )
        await self._authority.record(
            self._grant.id,
            "used",
            summary,
            scope=self._grant.profile_scope,
            capability=capability,
            tool_name=self.name,
            action_id=action_id,
            disposition=disposition,
        )
        if consume:
            await self._consume(f"authority consumed by a {disposition} effect")

    async def _finalize_bound_owned(
        self,
        *,
        action_id: str,
        capability: str,
        disposition: EffectDisposition,
        provider_reference: str | None,
        consume: bool,
        summary: str,
    ) -> None:
        async def operation() -> None:
            await self._finalize(
                action_id=action_id,
                capability=capability,
                disposition=disposition,
                provider_reference=provider_reference,
                consume=consume,
                summary=summary,
            )
            await _settle_bound_action(self._tool, action_id)

        finalization = asyncio.create_task(operation())
        try:
            await asyncio.shield(finalization)
        except asyncio.CancelledError:
            await finalization
            raise

    async def _consume(self, reason: str) -> None:
        """Retire the grant. A grant already retired by a concurrent call is fine."""

        with suppress(GrantStateError):
            await self._authority.consume(
                self._grant.id, scope=self._grant.profile_scope, reason=reason
            )
        await self._jobs.set_grant_budget_status(
            self._grant.id, "consumed", scope=self._grant.profile_scope
        )

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        hook = getattr(self._tool, "summarize_permission", None)
        if hook is not None:
            return hook(args, ctx)
        return f"delegated effect under grant {self._grant.id}: {self.name}"

    def normalize_permission_args(
        self, args: dict[str, object], ctx: ToolContext
    ) -> dict[str, object]:
        hook = getattr(self._tool, "normalize_permission_args", None)
        return hook(args, ctx) if hook is not None else args

    async def _deny(self, reason: str, *, capability: str | None) -> ToolResult:
        await self._authority.record(
            self._grant.id,
            "denied",
            f"{self.name} denied: {reason}",
            scope=self._grant.profile_scope,
            capability=capability,
            tool_name=self.name,
        )
        return ToolResult(
            content=f"delegated authority denied {self.name}: {reason}",
            is_error=True,
            effect_receipt=EffectReceipt(
                disposition="not_performed",
                attempt_reason="denied",
            ),
        )

    def _annotate(self, result: ToolResult, receipt: EffectReceipt, action_id: str) -> ToolResult:
        note = f"\ngrant_id: {self._grant.id}\njob_action_id: {action_id}"
        if receipt.disposition == "in_doubt":
            note += (
                "\nThis action is in doubt. Do not retry it. Report it to the user for "
                "reconciliation."
            )
        receipt = receipt.model_copy(update={"action_id": action_id})
        return result.model_copy(
            update={"content": f"{result.content}{note}", "effect_receipt": receipt}
        )


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
