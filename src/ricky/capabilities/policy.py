"""Deterministic narrowing of installed capability eligibility."""

from __future__ import annotations

import hashlib
import json

from ricky.capabilities.guardrails import GuardrailRegistry
from ricky.capabilities.registry import CapabilityRegistry
from ricky.capabilities.types import CapabilityDiagnostic, CapabilityPolicyDecision
from ricky.config import AgentCapabilityPolicySettings, GatewayRouteSettings

LEGACY_CAPABILITY_REPLACEMENTS = {
    "builtin.capabilities.authorize": "builtin.authorization.review",
    "builtin.project.write": "builtin.project.mutate",
    "builtin.shell.run": "builtin.host.execute",
    "builtin.session.tasks": "builtin.session.mutate",
    "builtin.memory.write": "builtin.memory.mutate",
    "builtin.memory.delete": "builtin.memory.mutate",
    "builtin.skills.use": "builtin.skill.use",
    "builtin.workflows.start": "builtin.automation.mutate",
    "builtin.workflows.validate": "builtin.automation.read",
    "builtin.tasks.read": "builtin.task.read",
    "builtin.tasks.create": "builtin.task.mutate",
    "builtin.tasks.coordinate": "builtin.task.mutate",
    "builtin.tasks.admin": "builtin.task.mutate",
    "builtin.tasks.artifacts.write": "builtin.task.mutate",
    "builtin.executions.start_jobs": "builtin.automation.mutate",
    "builtin.executions.delegate": "builtin.automation.mutate",
    "builtin.executions.manage": "builtin.automation.mutate",
    "builtin.executions.read": "builtin.automation.read",
    "builtin.notifications.send": "builtin.notification.mutate",
    "builtin.web.search": "builtin.web.read",
    "builtin.gmail.read": "builtin.email.read",
    "builtin.gmail.draft": "builtin.email.mutate",
    "builtin.gmail.send": "builtin.email.mutate",
    "builtin.gmail.labels.write": "builtin.email.mutate",
    "builtin.gmail.trash": "builtin.email.mutate",
    "builtin.gmail.download": "builtin.email.mutate",
    "builtin.calendar.write": "builtin.calendar.mutate",
    "builtin.calendar.delete": "builtin.calendar.mutate",
    "builtin.slack.read": "builtin.chat.read",
    "builtin.slack.mark_read": "builtin.chat.mutate",
    "builtin.slack.send": "builtin.chat.mutate",
    "builtin.slack.download": "builtin.chat.mutate",
}


