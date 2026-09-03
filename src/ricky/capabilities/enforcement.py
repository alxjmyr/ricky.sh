"""Runtime enforcement of immutable capability guardrails."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, cast

from pydantic import BaseModel

from ricky.capabilities.guardrails import (
    CompiledGuardrail,
    GuardrailEvaluator,
    GuardrailRegistry,
    GuardrailUsage,
)
from ricky.tools.base import Tool, ToolContext, ToolResult


class GuardrailEnforcementError(RuntimeError):
    """A contracted guardrail cannot be composed exactly."""


@dataclass
class _UsageState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    calls: int = 0
    dimensions: dict[str, int] = field(default_factory=dict)


def build_guardrailed_tools(
    tools: list[Tool],
    *,
    guardrails: tuple[CompiledGuardrail, ...],
    guardrail_tools: dict[str, tuple[str, ...]],
    registry: GuardrailRegistry,
) -> list[Tool]:
    """Wrap every exactly mapped tool with its immutable contract guardrail."""

    by_name = {tool.name: tool for tool in tools}
    wrapped = dict(by_name)
    claimed: set[str] = set()
    for guardrail in guardrails:
        evaluator = registry.get(guardrail.capability_id)
        if evaluator is None:
            raise GuardrailEnforcementError(
                f"no guardrail evaluator for capability: {guardrail.capability_id}"
            )
        if evaluator.schema_id != guardrail.schema_id or (
            evaluator.schema_version != guardrail.schema_version
        ):
            raise GuardrailEnforcementError(
                f"guardrail evaluator schema changed: {guardrail.capability_id}"
            )
        expected = frozenset(guardrail_tools.get(guardrail.capability_id, ()))
        if not expected or not expected <= evaluator.tools:
            raise GuardrailEnforcementError(
                f"guardrail tool mapping changed: {guardrail.capability_id}"
            )
        selected = _selected_tools(guardrail.constraints)
        if expected != evaluator.tools and selected != expected:
            raise GuardrailEnforcementError(
                f"partial guardrail tool mapping is not bound by its constraints: "
                f"{guardrail.capability_id}"
            )
        missing = sorted(expected - set(by_name))
        if missing:
            raise GuardrailEnforcementError(
                "guardrail governs tools outside the contract: " + ", ".join(missing)
            )
        overlap = sorted(claimed & expected)
        if overlap:
            raise GuardrailEnforcementError(
                "contract tools have multiple guardrails: " + ", ".join(overlap)
            )
        claimed.update(expected)
        usage = _UsageState()
        for name in expected:
            wrapped[name] = cast(
                Tool,
                GuardrailedTool(
                    wrapped[name],
                    guardrail=guardrail,
                    evaluator=evaluator,
                    usage=usage,
                ),
            )
    return [wrapped[tool.name] for tool in tools]


def _selected_tools(constraints: object) -> frozenset[str] | None:
    """Return an explicit schema-owned tool subset when one is present.

    Most capability guardrails govern their complete evaluator inventory. A
    capability may expose a narrower exact runtime subset only by carrying the
    canonical ``allowed_tools`` constraint, so a changed contract mapping can
    never silently widen to the evaluator's remaining tools.
    """

    if not isinstance(constraints, dict) or "allowed_tools" not in constraints:
        return None
    raw = constraints["allowed_tools"]
    if not isinstance(raw, list) or any(not isinstance(name, str) for name in raw):
        raise GuardrailEnforcementError("guardrail allowed_tools constraint is malformed")
    selected = frozenset(raw)
    if not selected or len(selected) != len(raw):
        raise GuardrailEnforcementError("guardrail allowed_tools constraint is invalid")
    return selected


class GuardrailedTool:
    """Reserve evaluator-owned usage before one bounded concrete call."""

    def __init__(
        self,
        tool: Tool,
        *,
        guardrail: CompiledGuardrail,
        evaluator: GuardrailEvaluator,
        usage: _UsageState,
    ) -> None:
        self._tool = tool
        self._guardrail = guardrail
        self._evaluator = evaluator
        self._usage = usage
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
        args = params.model_dump(mode="python")
        async with self._usage.lock:
            usage = GuardrailUsage(
                calls=self._usage.calls,
                dimensions=dict(self._usage.dimensions),
            )
            verdict = self._evaluator.evaluate_call(
                self._guardrail,
                self.name,
                args,
                usage,
            )
            if not verdict.allowed:
                return ToolResult(
                    content=f"contract guardrail denied {self.name}: {verdict.reason}",
                    is_error=True,
                )
            if any(value < 0 for value in verdict.usage_delta.values()):
                raise GuardrailEnforcementError("guardrail usage deltas cannot be negative")
            self._usage.calls += 1
            for name, value in verdict.usage_delta.items():
                self._usage.dimensions[name] = self._usage.dimensions.get(name, 0) + value
        return await self._tool.run(params, ctx)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tool, name)
