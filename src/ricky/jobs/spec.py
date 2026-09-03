"""Strict versioned job bundle specification."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from ricky.capabilities.guardrails import CompiledGuardrail
from ricky.durable_tasks.types import (
    TaskExecutionMode,
    TaskStatus,
    TaskTag,
    TaskWaitingOn,
    canonicalize_task_tags,
)
from ricky.jobs.types import ResultNotificationPolicy
from ricky.profiles import ProfileResourceRef

NAME_PATTERN = r"[a-z0-9][a-z0-9_-]{0,63}"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobBudget(_StrictModel):
    """Hard per-run bounds supported by the current agent harness."""

    wall_clock_seconds: float = Field(default=600.0, gt=0, le=86_400)
    iterations: int = Field(default=10, ge=1, le=100)
    max_completion_tokens_per_request: int = Field(default=4_096, ge=1, le=1_000_000)
    effect_calls: int = Field(default=0, ge=0, le=1_000)


class JobTools(_StrictModel):
    """Exact tool names exposed to a job run."""

    allow: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("allow")
    @classmethod
    def _unique_names(cls, value: list[str]) -> list[str]:
        if any(not name.strip() for name in value):
            raise ValueError("tool names cannot be empty")
        if len(set(value)) != len(value):
            raise ValueError("tool names must be unique")
        return value


class JobPermissions(_StrictModel):
    """Exact recurring mutation names approved ahead of each run."""

    allow_mutating: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("allow_mutating")
    @classmethod
    def _unique_names(cls, value: list[str]) -> list[str]:
        if any(not name.strip() for name in value):
            raise ValueError("mutating tool names cannot be empty")
        if len(set(value)) != len(value):
            raise ValueError("mutating tool names must be unique")
        return value


class JobWorkflow(_StrictModel):
    """One explicit workflow target plus authored, locked invocation arguments."""

    name: str = Field(min_length=1, max_length=200)
    args: dict[str, JsonValue] = Field(default_factory=dict, max_length=100)

    @field_validator("name")
    @classmethod
    def _workflow_name(cls, value: str) -> str:
        profile, separator, local_name = value.partition("/")
        if re.fullmatch(NAME_PATTERN, local_name if separator else value) is None:
            raise ValueError("workflow name is invalid")
        if separator:
            ProfileResourceRef(profile=profile, name=local_name)
        return value

    @field_validator("args")
    @classmethod
    def _json_args(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if any(re.fullmatch(NAME_PATTERN, name) is None for name in value):
            raise ValueError("workflow argument names must use the workflow name format")
        return json.loads(json.dumps(value, allow_nan=False))


class JobContext(_StrictModel):
    """Authored acknowledgement and continuity lane for model-visible history."""

    lineage: int = Field(default=1, ge=1)
    revision: int = Field(default=1, ge=1)


class JobBrowser(_StrictModel):
    """Authored ceiling for one named job's read-oriented browser."""

    resource: ProfileResourceRef | None = None
    allow_public_https_research: bool = False
    allowed_origins: tuple[str, ...] = Field(default=(), max_length=100)
    allow_masked_visual_observations: bool = False

    @field_validator("resource", mode="before")
    @classmethod
    def _resource(cls, value: object) -> object:
        if isinstance(value, str):
            return ProfileResourceRef.from_qualified(value)
        return value

    @field_validator("allowed_origins")
    @classmethod
    def _origins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        from ricky.executions.browser import BrowserResourcePin

        # Reuse the durable browser contract's canonical HTTPS-origin validator.
        validated = BrowserResourcePin(
            resource=ProfileResourceRef(profile="default", name="validation"),
            kind="persistent",
            configuration_digest="0" * 64,
            authenticated_origin_ceiling=values,
        )
        return validated.authenticated_origin_ceiling

    @model_validator(mode="after")
    def _launch_ceiling(self) -> JobBrowser:
        if self.resource is not None and not self.allowed_origins:
            raise ValueError(
                "a named persistent browser requires at least one exact allowed_origin"
            )
        return self


