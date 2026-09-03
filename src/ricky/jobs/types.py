"""Serializable job run and validation contracts."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from ricky.profiles import ProfileLabel, ProfileResourceRef, ProfileScope
from ricky.tools.base import EffectDisposition

RunOutcome = Literal[
    "succeeded",
    "failed",
    "budget_exceeded",
    "interrupted",
    "uncertain",
    "skipped_locked",
    "approval_required",
]
RunTrigger = Literal["manual", "schedule", "execution"]
ResultNotificationPolicy = Literal["always", "never"]
WorkflowRunStatus = Literal[
    "pending",
    "running",
    "completed",
    "completed_with_errors",
    "failed",
    "interrupted",
    "in_doubt",
    "abandoned",
]
MAX_FINAL_MESSAGE_CHARS = 100_000
MAX_RUN_ERROR_CHARS = 2_000


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobRun(_StrictModel):
    """One persisted named or ad-hoc launch attempt."""

    id: str = Field(min_length=1, max_length=100)
    job_name: str | None = Field(default=None, max_length=200)
    spec_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=500)
    profile_scope: ProfileScope
    session_id: str = Field(min_length=1, max_length=100)
    outcome: RunOutcome | None = None
    started_at: datetime
    finished_at: datetime | None = None
    iterations: int = Field(default=0, ge=0)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    final_message: str | None = Field(default=None, max_length=MAX_FINAL_MESSAGE_CHARS)
    error: str | None = Field(default=None, max_length=MAX_RUN_ERROR_CHARS)
    transcript_path: str | None = Field(default=None, max_length=4_096)
    dry_run: bool = False
    effect_calls: int = Field(default=0, ge=0)
    runtime_policy_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    result_notification: ResultNotificationPolicy = "always"
    context_lineage: int | None = Field(default=None, ge=1)
    context_revision: int | None = Field(default=None, ge=1)
    context_definition_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    trigger: RunTrigger = "manual"
    trigger_id: str | None = Field(default=None, min_length=1, max_length=100)
    workflow_name: str | None = Field(default=None, min_length=1, max_length=200)
    workflow_args: dict[str, JsonValue] | None = None
    workflow_run_id: str | None = Field(default=None, min_length=1, max_length=100)
    workflow_status: WorkflowRunStatus | None = None

    @model_validator(mode="after")
    def _workflow_link(self) -> JobRun:
        linked = (
            self.workflow_args is not None
            or self.workflow_run_id is not None
            or self.workflow_status is not None
        )
        if linked and self.workflow_name is None:
            raise ValueError("workflow run fields require workflow_name")
        if self.workflow_run_id is not None and self.workflow_args is None:
            raise ValueError("workflow_run_id requires resolved workflow_args")
        return self


class JobAction(_StrictModel):
    """One append-only external action attempt guarded by deterministic identity."""

    id: str = Field(min_length=1, max_length=100)
    job_name: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=100)
    profile_label: ProfileLabel
    action_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: str = Field(min_length=1, max_length=200)
    target: str = Field(min_length=1, max_length=500)
    occurrence: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=2_000)
    status: Literal["reserved", "performed", "not_performed", "in_doubt"]
    provider_reference: str | None = Field(default=None, max_length=500)
    created_at: datetime
    updated_at: datetime
    grant_id: str | None = Field(default=None, max_length=100)
    """Set when a delegation grant reserved this action."""
    task_id: str | None = Field(default=None, max_length=100)
    """Durable task that owns a delegated action's effect namespace."""


class ActionResolution(_StrictModel):
    """User-authored audit append for an ambiguous external action."""

    id: int = Field(ge=1)
    action_id: str
    profile_label: ProfileLabel
    disposition: EffectDisposition
    actor: str = Field(min_length=1, max_length=200)
    created_at: datetime


class JobValidationError(_StrictModel):
    """One bounded bundle or runtime-profile validation error."""

    source_path: str
    message: str = Field(max_length=2_000)


class JobValidationReport(_StrictModel):
    """Provider-free validation result for one discovered job."""

    name: str
    source_path: str
    spec_valid: bool
    tools_checked: bool = False
    tools_available: bool | None = None
    errors: list[JobValidationError] = Field(default_factory=list)


class JobContextEvidence(_StrictModel):
    """Persisted meaning assigned to one revision inside a context lane."""

    revision: int = Field(ge=1)
    definition_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class JobApprovalTool(_StrictModel):
    """One exact tool surface included in repeated unattended approval."""

    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    risk: Literal["read_only", "mutating", "destructive"]
    effect_kind: Literal["none", "ricky_state", "external"]
    unattended: Literal["allowed", "forbidden"]
    state_guard_id: str | None = Field(default=None, max_length=200)


class JobApprovalEnvelope(_StrictModel):
    """Approval-bearing authority and trust boundary for one job revision."""

    provider: str = Field(min_length=1, max_length=100)
    tools: dict[str, JobApprovalTool] = Field(default_factory=dict, max_length=100)
    mutating_tools: tuple[str, ...] = Field(default=(), max_length=100)
    source_scopes: dict[str, str] = Field(default_factory=dict, max_length=50)
    google_accounts: dict[str, str] = Field(default_factory=dict, max_length=100)
    workflow_args: dict[str, JsonValue] | None = None
    browser_scope: str | None = Field(default=None, max_length=20_000)
    effect_calls: int = Field(ge=0, le=1_000)

    @field_validator("source_scopes")
    @classmethod
    def _source_payloads(cls, value: dict[str, str]) -> dict[str, str]:
        for payload in value.values():
            if len(payload) > 10_000:
                raise ValueError("source approval scope exceeds 10000 characters")
            try:
                decoded = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ValueError("source approval scope must be canonical JSON") from exc
            if not isinstance(decoded, dict):
                raise ValueError("source approval scope must contain a JSON object")
        return value

    @field_validator("browser_scope")
    @classmethod
    def _browser_payload(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("browser approval scope must be canonical JSON") from exc
        if (
            not isinstance(decoded, dict)
            or json.dumps(decoded, sort_keys=True, separators=(",", ":")) != value
        ):
            raise ValueError("browser approval scope must be canonical JSON")
        return value

    @field_validator("google_accounts")
    @classmethod
    def _qualified_google_accounts(cls, value: dict[str, str]) -> dict[str, str]:
        for name, email in value.items():
            ProfileResourceRef.from_qualified(name)
            if not email.strip() or len(email) > 320:
                raise ValueError("Google account approval identities require a bounded email")
        return dict(sorted(value.items()))
