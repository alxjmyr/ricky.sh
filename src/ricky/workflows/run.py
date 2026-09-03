"""Persistent JSON-safe run records for Workflow."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ricky.llm.types import Usage
from ricky.profiles import ProfileResourceRef, ProfileScope

StepStatus = Literal[
    "pending",
    "ready",
    "running",
    "completed",
    "skipped",
    "failed",
    "blocked",
    "interrupted",
    "in_doubt",
]
RunStatus = Literal[
    "pending",
    "running",
    "completed",
    "completed_with_errors",
    "failed",
    "interrupted",
    "in_doubt",
    "abandoned",
]
AttemptStatus = Literal["running", "completed", "failed", "interrupted"]
EffectStatus = Literal[
    "prepared",
    "dispatched",
    "succeeded",
    "failed",
    "in_doubt",
    "would_dispatch",
    "reconciled",
]
ItemStatus = Literal[
    "pending",
    "running",
    "completed",
    "completed_with_errors",
    "failed",
    "interrupted",
    "blocked",
]
ItemKey = str | int | float | bool

TERMINAL_STEP_STATUSES: frozenset[StepStatus] = frozenset(
    {"completed", "skipped", "failed", "blocked", "interrupted", "in_doubt"}
)
SUCCESS_STEP_STATUSES: frozenset[StepStatus] = frozenset({"completed", "skipped"})


def utc_now() -> datetime:
    return datetime.now(UTC)


class _RunModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowError(_RunModel):
    """One bounded typed failure record."""

    category: Literal[
        "compile",
        "reference",
        "condition",
        "invalid_output",
        "provider_error",
        "timeout",
        "tool_error",
        "permission_denied",
        "approval_denied",
        "checkpoint",
        "scheduler",
        "interrupted",
        "in_doubt",
    ]
    message: str
    retryable: bool = False
    detail: str | None = None


class StepAttempt(_RunModel):
    """One bounded execution attempt for a step record."""

    number: int = Field(ge=1)
    status: AttemptStatus = "running"
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    error: WorkflowError | None = None
    usage: Usage = Field(default_factory=Usage)


class StepRecord(_RunModel):
    """Persistent state and typed output for one execution address."""

    step_id: str
    execution_address: str
    kind: str
    status: StepStatus = "pending"
    output: JsonValue = None
    error: WorkflowError | None = None
    attempts: list[StepAttempt] = Field(default_factory=list)
    condition_result: bool | None = None
    blocked_by: list[str] = Field(default_factory=list)
    input_references: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class ItemRunRecord(_RunModel):
    """Persistent nested-DAG state for one foreach source item."""

    foreach_step_id: str
    key: ItemKey
    index: int = Field(ge=0)
    source: JsonValue
    status: ItemStatus = "pending"
    steps: dict[str, StepRecord] = Field(default_factory=dict)
    error: WorkflowError | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class EffectJournalEntry(_RunModel):
    """Write-ahead and completion record for one observable tool mutation."""

    id: str = Field(default_factory=lambda: f"effect_{uuid4().hex}")
    step_id: str
    execution_address: str
    tool_name: str
    normalized_args: dict[str, JsonValue] = Field(default_factory=dict)
    risk: Literal["read_only", "mutating", "destructive"]
    effect_kind: Literal["ricky_state", "external"] = "external"
    status: EffectStatus = "prepared"
    idempotency_key: str | None = None
    safe_replay: bool = False
    result_summary: str | None = None
    prepared_at: datetime = Field(default_factory=utc_now)
    dispatched_at: datetime | None = None
    finished_at: datetime | None = None


class WorkflowSourceIdentity(_RunModel):
    """The bundle source used to compile a run."""

    path: str
    # ``project`` is retained only so checkpoints written before bundled
    # discovery still load. Ricky no longer produces it.
    scope: Literal["project", "user", "bundled", "fixture"]
    content_digest: str
    resource: ProfileResourceRef


class WorkflowInvocation(_RunModel):
    """A validated invocation queued by the model-facing start tool."""

    name: str
    args: dict[str, JsonValue] = Field(default_factory=dict)
    started: bool = False


class WorkflowRun(_RunModel):
    """Complete persistent workflow run state without private model transcripts."""

    id: str = Field(default_factory=lambda: f"workflow_{uuid4().hex}")
    workflow_name: str
    version: Literal[2] = 2
    source: WorkflowSourceIdentity
    provider: str
    model: str
    profile_scope: ProfileScope
    # ``project`` is retained only so checkpoints written before bundled
    # discovery still load. Ricky now always stores runs as ``user``.
    storage_scope: Literal["project", "user"] = "user"
    trigger: dict[str, JsonValue] = Field(default_factory=dict)
    graph_fingerprint: str
    status: RunStatus = "pending"
    steps: dict[str, StepRecord] = Field(default_factory=dict)
    item_runs: dict[str, list[ItemRunRecord]] = Field(default_factory=dict)
    effect_journal: list[EffectJournalEntry] = Field(default_factory=list)
    cumulative_usage: Usage = Field(default_factory=Usage)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None


def step_records_for(steps: Sequence[Any]) -> dict[str, StepRecord]:
    """Build pending records from step-like objects with id and kind fields."""

    records: dict[str, StepRecord] = {}
    for step in steps:
        step_id = step.id
        kind = step.kind
        records[step_id] = StepRecord(
            step_id=step_id,
            execution_address=step_id,
            kind=kind,
        )
    return records
