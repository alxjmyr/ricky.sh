"""Permission engine used in the tool dispatch path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ricky.agent.session import AgentSession
from ricky.permissions.types import GrantScope, PermissionDecision, Policy

if TYPE_CHECKING:
    # Annotation-only: a runtime import of tools.base would cycle
    # (tools.base -> agent.events -> permissions.types -> permissions.engine).
    from ricky.tools.base import Risk


@dataclass(frozen=True)
class PermissionCheck:
    """A permission decision plus human-readable reason."""

    decision: PermissionDecision
    reason: str


class PermissionEngine:
    """Evaluate session grants and policy rules for tool invocations."""

    def __init__(self, policy: Policy | None = None) -> None:
        self._policy = policy or Policy()

    def decide(
        self,
        session: AgentSession,
        *,
        tool_name: str,
        risk: Risk,
        params: dict[str, object],
        scope: GrantScope | None = None,
    ) -> PermissionCheck:
        """Decide whether a tool invocation is allowed, denied, or asks."""
        matched_rule = next(
            (rule for rule in self._policy.rules if rule.matches(tool_name, params)),
            None,
        )
        if matched_rule is not None and matched_rule.decision == "deny":
            return PermissionCheck(matched_rule.decision, matched_rule.reason)

        for grant in session.permission_grants:
            if grant.matches(tool_name, params):
                return PermissionCheck("allow", "allowed by session grant")

        if matched_rule is not None:
            return PermissionCheck(matched_rule.decision, matched_rule.reason)

        if scope is not None and scope.requires_permission:
            return PermissionCheck("ask", "host path is outside the active workspace")

        if risk == "read_only":
            return PermissionCheck(self._policy.read_only_default, "default for read-only tools")
        if risk == "mutating":
            return PermissionCheck(self._policy.mutating_default, "default for mutating tools")
        return PermissionCheck(self._policy.destructive_default, "default for destructive tools")
