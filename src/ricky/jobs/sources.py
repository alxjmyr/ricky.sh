"""Typed recurring input, candidate-batch, and disposition contracts."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from ricky.durable_tasks.types import (
    TaskExecutionMode,
    TaskPriority,
    TaskStatus,
    TaskTag,
    TaskWaitingOn,
)
from ricky.profiles import ProfileLabel

BatchKind = Literal["stream", "task_pool"]
ItemDispositionKind = Literal["declined", "blocked", "effect_reserved", "escalated"]
CandidateDispositionKind = Literal[
    "not_actionable",
    "lease_conflict",
    "progressed",
    "waiting",
    "completed",
    "blocked",
    "escalated",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceItem(_StrictModel):
    """One bounded immutable stream item presented to a recurring run."""

    id: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=100_000)
    occurred_at: datetime
    data: dict[str, JsonValue] = Field(default_factory=dict)
    required: bool = True


class CollectedBatch(_StrictModel):
    """Adapter result with an owned input and proposed next cursor."""

    items: list[SourceItem] = Field(max_length=500)
    input_cursor: JsonValue = None
    next_cursor: JsonValue = None
    upper_bound: datetime
    complete: bool


class TaskCandidate(_StrictModel):
    """Bounded durable-task snapshot; activity and artifacts stay in their store."""

    id: str
    revision: int = Field(ge=1)
    title: str
    execution_mode: TaskExecutionMode
    status: TaskStatus
    waiting_on: TaskWaitingOn | None = None
    priority: TaskPriority = 0
    due_at: datetime | None = None
    tags: list[TaskTag] = Field(default_factory=list, max_length=50)
    current_summary: str | None = None
    next_action: str | None = None

    @property
    def identity(self) -> str:
        return f"{self.id}@{self.revision}"


class CandidateBatch(_StrictModel):
    """One persisted, deduplicated work-pool snapshot."""

    candidates: list[TaskCandidate] = Field(max_length=500)


class PersistedBatch(_StrictModel):
    """Metadata for a source payload persisted before model reasoning."""

    id: str = Field(min_length=1, max_length=100)
    run_id: str = Field(min_length=1, max_length=100)
    profile_label: ProfileLabel
    job_name: str = Field(min_length=1, max_length=200)
    source_name: str = Field(min_length=1, max_length=64)
    kind: BatchKind
    payload_path: str = Field(max_length=4_096)
    upper_bound: datetime | None = None
    input_cursor: JsonValue = None
    next_cursor: JsonValue = None
    complete: bool
    dry_run: bool
    created_at: datetime


class Disposition(_StrictModel):
    """One terminal accounting decision against an exact persisted identity."""

    batch_id: str
    profile_label: ProfileLabel
    item_id: str = Field(min_length=1, max_length=500)
    kind: ItemDispositionKind | CandidateDispositionKind
    linked_id: str | None = Field(default=None, max_length=500)
    summary: str | None = Field(default=None, max_length=2_000)
    created_at: datetime

    @model_validator(mode="after")
    def _link_requirements(self) -> Disposition:
        if (
            self.kind in {"effect_reserved", "escalated", "progressed", "waiting", "completed"}
            and self.linked_id is None
        ):
            raise ValueError(f"{self.kind} disposition requires linked_id")
        return self


class JobStreamAdapter(Protocol):
    """Provider-neutral application-owned append-stream adapter."""

    name: str
    Config: ClassVar[type[BaseModel]]

    async def collect(
        self,
        config: BaseModel,
        *,
        cursor: JsonValue,
        upper_bound: datetime,
        limit: int,
    ) -> CollectedBatch: ...


class JobStreamRegistry:
    """Explicit adapter registry; unknown source kinds fail before model use."""

    def __init__(self, adapters: list[JobStreamAdapter] | None = None) -> None:
        self._adapters: dict[str, JobStreamAdapter] = {}
        for adapter in adapters or []:
            if adapter.name in self._adapters:
                raise ValueError(f"duplicate job stream adapter: {adapter.name}")
            self._adapters[adapter.name] = adapter

    def get(self, name: str) -> JobStreamAdapter | None:
        return self._adapters.get(name)


def candidate_identity(task_id: str, revision: int) -> str:
    return f"{task_id}@{revision}"
