"""Strict JSON-safe contracts for foreground gateway conversations."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ricky.capabilities import GuardrailIntakeSpec
from ricky.profiles import ProfileLabel, ProfileScope

ConversationStatus = Literal["active", "archived", "uncertain"]
GatewayInboundStatus = Literal["running", "committed", "failed", "uncertain"]
CorrelationRecordKind = Literal[
    "task",
    "execution_request",
    "job_run",
    "workflow_run",
    "conversation",
    "notification",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversationKey(_StrictModel):
    """Deterministic transport identity for one logical conversation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transport: str = Field(min_length=1, max_length=100)
    account: str = Field(min_length=1, max_length=100)
    destination_id: str = Field(min_length=1, max_length=500)
    thread_id: str | None = Field(default=None, min_length=1, max_length=500)

    def digest(self) -> str:
        payload = "\0".join(
            (self.transport, self.account, self.destination_id, self.thread_id or "")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Conversation(_StrictModel):
    """Persistent route and session mapping for one foreground conversation."""

    id: str = Field(pattern=r"^conversation_[0-9a-f]{32}$")
    key: ConversationKey
    session_id: str = Field(pattern=r"^session_[0-9a-f]{32}$")
    route_name: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=500)
    profile_scope: ProfileScope
    project_root: str | None = Field(default=None, max_length=2_000)
    route_policy_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_for_inbound_message_id: str | None = Field(
        default=None, pattern=r"^inbound_[0-9a-f]{32}$"
    )
    archived_for_inbound_message_id: str | None = Field(
        default=None, pattern=r"^inbound_[0-9a-f]{32}$"
    )
    status: ConversationStatus
    revision: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime
    last_processed_inbound_message_id: str | None = Field(
        default=None, pattern=r"^inbound_[0-9a-f]{32}$"
    )

    @model_validator(mode="after")
    def _validate_conversation(self) -> Conversation:
        _utc(self.created_at, "created_at")
        _utc(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("conversation updated_at cannot precede created_at")
        return self


class GatewayInboundResult(_StrictModel):
    """Durable outcome for exactly one authenticated inbox message."""

    message_id: str = Field(pattern=r"^inbound_[0-9a-f]{32}$")
    conversation_id: str = Field(pattern=r"^conversation_[0-9a-f]{32}$")
    session_id: str = Field(pattern=r"^session_[0-9a-f]{32}$")
    profile_label: ProfileLabel
    status: GatewayInboundStatus
    session_revision: int | None = Field(default=None, ge=0)
    response_outbox_id: str | None = Field(default=None, pattern=r"^outbox_[0-9a-f]{32}$")
    error: str | None = Field(default=None, max_length=2_000)
    started_at: datetime
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def _validate_result(self) -> GatewayInboundResult:
        _utc(self.started_at, "started_at")
        if self.finished_at is not None:
            _utc(self.finished_at, "finished_at")
            if self.finished_at < self.started_at:
                raise ValueError("gateway result finished_at cannot precede started_at")
        if self.status == "running" and self.finished_at is not None:
            raise ValueError("running gateway results cannot have finished_at")
        if self.status != "running" and self.finished_at is None:
            raise ValueError("terminal gateway results require finished_at")
        if self.status in {"failed", "uncertain"} and self.error is None:
            raise ValueError("failed or uncertain gateway results require an error")
        return self


class CorrelatedRecord(_StrictModel):
    """Bounded current-state projection for a trusted notification link."""

    kind: CorrelationRecordKind
    id: str = Field(min_length=1, max_length=500)
    profile_label: ProfileLabel
    revision: int | None = Field(default=None, ge=0)
    status: str | None = Field(default=None, max_length=100)
    summary: str = Field(min_length=1, max_length=4_000)
    artifact_links: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("artifact_links")
    @classmethod
    def _bounded_links(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 1_000 for value in values):
            raise ValueError("artifact links must contain 1 to 1000 characters")
        return values


class GatewayActivity(_StrictModel):
    """Deterministic context supplied beside, never inside, chat history."""

    profile_label: ProfileLabel
    replied_notification_id: str | None = Field(
        default=None, pattern=r"^notification_[0-9a-f]{32}$"
    )
    records: list[CorrelatedRecord] = Field(default_factory=list, max_length=100)
    recent_notifications: list[CorrelatedRecord] = Field(default_factory=list, max_length=100)


class GatewayCapabilityItem(_StrictModel):
    """One valid exact control-plane capability exposed as routing context."""

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=2_000)
    delegable_capabilities: list[str] = Field(default_factory=list, max_length=8)
    """Authority evaluator identities required by this capability."""
    resources: list[str] = Field(default_factory=list, max_length=100)
    confirmation_required: bool = False
    guardrail_required: bool = False
    guardrail_intake: GuardrailIntakeSpec | None = None

    @model_validator(mode="after")
    def _guardrail_intake(self) -> GatewayCapabilityItem:
        if self.guardrail_required != (self.guardrail_intake is not None):
            raise ValueError("a guarded gateway capability requires its exact intake specification")
        return self


class GatewayCapabilityCatalog(_StrictModel):
    """Bounded valid named-job and ad hoc capability choices for one turn."""

    named_jobs: list[GatewayCapabilityItem] = Field(default_factory=list, max_length=100)
    ad_hoc_capabilities: list[GatewayCapabilityItem] = Field(default_factory=list, max_length=100)


class GatewayProcessResult(_StrictModel):
    """Bounded coordinator result for service and interface callers."""

    message_id: str = Field(pattern=r"^inbound_[0-9a-f]{32}$")
    conversation_id: str = Field(pattern=r"^conversation_[0-9a-f]{32}$")
    session_id: str = Field(pattern=r"^session_[0-9a-f]{32}$")
    status: Literal["processed", "uncertain"]
    response_outbox_id: str | None = Field(default=None, pattern=r"^outbox_[0-9a-f]{32}$")


def _utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{name} must use UTC")