class PinnedExecutionRuntime(_StrictModel):
    """Neutral exact-resource selection for a compiled execution contract."""

    tool_digests: dict[str, str] = Field(default_factory=dict, max_length=100)
    tool_effect_kinds: dict[str, Literal["none", "ricky_state", "external"]] = Field(
        default_factory=dict, max_length=100
    )
    tool_unattended: dict[str, Literal["allowed", "forbidden"]] = Field(
        default_factory=dict, max_length=100
    )
    tool_state_guards: dict[str, str | None] = Field(default_factory=dict, max_length=100)
    skill_digests: dict[str, str] = Field(default_factory=dict, max_length=100)
    skill_instructions: dict[str, str] = Field(default_factory=dict, max_length=100)
    skill_bundle_paths: dict[str, str] = Field(default_factory=dict, max_length=100)
    guardrails: tuple[CompiledGuardrail, ...] = Field(default=(), max_length=20)
    guardrail_tools: dict[str, tuple[str, ...]] = Field(default_factory=dict, max_length=20)
    authorized_mutating_tools: tuple[str, ...] = Field(default=(), max_length=100)
    memory_index: bool = False

    @field_validator("tool_digests", "skill_digests")
    @classmethod
    def _digests(cls, value: dict[str, str]) -> dict[str, str]:
        if any(re.fullmatch(r"[0-9a-f]{64}", digest) is None for digest in value.values()):
            raise ValueError("pinned runtime resource digests must be SHA-256 hex")
        return value

    @model_validator(mode="after")
    def _skills(self) -> PinnedExecutionRuntime:
        if set(self.skill_instructions) != set(self.skill_digests) or (
            set(self.skill_bundle_paths) != set(self.skill_digests)
        ):
            raise ValueError("every pinned skill requires exact instructions and a snapshot bundle")
        guardrail_ids = {item.capability_id for item in self.guardrails}
        if len(guardrail_ids) != len(self.guardrails):
            raise ValueError("pinned runtime guardrails must be unique by capability")
        if set(self.guardrail_tools) != guardrail_ids:
            raise ValueError("every pinned guardrail requires an exact tool mapping")
        if any(
            not names or len(names) != len(set(names)) for names in self.guardrail_tools.values()
        ):
            raise ValueError("pinned guardrail tool mappings must be non-empty and unique")
        if len(self.authorized_mutating_tools) != len(set(self.authorized_mutating_tools)):
            raise ValueError("authorized mutating tool names must be unique")
        if not set(self.authorized_mutating_tools) <= set(self.tool_digests):
            raise ValueError("authorized mutating tools must belong to the pinned runtime")
        metadata_sets = (
            set(self.tool_effect_kinds),
            set(self.tool_unattended),
            set(self.tool_state_guards),
        )
        if any(names != set(self.tool_digests) for names in metadata_sets):
            raise ValueError("pinned tool metadata must cover every exact tool")
        return self


