"""Strict serializable contracts for managed job schedules."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ricky.jobs.spec import NAME_PATTERN
from ricky.jobs.types import JobApprovalEnvelope
from ricky.profiles import ProfileResourceRef, ProfileScope

SCHEDULE_ID_PATTERN = r"sched_[0-9a-f]{24}"
_CRON_LIMITS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_CRON_TOKEN = re.compile(r"^(?:\*|[0-9]+(?:-[0-9]+)?)(?:/[0-9]+)?$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def validate_schedule_id(value: str) -> str:
    """Validate one opaque schedule id before it reaches a path or command."""

    if re.fullmatch(SCHEDULE_ID_PATTERN, value) is None:
        raise ValueError("invalid opaque schedule id")
    return value


def validate_cron_expression(value: str) -> str:
    """Validate the numeric five-field cron subset supported by M20."""

    if not value or value != value.strip():
        raise ValueError("cron expression cannot be blank or have outer whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("cron expression cannot contain control characters")
    if any(character in value for character in ("#", "=", "%")):
        raise ValueError("cron expression cannot contain comments, assignments, or commands")
    fields = value.split()
    if len(fields) != 5:
        raise ValueError("cron expression must contain exactly five fields")
    for position, (field, limits) in enumerate(zip(fields, _CRON_LIMITS, strict=True), start=1):
        _validate_cron_field(field, limits, position)
    return " ".join(fields)


def _validate_cron_field(field: str, limits: tuple[int, int], position: int) -> None:
    for component in field.split(","):
        if not component or _CRON_TOKEN.fullmatch(component) is None:
            raise ValueError(f"cron field {position} has unsupported syntax: {field!r}")
        base, separator, step_text = component.partition("/")
        if separator:
            step = int(step_text)
            if step < 1 or step > limits[1] - limits[0] + 1:
                raise ValueError(f"cron field {position} has an invalid step: {step_text}")
        if base == "*":
            continue
        start_text, dash, end_text = base.partition("-")
        start = int(start_text)
        end = int(end_text) if dash else start
        if not limits[0] <= start <= limits[1] or not limits[0] <= end <= limits[1]:
            raise ValueError(f"cron field {position} is outside {limits[0]}..{limits[1]}")
        if dash and start > end:
            raise ValueError(f"cron field {position} range must be ascending")


class ScheduleSpec(_StrictModel):
    """One desired, approved mapping from a cron expression to a named job."""

    version: Literal[1] = 1
    id: str = Field(pattern=f"^{SCHEDULE_ID_PATTERN}$")
    job_name: str = Field(min_length=3, max_length=200)
    cron: str
    enabled: bool = True
    project_root: str = Field(min_length=1, max_length=4_096)
    profile_scope: ProfileScope
    approved_spec_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_runtime_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_authority: JobApprovalEnvelope | None = None
    approved_cron: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("cron")
    @classmethod
    def _cron(cls, value: str) -> str:
        return validate_cron_expression(value)

    @field_validator("approved_cron")
    @classmethod
    def _approved_cron(cls, value: str | None) -> str | None:
        return validate_cron_expression(value) if value is not None else None

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return validate_schedule_id(value)

    @field_validator("job_name")
    @classmethod
    def _qualified_job_name(cls, value: str) -> str:
        profile, separator, name = value.partition("/")
        if not separator:
            raise ValueError("scheduled jobs require a profile-qualified name")
        ProfileResourceRef(profile=profile, name=name)
        if re.fullmatch(NAME_PATTERN, name) is None:
            raise ValueError("scheduled job local name is invalid")
        return value

    @field_validator("project_root")
    @classmethod
    def _absolute_root(cls, value: str) -> str:
        from pathlib import Path

        path = Path(value)
        if not path.is_absolute() or any(ord(character) < 32 for character in value):
            raise ValueError("project_root must be an absolute path without controls")
        resolved = path.resolve()
        if str(path) != str(resolved):
            raise ValueError("project_root must be resolved and canonical")
        return str(resolved)

    @field_validator("created_at", "updated_at")
    @classmethod
    def _aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("schedule timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _ordered_times(self) -> ScheduleSpec:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        owner = self.job_name.partition("/")[0]
        if not self.profile_scope.includes(owner):
            raise ValueError("scheduled job owner must be in the pinned profile scope")
        return self


class ScheduleFile(_StrictModel):
    """Canonical schedules.toml document."""

    version: Literal[1] = 1
    schedules: list[ScheduleSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> ScheduleFile:
        ids = [schedule.id for schedule in self.schedules]
        if len(ids) != len(set(ids)):
            raise ValueError("schedule ids must be unique")
        return self


ScheduleState = Literal[
    "ready",
    "disabled",
    "validation_required",
    "lineage_required",
    "approval_required",
    "unavailable",
    "not_installed",
]


class ScheduleInspection(_StrictModel):
    """Current non-secret validation and approval state for one schedule."""

    schedule: ScheduleSpec
    state: ScheduleState
    current_spec_digest: str | None = None
    current_runtime_policy_digest: str | None = None
    detail: str | None = Field(default=None, max_length=2_000)


class ScheduleSyncReport(_StrictModel):
    """Verified result of one desired-state reconciliation."""

    changed: bool
    installed: list[str] = Field(default_factory=list)
    disabled: list[str] = Field(default_factory=list)
    validation_required: list[str] = Field(default_factory=list)
    lineage_required: list[str] = Field(default_factory=list)
    approval_required: list[str] = Field(default_factory=list)
    unavailable: list[str] = Field(default_factory=list)
    backup_path: str | None = None
    fragment_path: str


class DoctorIssue(_StrictModel):
    severity: Literal["warning", "error"]
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=2_000)
    schedule_id: str | None = None


class ScheduleDoctorReport(_StrictModel):
    healthy: bool
    executable_path: str | None = None
    desired_count: int = Field(ge=0)
    installed_count: int = Field(ge=0)
    issues: list[DoctorIssue] = Field(default_factory=list)
