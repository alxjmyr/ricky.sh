"""Canonical durable-task boundary models."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from ricky.profiles import ProfileName

TaskExecutionMode = Literal["agent", "joint", "user"]
TaskStatus = Literal["open", "in_progress", "waiting", "blocked", "completed", "cancelled"]
TaskWaitingOn = Literal["agent", "user", "external", "time"]
TaskActivityKind = Literal[
    "created",
    "claimed",
    "lease_renewed",
    "progressed",
    "waiting",
    "blocked",
    "completed",
    "cancelled",
    "reopened",
    "released",
    "lease_expired",
    "artifact_created",
    "artifact_updated",
    "tags_updated",
]
TaskAuthority = Literal[
    "agent_autonomy",
    "joint_work",
    "direct_user_instruction",
    "deterministic_user_command",
    "system_recovery",
]

TaskPriority = Annotated[int, Field(ge=-100, le=100)]
TaskTag = Annotated[str, Field(min_length=1, max_length=100)]
_TASK_ID = re.compile(r"^task_[0-9a-f]{32}$")
_TAG = re.compile(r"^[a-z0-9][a-z0-9._-]*(?::[a-z0-9][a-z0-9._-]*)*$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskLease(_StrictModel):
    id: str = Field(min_length=1)
    holder_session_id: str = Field(min_length=1)
    epoch: int = Field(ge=1)
    acquired_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def _validate_times(self) -> TaskLease:
        _require_aware(self.acquired_at, "acquired_at")
        _require_aware(self.expires_at, "expires_at")
        if self.expires_at <= self.acquired_at:
            raise ValueError("lease expires_at must be after acquired_at")
        return self


class DurableTask(_StrictModel):
    id: str
    profile: ProfileName
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    closure_criteria: str = Field(min_length=1)
    execution_mode: TaskExecutionMode
    status: TaskStatus
    waiting_on: TaskWaitingOn | None = None
    current_summary: str | None = None
    next_action: str | None = None
    priority: TaskPriority = 0
    tags: list[TaskTag] = Field(default_factory=list, max_length=50)
    due_at: datetime | None = None
    completion_summary: str | None = None
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    lease: TaskLease | None = None

    @field_validator(
        "id",
        "title",
        "objective",
        "closure_criteria",
        "current_summary",
        "next_action",
        "completion_summary",
    )
    @classmethod
    def _trim_text(cls, value: str | None, info: object) -> str | None:
        del info
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("text values must be non-empty after trimming")
        return value

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not _TASK_ID.fullmatch(value):
            raise ValueError("invalid durable task id")
        return value

    @field_validator("tags", mode="before")
    @classmethod
    def _canonical_tags(cls, value: object) -> list[str]:
        return canonicalize_task_tags(value)

    @model_validator(mode="after")
    def _validate_state(self) -> DurableTask:
        for name in ("created_at", "updated_at", "due_at", "completed_at", "cancelled_at"):
            value = getattr(self, name)
            if value is not None:
                _require_aware(value, name)
        if self.status == "waiting":
            if self.waiting_on is None or self.next_action is None:
                raise ValueError("waiting tasks require waiting_on and next_action")
        elif self.waiting_on is not None:
            raise ValueError("waiting_on is valid only for waiting tasks")
        if self.status == "completed":
            if self.completion_summary is None or self.completed_at is None:
                raise ValueError("completed tasks require completion summary and timestamp")
        elif self.completion_summary is not None or self.completed_at is not None:
            raise ValueError("completion fields are valid only for completed tasks")
        if self.status == "cancelled":
            if self.cancelled_at is None:
                raise ValueError("cancelled tasks require cancelled_at")
        elif self.cancelled_at is not None:
            raise ValueError("cancelled_at is valid only for cancelled tasks")
        return self


class TaskActivity(_StrictModel):
    id: int = Field(ge=1)
    task_id: str
    profile: ProfileName
    kind: TaskActivityKind
    authority: TaskAuthority
    executor_id: str = Field(min_length=1, max_length=512)
    session_id: str | None = Field(default=None, max_length=512)
    from_status: TaskStatus | None = None
    to_status: TaskStatus | None = None
    summary: str = Field(min_length=1, max_length=4_000)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    task_revision: int = Field(ge=1)
    created_at: datetime

    @field_validator("task_id")
    @classmethod
    def _validate_task_id(cls, value: str) -> str:
        if not _TASK_ID.fullmatch(value):
            raise ValueError("invalid durable task id")
        return value

    @field_validator("summary")
    @classmethod
    def _trim_summary(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("activity summary must be non-empty")
        return value

    @field_validator("created_at")
    @classmethod
    def _aware_created_at(cls, value: datetime) -> datetime:
        _require_aware(value, "created_at")
        return value


class TaskSearchQuery(_StrictModel):
    text: str | None = None
    statuses: list[TaskStatus] = Field(default_factory=list)
    execution_modes: list[TaskExecutionMode] = Field(default_factory=list)
    waiting_on: list[TaskWaitingOn] = Field(default_factory=list)
    tags_any: list[TaskTag] = Field(default_factory=list, max_length=50)
    tags_all: list[TaskTag] = Field(default_factory=list, max_length=50)
    tags_none: list[TaskTag] = Field(default_factory=list, max_length=50)
    due_before: datetime | None = None
    include_closed: bool = False
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0, le=1_000_000)

    @field_validator("text")
    @classmethod
    def _trim_search_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("due_before")
    @classmethod
    def _aware_due_before(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            _require_aware(value, "due_before")
        return value

    @field_validator("tags_any", "tags_all", "tags_none", mode="before")
    @classmethod
    def _canonical_query_tags(cls, value: object) -> list[str]:
        return canonicalize_task_tags(value)


class TaskArtifactEntry(_StrictModel):
    path: str = Field(min_length=1)
    size: int = Field(ge=0)
    modified_at: datetime
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("modified_at")
    @classmethod
    def _aware_modified_at(cls, value: datetime) -> datetime:
        _require_aware(value, "modified_at")
        return value


class TaskDetail(_StrictModel):
    task: DurableTask
    recent_activity: list[TaskActivity]
    artifact_files: list[TaskArtifactEntry]


def validate_task_id(value: str) -> str:
    """Validate a durable-task id at store and artifact boundaries."""

    if not _TASK_ID.fullmatch(value):
        raise ValueError("invalid durable task id")
    return value


def canonicalize_task_tags(value: object) -> list[str]:
    """Normalize an open task-tag vocabulary into stable exact keys."""

    if value is None:
        return []
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError("task tags must be a list")
    canonical: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            raise ValueError("task tags must be strings")
        tag = raw.strip().lower()
        if len(tag) > 100 or _TAG.fullmatch(tag) is None:
            raise ValueError(
                "task tags must contain colon-separated lowercase letters, digits, '.', '_', or '-'"
            )
        canonical.add(tag)
    if len(canonical) > 50:
        raise ValueError("a durable task may have at most 50 tags")
    return sorted(canonical)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