def policy_digest(
    base: AgentCapabilityPolicySettings,
    route: GatewayRouteSettings | None = None,
) -> str:
    payload = {
        "base": base.model_dump(mode="json"),
        "route": (
            {
                "exclude_capabilities": route.exclude_capabilities,
                "confirmation_required_capabilities": (route.confirmation_required_capabilities),
                "guardrail_required_capabilities": route.guardrail_required_capabilities,
            }
            if route is not None
            else None
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def resolve_capability_policy(
    registry: CapabilityRegistry,
    base: AgentCapabilityPolicySettings,
    *,
    route: GatewayRouteSettings | None = None,
    require_unattended: bool = False,
) -> tuple[CapabilityPolicyDecision, ...]:
    """Resolve default eligibility plus every narrowing policy layer."""

    excluded = set(base.exclude_capabilities)
    confirmations = set(base.confirmation_required_capabilities)
    guardrails = set(base.guardrail_required_capabilities)
    if route is not None:
        excluded.update(route.exclude_capabilities)
        confirmations.update(route.confirmation_required_capabilities)
        guardrails.update(route.guardrail_required_capabilities)
    digest = policy_digest(base, route)
    decisions: list[CapabilityPolicyDecision] = []
    for definition in registry.definitions():
        reasons: list[str] = []
        eligible = definition.id not in excluded and (
            not require_unattended or definition.unattended_eligible
        )
        if definition.id in excluded:
            reasons.append("excluded by configured owner policy")
        if require_unattended and not definition.unattended_eligible:
            reasons.extend(definition.unattended_blockers)
        confirmation = eligible and definition.id in confirmations
        guardrail = eligible and definition.id in guardrails
        if confirmation:
            reasons.append("live confirmation is required")
        if guardrail:
            reasons.append("a task-specific live guardrail is required")
        if eligible and not reasons:
            reasons.append("installed and eligible by default")
        decisions.append(
            CapabilityPolicyDecision(
                capability_id=definition.id,
                eligible=eligible,
                confirmation_required=confirmation,
                guardrail_required=guardrail,
                reasons=tuple(reasons),
                policy_digest=digest,
            )
        )
    return tuple(decisions)


def validate_capability_policy(
    registry: CapabilityRegistry,
    guardrails: GuardrailRegistry,
    base: AgentCapabilityPolicySettings,
    *,
    route: GatewayRouteSettings | None = None,
) -> tuple[CapabilityDiagnostic, ...]:
    configured = {
        *base.exclude_capabilities,
        *base.confirmation_required_capabilities,
        *base.guardrail_required_capabilities,
    }
    required_guardrails = set(base.guardrail_required_capabilities)
    if route is not None:
        configured.update(route.exclude_capabilities)
        configured.update(route.confirmation_required_capabilities)
        configured.update(route.guardrail_required_capabilities)
        required_guardrails.update(route.guardrail_required_capabilities)
    installed = set(registry.ids())
    diagnostics: list[CapabilityDiagnostic] = []
    for capability_id in sorted(configured - installed):
        severity = "error" if capability_id.startswith("builtin.") else "warning"
        replacement = LEGACY_CAPABILITY_REPLACEMENTS.get(capability_id)
        diagnostics.append(
            CapabilityDiagnostic(
                capability_id=capability_id,
                severity=severity,
                message=(
                    f"configured legacy capability is not installed; replace with {replacement}"
                    if replacement is not None
                    else "configured capability is not installed"
                ),
            )
        )
    hard_scoped = {
        definition.id
        for definition in registry.definitions()
        if definition.authority_capability is not None
    }
    for capability_id in sorted((required_guardrails | hard_scoped) & installed):
        definition = registry.require(capability_id)
        evaluator = guardrails.get(capability_id)
        if definition.guardrail_schema_id is None or evaluator is None:
            diagnostics.append(
                CapabilityDiagnostic(
                    capability_id=capability_id,
                    severity="error",
                    message="guardrail is required but no evaluator/schema is registered",
                )
            )
            continue
        tool_ids = {resource.id for resource in definition.resources if resource.kind == "tool"}
        if evaluator.schema_id != definition.guardrail_schema_id:
            diagnostics.append(
                CapabilityDiagnostic(
                    capability_id=capability_id,
                    severity="error",
                    message="guardrail evaluator schema differs from capability registration",
                )
            )
        if not tool_ids <= evaluator.tools:
            diagnostics.append(
                CapabilityDiagnostic(
                    capability_id=capability_id,
                    severity="error",
                    message=(
                        "guardrail evaluator tools differ: evaluator does not cover "
                        "capability resources"
                    ),
                )
            )
    return tuple(diagnostics)


def validate_capability_inventory(
    registry: CapabilityRegistry,
    guardrails: GuardrailRegistry,
) -> tuple[CapabilityDiagnostic, ...]:
    """Validate capability-owned evaluators independently of user policy."""

    diagnostics: list[CapabilityDiagnostic] = []
    for definition in registry.definitions():
        evaluator = guardrails.get(definition.id)
        declares_guardrail = definition.guardrail_schema_id is not None
        if declares_guardrail != (evaluator is not None):
            diagnostics.append(
                CapabilityDiagnostic(
                    capability_id=definition.id,
                    severity="error",
                    message=("capability guardrail declaration and evaluator registration differ"),
                )
            )
            continue
        if evaluator is None:
            if definition.authority_capability is not None:
                diagnostics.append(
                    CapabilityDiagnostic(
                        capability_id=definition.id,
                        severity="error",
                        message="specialized authority capability requires a guardrail evaluator",
                    )
                )
            continue
        if evaluator.schema_id != definition.guardrail_schema_id:
            diagnostics.append(
                CapabilityDiagnostic(
                    capability_id=definition.id,
                    severity="error",
                    message="guardrail evaluator schema differs from capability registration",
                )
            )
        tool_ids = {resource.id for resource in definition.resources if resource.kind == "tool"}
        if not tool_ids <= evaluator.tools:
            diagnostics.append(
                CapabilityDiagnostic(
                    capability_id=definition.id,
                    severity="error",
                    message=(
                        "guardrail evaluator tools differ: evaluator does not cover "
                        "capability resources"
                    ),
                )
            )
    return tuple(diagnostics)


def validate_foreground_live_policy(
    registry: CapabilityRegistry,
    base: AgentCapabilityPolicySettings,
    *,
    route: GatewayRouteSettings | None = None,
) -> tuple[CapabilityDiagnostic, ...]:
    """Reject live-review settings for tools deliberately absent from foreground."""

    configured = {
        *base.confirmation_required_capabilities,
        *base.guardrail_required_capabilities,
    }
    if route is not None:
        configured.update(route.confirmation_required_capabilities)
        configured.update(route.guardrail_required_capabilities)
    diagnostics: list[CapabilityDiagnostic] = []
    excluded = set(base.exclude_capabilities)
    if route is not None:
        excluded.update(route.exclude_capabilities)
    if "builtin.authorization.review" in configured:
        diagnostics.append(
            CapabilityDiagnostic(
                capability_id="builtin.authorization.review",
                severity="error",
                message="the live-review control cannot require its own live review",
            )
        )
    if configured and "builtin.authorization.review" in excluded:
        diagnostics.append(
            CapabilityDiagnostic(
                capability_id="builtin.authorization.review",
                severity="error",
                message=(
                    "foreground live requirements need the source-bound authorization control"
                ),
            )
        )
    for capability_id in sorted(configured & set(registry.ids())):
        definition = registry.require(capability_id)
        direct_supported = definition.risk_class == "read_only" or capability_id.startswith(
            ("builtin.task.", "builtin.automation.", "builtin.authorization.")
        )
        if not direct_supported:
            diagnostics.append(
                CapabilityDiagnostic(
                    capability_id=capability_id,
                    severity="error",
                    message=(
                        "live foreground review is configured, but this external mutation "
                        "is hard-excluded from direct foreground execution"
                    ),
                )
            )
    return tuple(diagnostics)
