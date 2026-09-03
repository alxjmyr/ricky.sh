"""Strict platform-neutral notification and outbox contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ricky.attachments import StoredAttachment
from ricky.profiles import ProfileLabel

NotificationUrgency = Literal["normal", "attention", "urgent"]
MessageTextFormat = Literal["plain_text", "portable_markdown_v1"]
CorrelationKind = Literal[
    "task",
    "job_run",
    "execution_request",
    "workflow_run",
    "conversation",
]
OutboxStatus = Literal[
    "pending",
    "claimed",
    "delivered",
    "failed",
    "in_doubt",
    "cancelled",
]
AttemptOutcome = Literal["claimed", "delivered", "not_performed", "in_doubt", "released"]
ResolutionDisposition = Literal["delivered", "not_delivered"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CorrelationRef(_StrictModel):
    """A typed link back to durable state owned by another subsystem."""

    kind: CorrelationKind
    id: str = Field(min_length=1, max_length=500)
    revision: int | None = Field(default=None, ge=0)
    profile_label: ProfileLabel


class NotificationRequest(_StrictModel):
    """Immutable request created by a producer."""

    id: str = Field(pattern=r"^notification_[0-9a-f]{32}$")
    route: str = Field(min_length=1, max_length=200)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=20_000)
    body_format: MessageTextFormat = "plain_text"
    urgency: NotificationUrgency = "normal"
    source_kind: str = Field(min_length=1, max_length=100)
    profile_label: ProfileLabel
    source_id: str = Field(min_length=1, max_length=500)
    dedupe_key: str = Field(min_length=1, max_length=500)
    correlations: list[CorrelationRef] = Field(default_factory=list, max_length=20)
    attachments: list[StoredAttachment] = Field(default_factory=list, max_length=20)
    created_at: datetime
    expires_at: datetime | None = None

    @field_validator("route", "title", "body", "source_kind", "source_id", "dedupe_key")
    @classmethod
    def _trim_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("notification text values cannot be empty")
        return value

    @model_validator(mode="after")
    def _validate_times(self) -> NotificationRequest:
        _require_utc(self.created_at, "created_at")
        if self.expires_at is not None:
            _require_utc(self.expires_at, "expires_at")
            if self.expires_at <= self.created_at:
                raise ValueError("expires_at must follow created_at")
        return self


class OutboxEntry(_StrictModel):
    """Current delivery state for one immutable notification request."""

    id: str = Field(pattern=r"^outbox_[0-9a-f]{32}$")
    notification_id: str = Field(pattern=r"^notification_[0-9a-f]{32}$")
    route: str = Field(min_length=1, max_length=200)
    status: OutboxStatus
    attempt_count: int = Field(ge=0)
    fence: int = Field(ge=0)
    lease_owner: str | None = Field(default=None, min_length=1, max_length=200)
    lease_token: str | None = Field(default=None, min_length=1, max_length=128)
    lease_expires_at: datetime | None = None
    transport: str | None = Field(default=None, min_length=1, max_length=100)
    destination_ref: str | None = Field(default=None, min_length=1, max_length=500)
    platform_message_id: str | None = Field(default=None, min_length=1, max_length=500)
    error: str | None = Field(default=None, min_length=1, max_length=2_000)
    created_at: datetime
    updated_at: datetime
    delivered_at: datetime | None = None

    @model_validator(mode="after")
    def _validate_state(self) -> OutboxEntry:
        for name in ("created_at", "updated_at", "lease_expires_at", "delivered_at"):
            value = getattr(self, name)
            if value is not None:
                _require_utc(value, name)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        lease_values = (self.lease_owner, self.lease_token, self.lease_expires_at)
        if self.status == "claimed" and any(value is None for value in lease_values):
            raise ValueError("claimed outbox entries require a complete lease")
        if self.status != "claimed" and any(value is not None for value in lease_values):
            raise ValueError("only claimed outbox entries can hold a lease")
        if self.status == "delivered" and self.delivered_at is None:
            raise ValueError("delivered outbox entries require delivered_at")
        if self.status != "delivered" and self.delivered_at is not None:
            raise ValueError("delivered_at is valid only for delivered entries")
        return self


class NotificationRecord(_StrictModel):
    """One request with its current delivery state."""

    request: NotificationRequest
    outbox: OutboxEntry


class DeliveryAttempt(_StrictModel):
    """Append-only identity and terminal outcome for one delivery claim."""

    id: int = Field(ge=1)
    outbox_id: str
    attempt_number: int = Field(ge=1)
    fence: int = Field(ge=1)
    worker: str = Field(min_length=1, max_length=200)
    transport: str = Field(min_length=1, max_length=100)
    destination_ref: str = Field(min_length=1, max_length=500)
    outcome: AttemptOutcome
    error: str | None = Field(default=None, max_length=2_000)
    started_at: datetime
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def _validate_times(self) -> DeliveryAttempt:
        _require_utc(self.started_at, "started_at")
        if self.finished_at is not None:
            _require_utc(self.finished_at, "finished_at")
            if self.finished_at < self.started_at:
                raise ValueError("finished_at cannot precede started_at")
        if self.outcome == "claimed" and self.finished_at is not None:
            raise ValueError("an active attempt cannot have finished_at")
        if self.outcome != "claimed" and self.finished_at is None:
            raise ValueError("a terminal attempt requires finished_at")
        return self


class OperatorResolution(_StrictModel):
    """Append-only human resolution of an ambiguous delivery."""

    id: int = Field(ge=1)
    outbox_id: str
    disposition: ResolutionDisposition
    actor: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=2_000)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _created_at_utc(cls, value: datetime) -> datetime:
        _require_utc(value, "created_at")
        return value


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(UTC)


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{name} must use UTC")
