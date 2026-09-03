"""Shared per-call permission decision for the loop and the workflow runner.

Both callers must gate a tool call with exactly the same rules, so the
decision logic lives here once. The caller yields the returned events in
order and dispatches only on an ``allow``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from ricky.agent.events import (
    AgentEvent,
    PermissionDecidedEvent,
    PermissionRequestedEvent,
    ToolCallNormalizedEvent,
    ToolCallRejectedEvent,
    ToolCallRequestedEvent,
)
from ricky.agent.session import AgentSession, PermissionGrant
from ricky.llm import ToolCallPart
from ricky.permissions import GrantOption, GrantScope, PermissionEngine, PermissionResponse
from ricky.tools import (
    EffectReceipt,
    PreparedEffect,
    PreparedEffectProvider,
    ToolContext,
    ToolRegistry,
    ToolResult,
)

PermissionResponder = Callable[
    [PermissionRequestedEvent],
    Awaitable[PermissionResponse],
]


async def deny_permission(_event: PermissionRequestedEvent) -> PermissionResponse:
    """Default responder used when no interface is attached: fail closed."""
    return PermissionResponse(decision="deny")


@dataclass(frozen=True)
class GateOutcome:
    """The ordered events plus the decision for one gated tool call."""

    events: list[AgentEvent]
    decision: Literal["error", "deny", "allow"]
    error_result: ToolResult | None = None
    """Set only for ``error``: the caller records it and does not dispatch."""
    normalized_args: dict[str, object] | None = None
    """Canonical validated arguments used by policy and execution on allow."""
    remembered_grant: PermissionGrant | None = None
    """The grant appended to the session, when the responder chose one."""
    prepared_effect: PreparedEffect | None = None
    """Ephemeral exact payload carried only from review into foreground dispatch."""


def build_grant_candidates(
    call: ToolCallPart, scope: GrantScope | None
) -> list[tuple[GrantOption, PermissionGrant]]:
    """Turn a tool's declared scope into offered options and their grants.

    This is the single place a session ``PermissionGrant`` is built from a
    decision, so the ceiling is enforced here: no scope means nothing is
    offered, and a whole-tool option exists only when the tool opts in with
    ``allow_unconstrained``. No interface can widen a grant beyond this.
    """
    if scope is None:
        return []
    candidates: list[tuple[GrantOption, PermissionGrant]] = []
    if scope.params_equal:
        candidates.append(
            (
                GrantOption(id="scoped", label=scope.label),
                PermissionGrant(
                    tool_name=call.name,
                    params_equal=dict(scope.params_equal),
                    label=scope.label,
                ),
            )
        )
    if (
        scope.directory_param is not None
        and scope.directory_path is not None
        and scope.directory_label is not None
    ):
        candidates.append(
            (
                GrantOption(id="directory", label=scope.directory_label),
                PermissionGrant(
                    tool_name=call.name,
                    directory_param=scope.directory_param,
                    directory_path=scope.directory_path,
                    label=scope.directory_label,
                ),
            )
        )
    if scope.allow_unconstrained:
        label = f"all {call.name} (any params)"
        candidates.append(
            (
                GrantOption(id="tool", label=label),
                PermissionGrant(tool_name=call.name, params_equal={}, label=label),
            )
        )
    return candidates


async def decide_tool_permission(
    *,
    session: AgentSession,
    registry: ToolRegistry,
    engine: PermissionEngine,
    responder: PermissionResponder,
    turn_id: str,
    call: ToolCallPart,
    ctx: ToolContext,
) -> GateOutcome:
    """Decide one tool call: unknown/invalid args, deny, or allow.

    Appends a remembered grant to ``session.permission_grants`` only when the
    responder returns a grant id that was actually offered.
    """
    events: list[AgentEvent] = [
        ToolCallRequestedEvent(
            turn_id=turn_id,
            call_id=call.id,
            tool_name=call.name,
            args=call.args,
        )
    ]
    tool = registry.get(call.name)
    if tool is None:
        malformed = call.argument_error is not None
        if malformed:
            result = ToolResult(
                content=(
                    "Malformed tool call: the provider returned "
                    f"{call.argument_error}. Reissue one catalog tool with a valid JSON object."
                ),
                data={"error": "malformed_json", "tool": call.name},
                is_error=True,
            )
        else:
            result = registry.prepare_args(call.name, call.args).error
            assert result is not None
        events.append(
            _rejected_event(
                turn_id,
                call,
                "malformed_json" if malformed else "unknown_tool",
                repairable=True,
                external_effect=False,
            )
        )
        return GateOutcome(
            events=events,
            decision="error",
            error_result=result,
        )

    prepared = registry.prepare_args(
        call.name,
        call.args,
        parse_error=call.argument_error,
    )
    if prepared.normalized_paths:
        events.append(
            ToolCallNormalizedEvent(
                turn_id=turn_id,
                call_id=call.id,
                tool_name=call.name,
                paths=list(prepared.normalized_paths),
            )
        )
    if prepared.error is not None:
        reason = "malformed_json" if call.argument_error is not None else "invalid_arguments"
        events.append(
            _rejected_event(
                turn_id,
                call,
                reason,
                repairable=True,
                external_effect=getattr(tool, "effect_kind", None) == "external",
            )
        )
        return GateOutcome(events=events, decision="error", error_result=prepared.error)
    assert prepared.args is not None

    permission_args = registry.permission_args(call.name, prepared.args, ctx)
    execution_candidate = {
        key: permission_args.get(key, value) for key, value in prepared.args.items()
    }
    canonical_execution = registry.prepare_args(call.name, execution_candidate)
    if canonical_execution.error is not None or canonical_execution.args is None:
        raise ValueError(
            f"{call.name} returned permission arguments incompatible with its Params model"
        )
    execution_args = canonical_execution.args
    scope = registry.permission_scope(call.name, permission_args, ctx)
    check = engine.decide(
        session,
        tool_name=call.name,
        risk=tool.risk,
        params=permission_args,
        scope=scope,
    )
    decision = check.decision
    remembered = False
    remembered_grant: PermissionGrant | None = None
    grant_label: str | None = None
    reason = check.reason
    prepared_effect: PreparedEffect | None = None
    if decision == "deny":
        events.append(
            PermissionDecidedEvent(
                turn_id=turn_id,
                call_id=call.id,
                tool_name=call.name,
                decision="deny",
                reason=reason,
                remembered=False,
            )
        )
        return GateOutcome(events=events, decision="deny", normalized_args=execution_args)

    fresh_review = registry.review_mode(call.name) == "fresh"
    if fresh_review:
        decision = "ask"
        reason = "fresh interactive review required"

    if isinstance(tool, PreparedEffectProvider):
        try:
            prepared_effect = await tool.prepare_effect(execution_args, ctx)
            if prepared_effect.tool_name != call.name:
                raise ValueError(
                    f"prepared effect belongs to {prepared_effect.tool_name}, not {call.name}"
                )
        except Exception as exc:  # noqa: BLE001 - preparation failure becomes tool feedback.
            events.append(
                PermissionDecidedEvent(
                    turn_id=turn_id,
                    call_id=call.id,
                    tool_name=call.name,
                    decision="deny",
                    reason="effect preparation failed",
                    remembered=False,
                )
            )
            return GateOutcome(
                events=events,
                decision="error",
                error_result=ToolResult(
                    content=f"{call.name} preparation failed: {exc}",
                    is_error=True,
                    effect_receipt=EffectReceipt(disposition="not_performed"),
                ),
            )
    if decision == "ask":
        permission_call = call.model_copy(update={"args": permission_args, "argument_error": None})
        candidates = [] if fresh_review else build_grant_candidates(permission_call, scope)
        request = PermissionRequestedEvent(
            turn_id=turn_id,
            call_id=call.id,
            tool_name=call.name,
            args=permission_args,
            reason=reason,
            summary=(
                prepared_effect.permission_summary
                if prepared_effect is not None and prepared_effect.permission_summary is not None
                else registry.permission_summary(call.name, permission_args, ctx)
            ),
            offered_grants=[option for option, _ in candidates],
        )
        events.append(request)
        response = await responder(request)
        decision = response.decision
        reason = "allowed by user" if decision == "allow" else "denied by user"
        if decision == "allow" and response.grant is not None:
            # Authority guard: only honor an id we actually offered, so a
            # renderer can never widen a grant past the loop's ceiling.
            chosen = next(
                (grant for option, grant in candidates if option.id == response.grant),
                None,
            )
            if chosen is not None:
                session.permission_grants.append(chosen)
                remembered = True
                remembered_grant = chosen
                grant_label = chosen.label

    events.append(
        PermissionDecidedEvent(
            turn_id=turn_id,
            call_id=call.id,
            tool_name=call.name,
            decision=decision,
            reason=reason,
            remembered=remembered,
            grant_label=grant_label,
        )
    )
    if decision == "deny":
        return GateOutcome(events=events, decision="deny", normalized_args=execution_args)
    return GateOutcome(
        events=events,
        decision="allow",
        normalized_args=execution_args,
        remembered_grant=remembered_grant,
        prepared_effect=prepared_effect,
    )


def _rejected_event(
    turn_id: str,
    call: ToolCallPart,
    reason: Literal["unknown_tool", "malformed_json", "invalid_arguments"],
    *,
    repairable: bool,
    external_effect: bool,
) -> ToolCallRejectedEvent:
    encoded = json.dumps(
        {
            "name": call.name,
            "args": call.args,
            "argument_error": call.argument_error,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return ToolCallRejectedEvent(
        turn_id=turn_id,
        call_id=call.id,
        tool_name=call.name,
        reason=reason,
        repairable=repairable,
        external_effect=external_effect,
        input_digest=hashlib.sha256(encoded).hexdigest(),
    )
