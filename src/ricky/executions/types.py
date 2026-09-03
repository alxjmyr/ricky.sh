"""Strict durable-execution boundary models."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ricky.durable_tasks.types import validate_task_id
from ricky.profiles import ProfileLabel, ProfileScope

ExecutionKind = Literal["named_job", "ad_hoc"]
ExecutionStatus = Literal[
    "queued",
    "claimed",
    "running",
    "awaiting_protected_approval",
    "awaiting_transaction_approval",
    "cancel_requested",
    "succeeded",
    "failed",
    "blocked",
    "cancelled",
    "uncertain",
]
ExecutionActivityKind = Literal[
    "submitted",
    "claimed",
    "reclaimed",
    "started",
    "approval_requested",
    "approval_resumed",
    "lease_renewed",
    "released",
    "cancel_requested",
    "succeeded",
    "failed",
    "blocked",
    "cancelled",
    "uncertain",
    "retried",
    "resolved",
]
ExecutionResolutionDisposition = Literal["confirmed_completed", "confirmed_not_completed"]

_REQUEST_ID = re.compile(r"^execution_[0-9a-f]{32}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecutionRequest(_StrictModel):
    """One immutable submission plus its current fenced processing state."""

    id: str
    kind: ExecutionKind
    status: ExecutionStatus
    named_job: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_-]{0,63}/[a-z0-9][a-z0-9_-]{0,63}$",
    )
    job_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    project_root_ref: str | None = Field(default=None, max_length=2_000)
    goal: str | None = Field(default=None, max_length=50_000)
    contract_id: str | None = Field(default=None, pattern=r"^contract_[0-9a-f]{32}$")
    contract_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    task_id: str | None = None
    task_revision: int | None = Field(default=None, ge=1)
    profile_scope: ProfileScope
    source_conversation_id: str | None = Field(default=None, max_length=512)
    source_message_id: str | None = Field(default=None, max_length=512)
    grant_id: str | None = Field(default=None, pattern=r"^grant_[0-9a-f]{32}$")
    notification_route: str = Field(min_length=1, max_length=200)
    request_key: str = Field(min_length=1, max_length=500)
    parent_request_id: str | None = None
    created_at: datetime
    not_before: datetime | None = None
    expires_at: datetime | None = None
    claimed_by: str | None = Field(default=None, max_length=200)
    claim_token: str | None = Field(default=None, max_length=100)
    claim_fence: int = Field(default=0, ge=0)
    claim_expires_at: datetime | None = None
    run_id: str | None = Field(default=None, max_length=100)
    error: str | None = Field(default=None, max_length=2_000)

    @field_validator("id", "parent_request_id")
    @classmethod
    def _request_id(cls, value: str | None) -> str | None:
        if value is not None and _REQUEST_ID.fullmatch(value) is None:
            raise ValueError("invalid execution request id")
        return value

    @field_validator("goal")
    @classmethod
    def _goal(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("execution goal cannot be blank")
        return value

    @field_validator("task_id")
    @classmethod
    def _task_id(cls, value: str | None) -> str | None:
        return validate_task_id(value) if value is not None else None

    @field_validator("project_root_ref")
    @classmethod
    def _project_root_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value).expanduser()
        if not path.is_absolute() or str(path.resolve()) != value:
            raise ValueError("project_root_ref must be an absolute canonical path")
        return value

    @model_validator(mode="after")
    def _contract(self) -> ExecutionRequest:
        for name in ("created_at", "not_before", "expires_at", "claim_expires_at"):
            value = getattr(self, name)
            if value is not None:
                _aware(value, name)
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        if self.kind == "named_job":
            if self.named_job is None or self.job_digest is None:
                raise ValueError("named_job requests require named_job and job_digest")
            if (
                self.goal is not None
                or self.contract_id is not None
                or self.contract_digest is not None
            ):
                raise ValueError("named_job requests cannot carry an ad hoc contract or goal")
        else:
            if self.goal is None:
                raise ValueError("ad_hoc requests require a goal")
            if self.contract_id is None or self.contract_digest is None:
                raise ValueError("compiled ad_hoc requests require a complete pinned contract")
            if self.task_id is None or self.task_revision is None:
                raise ValueError("ad_hoc requests require a profile-owned durable task")
            if self.named_job is not None or self.job_digest is not None:
                raise ValueError("ad_hoc requests cannot carry a named job")
        if self.grant_id is not None and self.kind != "ad_hoc":
            raise ValueError("only ad hoc requests may carry a delegation grant")
        if self.task_id is None:
            if self.task_revision is not None:
                raise ValueError("task_revision requires task_id")
        elif self.task_revision is None:
            raise ValueError("task linkage requires a revision")
        claim_fields = (self.claimed_by, self.claim_token, self.claim_expires_at)
        if self.status in {
            "claimed",
            "running",
            "awaiting_protected_approval",
            "awaiting_transaction_approval",
            "cancel_requested",
        }:
            if any(value is None for value in claim_fields) or self.claim_fence < 1:
                raise ValueError(
                    "claimed, running, and cancel-requested requests require a complete "
                    "fenced claim"
                )
        elif any(value is not None for value in claim_fields):
            raise ValueError("only claimed and running requests may carry a claim")
        if (
            self.status
            in {
                "running",
                "awaiting_protected_approval",
                "awaiting_transaction_approval",
                "cancel_requested",
            }
            and self.run_id is None
        ):
            raise ValueError("active execution requests require run_id")
        return self


class ExecutionActivity(_StrictModel):
    id: int = Field(ge=1)
    request_id: str
    profile_label: ProfileLabel
    kind: ExecutionActivityKind
    from_status: ExecutionStatus | None = None
    to_status: ExecutionStatus
    worker_id: str | None = Field(default=None, max_length=200)
    summary: str = Field(min_length=1, max_length=2_000)
    fence: int = Field(ge=0)
    created_at: datetime

    @field_validator("request_id")
    @classmethod
    def _id(cls, value: str) -> str:
        if _REQUEST_ID.fullmatch(value) is None:
            raise ValueError("invalid execution request id")
        return value

    @field_validator("created_at")
    @classmethod
    def _created(cls, value: datetime) -> datetime:
        _aware(value, "created_at")
        return value


class ExecutionResolution(_StrictModel):
    id: int = Field(ge=1)
    request_id: str
    profile_label: ProfileLabel
    disposition: ExecutionResolutionDisposition
    actor: str = Field(min_length=1, max_length=200)
    note: str = Field(min_length=1, max_length=2_000)
    created_at: datetime

    @field_validator("request_id")
    @classmethod
    def _id(cls, value: str) -> str:
        return validate_execution_id(value)

    @field_validator("created_at")
    @classmethod
    def _created(cls, value: datetime) -> datetime:
        _aware(value, "created_at")
        return value


def validate_execution_id(value: str) -> str:
    if _REQUEST_ID.fullmatch(value) is None:
        raise ValueError("invalid execution request id")
    return value


def is_retryable_execution_status(status: ExecutionStatus) -> bool:
    """Return whether durable evidence permits creation of a child attempt."""

    return status in {"succeeded", "failed", "blocked", "cancelled"}


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be UTC")
