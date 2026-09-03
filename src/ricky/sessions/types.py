"""Strict JSON-safe contracts for persistent conversations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ricky.agent.session import AgentSession
from ricky.profiles import ProfileLabel

SessionStatus = Literal["active", "archived", "uncertain"]
TurnStatus = Literal["running", "committed", "failed", "uncertain"]


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(UTC)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StoredSession(_StrictModel):
    """One canonical session snapshot plus storage metadata."""

    session: AgentSession
    profile_label: ProfileLabel
    revision: int = Field(ge=0)
    status: SessionStatus
    created_at: datetime
    updated_at: datetime
    last_turn_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _validate_times(self) -> StoredSession:
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.profile_label != self.session.profile_scope.label():
            raise ValueError("stored session profile label must match its session scope")
        return self


class StaleSessionLease(_StrictModel):
    """One session lease whose holder is gone.

    The store already refuses to commit through an expired lease, so this record
    describes work that is safe to release, not work that might still land.
    """

    session_id: str = Field(min_length=1, max_length=128)
    profile_label: ProfileLabel
    owner: str = Field(min_length=1, max_length=200)
    fence: int = Field(ge=1)
    expired_at: datetime
    running_turn_ids: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def _validate_stale_lease(self) -> StaleSessionLease:
        _require_utc(self.expired_at, "expired_at")
        return self


class SessionLease(_StrictModel):
    """A short, fenced right to change one stored session."""

    session_id: str = Field(min_length=1, max_length=128)
    profile_label: ProfileLabel
    owner: str = Field(min_length=1, max_length=200)
    token: str = Field(min_length=1, max_length=128)
    fence: int = Field(ge=1)
    acquired_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def _validate_times(self) -> SessionLease:
        _require_utc(self.acquired_at, "acquired_at")
        _require_utc(self.expires_at, "expires_at")
        if self.expires_at <= self.acquired_at:
            raise ValueError("expires_at must follow acquired_at")
        return self


class StoredTurn(_StrictModel):
    """Durable lifecycle record for one bounded conversation turn."""

    id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    profile_label: ProfileLabel
    inbound_ref: str | None = Field(default=None, max_length=500)
    base_revision: int = Field(ge=0)
    status: TurnStatus
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = Field(default=None, max_length=16_000)

    @model_validator(mode="after")
    def _validate_state(self) -> StoredTurn:
        _require_utc(self.started_at, "started_at")
        if self.finished_at is not None:
            _require_utc(self.finished_at, "finished_at")
            if self.finished_at < self.started_at:
                raise ValueError("finished_at cannot precede started_at")
        if self.status == "running" and self.finished_at is not None:
            raise ValueError("a running turn cannot be finished")
        if self.status != "running" and self.finished_at is None:
            raise ValueError("a terminal turn requires finished_at")
        if self.status in {"failed", "uncertain"} and not self.error:
            raise ValueError("a failed or uncertain turn requires an error")
        return self


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field_name} must use UTC")
