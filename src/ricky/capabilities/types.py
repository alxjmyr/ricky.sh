"""Strict capability inventory and policy boundary models."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ricky.tool_contracts import EffectKind, ReviewMode, UnattendedUse

CapabilityKind = Literal["tool_group", "direct_tool", "skill"]
CapabilityResourceKind = Literal["tool", "skill"]
CapabilityRisk = Literal["read_only", "mutating", "destructive"]
_CAPABILITY_ID = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+$")


def validate_capability_id(value: str) -> str:
    if _CAPABILITY_ID.fullmatch(value) is None:
        raise ValueError(f"invalid capability id: {value}")
    return value


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CapabilityResource(_FrozenModel):
    """One exact runtime resource exposed by a capability."""

    kind: CapabilityResourceKind
    id: str = Field(min_length=1, max_length=300)
    contract_version: int = Field(ge=1, le=1_000)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: str = Field(min_length=1, max_length=1_000)
    risk_class: CapabilityRisk | None = None
    effect_kind: EffectKind | None = None
    unattended: UnattendedUse | None = None
    state_guard_id: str | None = Field(default=None, max_length=200)
    review_mode: ReviewMode | None = None

    @model_validator(mode="after")
    def _kind_contract(self) -> CapabilityResource:
        if self.kind == "tool" and self.risk_class is None:
            raise ValueError("tool capability resources require a risk class")
        if self.kind == "tool" and (
            self.effect_kind is None or self.unattended is None or self.review_mode is None
        ):
            raise ValueError(
                "tool capability resources require effect, unattended, and review facts"
            )
        if self.kind == "skill" and self.risk_class is not None:
            raise ValueError("skill capability resources cannot carry a tool risk class")
        if self.kind == "skill" and any(
            value is not None
            for value in (
                self.effect_kind,
                self.unattended,
                self.state_guard_id,
                self.review_mode,
            )
        ):
            raise ValueError("skill capability resources cannot carry tool execution facts")
        return self


class CapabilitySpec(_FrozenModel):
    """Stable shared policy meaning for a declared grouped capability."""

    id: str
    owner: str = Field(min_length=1, max_length=200)
    version: int = Field(default=1, ge=1, le=1_000)
    description: str = Field(min_length=1, max_length=2_000)
    guardrail_schema_id: str | None = Field(default=None, max_length=200)
    authority_capability: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _spec_contract(self) -> CapabilitySpec:
        validate_capability_id(self.id)
        if self.id != self.owner and not self.id.startswith(f"{self.owner}."):
            raise ValueError("capability id must use its owner's namespace")
        return self


class CapabilityDefinition(_FrozenModel):
    """One installed, provenance-aware user-facing availability unit."""

    id: str
    version: int = Field(default=1, ge=1, le=1_000)
    kind: CapabilityKind
    description: str = Field(min_length=1, max_length=2_000)
    owner: str = Field(min_length=1, max_length=200)
    resources: tuple[CapabilityResource, ...] = Field(min_length=1, max_length=100)
    risk_class: CapabilityRisk
    guardrail_schema_id: str | None = Field(default=None, max_length=200)
    authority_capability: str | None = Field(default=None, max_length=200)
    unattended_eligible: bool = True
    unattended_blockers: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _definition_contract(self) -> CapabilityDefinition:
        validate_capability_id(self.id)
        identities = [(resource.kind, resource.id) for resource in self.resources]
        if len(identities) != len(set(identities)):
            raise ValueError("a capability cannot repeat a resource")
        tool_risks: list[CapabilityRisk] = [
            resource.risk_class
            for resource in self.resources
            if resource.kind == "tool" and resource.risk_class is not None
        ]
        rank = {"read_only": 0, "mutating": 1, "destructive": 2}
        if tool_risks and max(tool_risks, key=rank.__getitem__) != self.risk_class:
            raise ValueError("capability risk must equal its highest member risk")
        if self.kind == "skill" and any(resource.kind != "skill" for resource in self.resources):
            raise ValueError("skill capabilities may contain only skill resources")
        if self.kind != "skill" and any(resource.kind != "tool" for resource in self.resources):
            raise ValueError("tool capabilities may contain only tool resources")
        if self.unattended_eligible == bool(self.unattended_blockers):
            raise ValueError("unattended eligibility and blockers disagree")
        return self


class CapabilityPolicyDecision(_FrozenModel):
    """Resolved eligibility and live-evidence requirements for one agent class."""

    capability_id: str
    eligible: bool
    confirmation_required: bool = False
    guardrail_required: bool = False
    reasons: tuple[str, ...] = ()
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _decision_contract(self) -> CapabilityPolicyDecision:
        validate_capability_id(self.capability_id)
        if not self.eligible and (self.confirmation_required or self.guardrail_required):
            raise ValueError("an excluded capability cannot carry live-evidence requirements")
        return self


class CapabilityDiagnostic(_FrozenModel):
    capability_id: str
    severity: Literal["info", "warning", "error"]
    message: str = Field(min_length=1, max_length=2_000)