class TaskSourceSpec(_StrictModel):
    """Structured durable-task eligibility query, never an assignment."""

    name: str = Field(pattern=f"^{NAME_PATTERN}$")
    tags_any: list[TaskTag] = Field(default_factory=list, max_length=50)
    tags_all: list[TaskTag] = Field(default_factory=list, max_length=50)
    tags_none: list[TaskTag] = Field(default_factory=list, max_length=50)
    execution_modes: list[TaskExecutionMode] = Field(default_factory=list)
    statuses: list[TaskStatus] = Field(default_factory=list)
    waiting_on: list[TaskWaitingOn] = Field(default_factory=list)
    due_before: datetime | None = None
    text: str | None = Field(default=None, max_length=500)
    limit: int = Field(default=10, ge=1, le=500)
    reconsider_after_hours: float = Field(default=24, ge=0, le=8_760)

    @field_validator("tags_any", "tags_all", "tags_none", mode="before")
    @classmethod
    def _tags(cls, value: object) -> list[str]:
        return canonicalize_task_tags(value)

    @field_validator("text")
    @classmethod
    def _text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("due_before")
    @classmethod
    def _aware_due_before(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("task source due_before must include a timezone offset")
        return value.astimezone(UTC)


class SlackStreamSourceSpec(_StrictModel):
    """Typed Slack channel stream configuration."""

    name: str = Field(pattern=f"^{NAME_PATTERN}$")
    adapter: Literal["slack_channel"]
    channel_id: str = Field(pattern=r"^[CDG][A-Z0-9]+$", max_length=100)
    initial_lookback_hours: float = Field(default=24, gt=0, le=8_760)
    item_limit: int = Field(default=100, ge=1, le=500)


class JobSpec(_StrictModel):
    """Version 3 agent or workflow-backed recurring job definition."""

    version: int
    name: str = Field(pattern=f"^{NAME_PATTERN}$")
    description: str = Field(min_length=1, max_length=500)
    provider: str | None = None
    model: str | None = None
    goal: str | None = Field(default=None, min_length=1, max_length=50_000)
    workflow: JobWorkflow | None = None
    result_notification: ResultNotificationPolicy = "always"
    context: JobContext = Field(default_factory=JobContext)
    browser: JobBrowser | None = None
    budget: JobBudget = Field(default_factory=JobBudget)
    tools: JobTools = Field(default_factory=JobTools)
    permissions: JobPermissions = Field(default_factory=JobPermissions)
    task_sources: list[TaskSourceSpec] = Field(default_factory=list, max_length=25)
    stream_sources: list[SlackStreamSourceSpec] = Field(default_factory=list, max_length=25)

    @field_validator("version")
    @classmethod
    def _version_three(cls, value: int) -> int:
        if value != 3:
            raise ValueError("job version must be 3")
        return value

    @field_validator("goal")
    @classmethod
    def _nonblank_goal(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("goal cannot be blank")
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def _validate_recurring_contract(self) -> JobSpec:
        exposed = set(self.tools.allow)
        mutating = set(self.permissions.allow_mutating)
        if self.workflow is None and not mutating.issubset(exposed):
            missing = ", ".join(sorted(mutating - exposed))
            raise ValueError(f"allow_mutating must be a subset of tools.allow: {missing}")
        if self.workflow is not None:
            if exposed:
                raise ValueError(
                    "workflow-backed jobs derive tools from the workflow; tools.allow must be empty"
                )
            if self.task_sources or self.stream_sources:
                raise ValueError(
                    "workflow-backed jobs cannot declare recurring sources; pass context through "
                    "workflow arguments"
                )
            if self.browser is not None:
                raise ValueError("workflow-backed jobs cannot declare a browser")
        for label, sources in (
            ("task source", self.task_sources),
            ("stream source", self.stream_sources),
        ):
            names = [source.name for source in sources]
            if len(names) != len(set(names)):
                raise ValueError(f"{label} names must be unique")
        return self


def ad_hoc_job_spec(
    *,
    provider: str,
    model: str,
    goal: str,
    tools: list[str],
    budget: JobBudget,
) -> JobSpec:
    """Build a v3-shaped but recurrence-free read-only ad-hoc run."""

    return JobSpec(
        version=3,
        name="ad-hoc",
        description="One ad-hoc bounded read-only job.",
        provider=provider,
        model=model,
        goal=goal,
        budget=budget,
        tools=JobTools(allow=tools),
    )


def validate_job_name(name: str) -> str:
    """Validate one path-safe stable job name."""

    if re.fullmatch(NAME_PATTERN, name) is None:
        raise ValueError(
            "job name must start with a lowercase letter or digit and contain only "
            "lowercase letters, digits, '-' and '_' (maximum 64 characters)"
        )
    return name
