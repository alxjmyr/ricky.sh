"""Capability-owned authority evaluation.

There is deliberately no generic JSON policy language. Each delegable effect
capability registers an :class:`AuthorityEvaluator` that owns one versioned
scope schema, a bounded human summary, tool-call evaluation, deterministic
effect identity, typed receipt rules, and the rule for when its grant is
consumed. Live proposal validation belongs to its paired guardrail evaluator.

An effect tool without an evaluator is not delegable, and an evaluator can
never add a tool to an execution contract.
"""

from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable

from ricky.authority.types import AuthorityScope, AuthorityVerdict
from ricky.tools.base import EffectIdentity, EffectReceipt, ToolResult


@runtime_checkable
class AuthorityEvaluator(Protocol):
    """Owner of one delegable capability's authority semantics."""

    capability: ClassVar[str]
    schema_id: ClassVar[str]
    schema_version: ClassVar[int]
    tools: ClassVar[frozenset[str]]

    def summarize(self, scope: AuthorityScope) -> str:
        """Return a bounded human summary of the authority this scope grants."""
        ...

    def evaluate_call(
        self, scope: AuthorityScope, tool_name: str, args: dict[str, object]
    ) -> AuthorityVerdict:
        """Decide whether one concrete effect call stays inside the scope."""
        ...

    def effect_identity(
        self, scope: AuthorityScope, tool_name: str, args: dict[str, object]
    ) -> EffectIdentity:
        """Derive deterministic occurrence identity before dispatch."""
        ...

    def receipt(self, scope: AuthorityScope, result: ToolResult) -> EffectReceipt:
        """Return the typed receipt for an already-dispatched effect call."""
        ...

    def consumes_grant(self, scope: AuthorityScope, receipt: EffectReceipt) -> bool:
        """Return true when this receipt exhausts the delegated authority."""
        ...


class AuthorityRegistryError(RuntimeError):
    """A delegable capability registration is invalid or unknown."""


class AuthorityRegistry:
    """Exact lookup from capability name and effect tool name to its evaluator."""

    def __init__(self, evaluators: list[AuthorityEvaluator] | None = None) -> None:
        self._by_capability: dict[str, AuthorityEvaluator] = {}
        self._by_tool: dict[str, AuthorityEvaluator] = {}
        for evaluator in evaluators or []:
            self.register(evaluator)

    def register(self, evaluator: AuthorityEvaluator) -> None:
        capability = evaluator.capability
        if capability in self._by_capability:
            raise AuthorityRegistryError(f"duplicate delegable capability: {capability}")
        if not evaluator.tools:
            raise AuthorityRegistryError(
                f"delegable capability '{capability}' governs no effect tools"
            )
        for name in evaluator.tools:
            if name in self._by_tool:
                raise AuthorityRegistryError(f"effect tool has two evaluators: {name}")
        self._by_capability[capability] = evaluator
        for name in evaluator.tools:
            self._by_tool[name] = evaluator

    def get(self, capability: str) -> AuthorityEvaluator | None:
        return self._by_capability.get(capability)

    def require(self, capability: str) -> AuthorityEvaluator:
        evaluator = self._by_capability.get(capability)
        if evaluator is None:
            raise AuthorityRegistryError(f"no authority evaluator for capability: {capability}")
        return evaluator

    def for_tool(self, tool_name: str) -> AuthorityEvaluator | None:
        return self._by_tool.get(tool_name)

    def capabilities(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_capability))

    def tools_for(self, capability: str) -> frozenset[str]:
        return self.require(capability).tools


def default_authority_registry() -> AuthorityRegistry:
    """Return the production authority evaluators shipped with Ricky."""

    from ricky.browser.authority import browser_authority_evaluators

    return AuthorityRegistry(list(browser_authority_evaluators()))
