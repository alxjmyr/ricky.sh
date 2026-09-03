"""Durable multi-turn state for live ad hoc delegation review."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ricky.capabilities.guardrails import (
    AuthenticatedSource,
    CollectedGuardrailField,
    CompiledGuardrail,
    GuardrailProposal,
)
from ricky.executions.contracts import ConfirmationRef
from ricky.profiles import ProfileLabel, ProfileScope

DraftStatus = Literal[
    "collecting_guardrails",
    "awaiting_confirmation",
    "ready",
    "queued",
    "executing",
    "completed",
    "uncertain",
    "expired",
    "cancelled",
    "rejected",
]
DraftActivityKind = Literal[
    "created",
    "guardrails_updated",
    "confirmation_requested",
    "confirmed",
    "ready",
    "queued",
    "executing",
    "completed",
    "uncertain",
    "expired",
    "cancelled",
    "rejected",
]

_DRAFT_ID = re.compile(r"^draft_[0-9a-f]{32}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AdHocExecutionProposal(_StrictModel):
    """Start one model-proposed task review; never authority by itself."""

    action: Literal["start"] = "start"
    task_id: str
    expected_task_revision: int = Field(ge=1)
    retry_of: str | None = Field(default=None, pattern=r"^execution_[0-9a-f]{32}$")
    goal: str = Field(min_length=1, max_length=50_000)
    requested_capabilities: Sequence[str] = Field(min_length=1, max_length=100)
    guardrails: Sequence[GuardrailProposal] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _proposal(self) -> AdHocExecutionProposal:
        if len(self.requested_capabilities) != len(set(self.requested_capabilities)):
            raise ValueError("requested capabilities must be unique")
        proposed = [item.capability_id for item in self.guardrails]
        if len(proposed) != len(set(proposed)):
            raise ValueError("a proposal cannot repeat a capability guardrail")
        if not set(proposed) <= set(self.requested_capabilities):
            raise ValueError("guardrails must belong to requested capabilities")
        return self


class AdHocGuardrailContinuation(_StrictModel):
    """Add only newly supplied or corrected fields to an existing draft."""

    action: Literal["supply_guardrails"]
    draft_id: str = Field(pattern=r"^draft_[0-9a-f]{32}$")
    expected_draft_revision: int = Field(ge=1)
    guardrails: Sequence[GuardrailProposal] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _guardrails(self) -> AdHocGuardrailContinuation:
        capabilities = [item.capability_id for item in self.guardrails]
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("a continuation cannot repeat a capability guardrail")
        return self


class AdHocConfirmation(_StrictModel):
    """Confirm one exact persisted draft summary without reconstructing it."""

    action: Literal["confirm"]
    draft_id: str = Field(pattern=r"^draft_[0-9a-f]{32}$")
    expected_draft_revision: int = Field(ge=1)


class AdHocCancellation(_StrictModel):
    """Reject one pending draft without reconstructing or confirming it."""

    action: Literal["cancel"]
    draft_id: str = Field(pattern=r"^draft_[0-9a-f]{32}$")
    expected_draft_revision: int = Field(ge=1)


AdHocDelegationCommand = Annotated[
    AdHocExecutionProposal | AdHocGuardrailContinuation | AdHocConfirmation | AdHocCancellation,
    Field(discriminator="action"),
]


class ForegroundCapabilityCall(_FrozenModel):
    """One exact proposed direct call; raw arguments are never persisted."""

    capability_id: str = Field(min_length=3, max_length=300)
    tool_name: str = Field(min_length=1, max_length=300)
    arguments_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    safe_summary: str = Field(min_length=1, max_length=4_000)


class ExecutionDraft(_FrozenModel):
    id: str
    target: Literal["gateway_foreground", "ad_hoc_background"]
    status: DraftStatus
    revision: int = Field(ge=1)
    principal_id: str = Field(min_length=1, max_length=500)
    conversation_id: str = Field(min_length=1, max_length=512)
    task_id: str | None = None
    task_revision: int | None = Field(default=None, ge=1)
    retry_of: str | None = Field(default=None, pattern=r"^execution_[0-9a-f]{32}$")
    profile_scope: ProfileScope
    goal: str = Field(min_length=1, max_length=50_000)
    requested_capabilities: tuple[str, ...] = Field(min_length=1, max_length=100)
    sources: tuple[AuthenticatedSource, ...] = Field(min_length=1, max_length=20)
    collected_guardrail_fields: tuple[CollectedGuardrailField, ...] = Field(
        default=(), max_length=1_000
    )
    guardrails: tuple[CompiledGuardrail, ...] = Field(default=(), max_length=20)
    pending_questions: tuple[str, ...] = Field(default=(), max_length=20)
    confirmation_required: bool = False
    confirmation_summary: str | None = Field(default=None, max_length=8_000)
    confirmation_summary_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    confirmation: ConfirmationRef | None = None
    agent_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    route_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    inventory_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    contract_id: str | None = Field(default=None, pattern=r"^contract_[0-9a-f]{32}$")
    request_id: str | None = Field(default=None, pattern=r"^execution_[0-9a-f]{32}$")
    foreground_call: ForegroundCapabilityCall | None = None
    reason: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def _draft(self) -> ExecutionDraft:
        if _DRAFT_ID.fullmatch(self.id) is None:
            raise ValueError("invalid execution draft id")
        for field in ("created_at", "updated_at", "expires_at"):
            _utc(getattr(self, field), field)
        if self.updated_at < self.created_at or self.expires_at <= self.created_at:
            raise ValueError("execution draft timestamps are inconsistent")
        if len(self.requested_capabilities) != len(set(self.requested_capabilities)):
            raise ValueError("draft capabilities must be unique")
        if (self.task_id is None) != (self.task_revision is None):
            raise ValueError("draft task linkage requires id and revision together")
        if self.target == "ad_hoc_background" and (
            self.task_id is None or self.foreground_call is not None
        ):
            raise ValueError("ad hoc background drafts require a task and no direct call")
        if self.target == "gateway_foreground" and self.foreground_call is None:
            raise ValueError("foreground drafts require one exact proposed call")
        if len({source.message_id for source in self.sources}) != len(self.sources):
            raise ValueError("draft authenticated sources must be unique")
        collected_keys = [
            (item.capability_id, item.field) for item in self.collected_guardrail_fields
        ]
        if len(collected_keys) != len(set(collected_keys)):
            raise ValueError("draft cannot collect the same guardrail field twice")
        source_by_id = {source.message_id: source for source in self.sources}
        for item in self.collected_guardrail_fields:
            source = source_by_id.get(item.source_message_id)
            if source is None or source.text_digest != item.source_text_digest:
                raise ValueError("collected guardrail field source is outside the draft")
            if item.capability_id not in self.requested_capabilities:
                raise ValueError("collected guardrail field belongs to an unrequested capability")
        for guardrail in self.guardrails:
            evidence = tuple(
                item
                for item in self.collected_guardrail_fields
                if item.capability_id == guardrail.capability_id
            )
            if evidence:
                if any(
                    item.schema_id != guardrail.schema_id
                    or item.schema_version != guardrail.schema_version
                    for item in evidence
                ):
                    raise ValueError("compiled guardrail differs from collected field schema")
                if set(guardrail.source_message_ids) != {
                    item.source_message_id for item in evidence
                }:
                    raise ValueError("compiled guardrail source ids differ from field evidence")
        if self.status == "collecting_guardrails" and not self.pending_questions:
            raise ValueError("a collecting draft requires guardrail questions")
        if self.status == "awaiting_confirmation" and (
            not self.confirmation_required or self.confirmation_summary is None
        ):
            raise ValueError("an awaiting draft requires an exact confirmation summary")
        if (self.confirmation_summary is None) != (self.confirmation_summary_digest is None):
            raise ValueError("confirmation summary and digest must be stored together")
        if self.confirmation_summary is not None and (
            summary_digest(self.confirmation_summary) != self.confirmation_summary_digest
        ):
            raise ValueError("confirmation summary digest mismatch")
        if self.confirmation is not None:
            if self.confirmation.draft_id != self.id:
                raise ValueError("confirmation belongs to another draft")
            if self.confirmation.draft_revision >= self.revision or (
                self.status == "ready"
                and self.contract_id is None
                and self.confirmation.draft_revision != self.revision - 1
            ):
                raise ValueError("confirmation covers another draft revision")
            if self.confirmation.principal_id != self.principal_id:
                raise ValueError("confirmation belongs to another principal")
            if self.confirmation.source_message_id not in {
                source.message_id for source in self.sources
            }:
                raise ValueError("confirmation source is outside the draft")
            if self.confirmation.summary_digest != self.confirmation_summary_digest:
                raise ValueError("confirmation covers another summary")
        if (
            self.confirmation_required
            and self.status
            in {
                "ready",
                "queued",
                "executing",
                "completed",
                "uncertain",
            }
            and self.confirmation is None
        ):
            raise ValueError("an authorized draft requires confirmation evidence")
        if self.status == "queued" and (self.contract_id is None or self.request_id is None):
            raise ValueError("queued drafts require contract and request linkage")
        return self


class ExecutionDraftActivity(_FrozenModel):
    id: int = Field(ge=1)
    draft_id: str = Field(pattern=r"^draft_[0-9a-f]{32}$")
    profile_label: ProfileLabel
    kind: DraftActivityKind
    from_status: DraftStatus | None = None
    to_status: DraftStatus
    revision: int = Field(ge=1)
    summary: str = Field(min_length=1, max_length=2_000)
    created_at: datetime


def summary_digest(summary: str) -> str:
    return hashlib.sha256(summary.encode("utf-8")).hexdigest()


def draft_digest(draft: ExecutionDraft) -> str:
    payload = draft.model_dump(mode="json")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must use UTC")
