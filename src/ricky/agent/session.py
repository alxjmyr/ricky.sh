"""Serializable agent session state."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ricky.agent.context_types import SessionModelContext
from ricky.config import (
    RickySettings,
    profile_data_path,
    user_data_path,
    validate_timezone_name,
)
from ricky.durable_tasks.types import TaskLease
from ricky.llm import MediaArtifactRef, Message, ModelInfo, ToolArtifactRef, Usage
from ricky.profiles import ProfileName, ProfileScope
from ricky.skills.spec import ActiveSkill
from ricky.workflows.run import WorkflowInvocation

TaskStatus = Literal["pending", "in_progress", "done"]


def _now() -> datetime:
    return datetime.now(UTC)


class TaskItem(BaseModel):
    """A user-visible task tracked by the built-in update_tasks tool."""

    id: str = Field(default_factory=lambda: f"task_{uuid4().hex[:8]}")
    title: str
    status: TaskStatus = "pending"


class PermissionGrant(BaseModel):
    """A session-scoped permission grant matched by tool and a params subset."""

    tool_name: str
    params_equal: dict[str, Any] = Field(default_factory=dict)
    directory_param: str | None = None
    directory_path: str | None = None
    label: str | None = None
    """Human description for /permissions display; not used in matching."""

    def matches(self, tool_name: str, params: dict[str, Any]) -> bool:
        """Return true when this grant covers a tool invocation."""
        if self.tool_name != tool_name:
            return False
        if not all(params.get(key) == value for key, value in self.params_equal.items()):
            return False
        if self.directory_param is None or self.directory_path is None:
            return True
        candidate = params.get(self.directory_param)
        if not isinstance(candidate, str):
            return False
        raw_path = Path(candidate)
        if not raw_path.is_absolute() or ".." in raw_path.parts:
            return False
        path = raw_path.resolve()
        return path == raw_path and path.is_relative_to(Path(self.directory_path))

    @model_validator(mode="after")
    def _directory_grant_is_complete_and_canonical(self) -> PermissionGrant:
        if (self.directory_param is None) != (self.directory_path is None):
            raise ValueError("directory permission grant requires param and path")
        if self.directory_path is not None:
            path = Path(self.directory_path)
            if not path.is_absolute() or ".." in path.parts or path != path.resolve():
                raise ValueError("directory permission grant path must be canonical and absolute")
        return self


class SessionArtifactRecord(ToolArtifactRef):
    """Private manifest record used to reopen and validate one stored body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^artifact_[0-9a-f]{32}\.txt$",
    )
    created_at: datetime = Field(default_factory=_now)

    def reference(self) -> ToolArtifactRef:
        """Return the provider-safe portion without its storage locator."""
        return ToolArtifactRef.model_validate(
            self.model_dump(exclude={"relative_path", "created_at"})
        )


class MediaAdmissionEvidence(BaseModel):
    """Immutable source-policy evidence captured when media enters a session."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    disclosure_class: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    admitted_provider: str = Field(min_length=1, max_length=100)
    source_owner: ProfileName
    admitted_at: datetime = Field(default_factory=_now)


class SessionMediaRecord(MediaArtifactRef):
    """Private manifest record for one confined immutable media body."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    relative_path: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^media_[0-9a-f]{32}\.png$",
    )
    provenance: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    retention: Literal["runtime", "session", "conversation"]
    admission: MediaAdmissionEvidence
    created_at: datetime = Field(default_factory=_now)

    def reference(self) -> MediaArtifactRef:
        """Return canonical identity without storage or policy details."""
        return MediaArtifactRef.model_validate(
            self.model_dump(
                exclude={
                    "relative_path",
                    "provenance",
                    "retention",
                    "admission",
                    "created_at",
                }
            )
        )


class CheckpointObservedState(BaseModel):
    """Mechanically observed facts retained separately from summary prose."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_ids: list[str] = Field(default_factory=list, max_length=10_000)
    tool_names: list[str] = Field(default_factory=list, max_length=10_000)
    files_read: list[str] = Field(default_factory=list, max_length=10_000)
    files_modified: list[str] = Field(default_factory=list, max_length=10_000)
    tool_errors: list[str] = Field(default_factory=list, max_length=10_000)
    task_list: list[TaskItem] = Field(default_factory=list, max_length=10_000)
    active_skill_name: str | None = Field(default=None, max_length=500)


class ContextCheckpoint(BaseModel):
    """One immutable semantic replacement for a contiguous history prefix."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^checkpoint_[0-9a-f]{32}$")
    created_at: datetime = Field(default_factory=_now)
    summary: str = Field(min_length=1, max_length=1_000_000)
    covered_message_count: int = Field(ge=1)
    retained_from_message: int = Field(ge=1)
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_checkpoint_id: str | None = Field(
        default=None,
        pattern=r"^checkpoint_[0-9a-f]{32}$",
    )
    estimated_tokens_before: int = Field(ge=0)
    estimated_tokens_after: int = Field(ge=0)
    usage: Usage = Field(default_factory=Usage)
    observed: CheckpointObservedState = Field(default_factory=CheckpointObservedState)
    focus: str | None = Field(default=None, max_length=10_000)
    prompt_version: Literal["context_compaction_v1"] = "context_compaction_v1"

    @model_validator(mode="after")
    def _matching_boundaries(self) -> ContextCheckpoint:
        if self.covered_message_count != self.retained_from_message:
            raise ValueError(
                "checkpoint covered_message_count and retained_from_message must match"
            )
        return self


class AgentSession(BaseModel):
    """Ephemeral v0.1 session state, designed for future persistence."""

    id: str = Field(default_factory=lambda: f"session_{uuid4().hex}")
    created_at: datetime = Field(default_factory=_now)
    timezone: str = "UTC"
    provider: str
    model: str
    profile_scope: ProfileScope
    history: list[Message] = Field(default_factory=list)
    tasks: list[TaskItem] = Field(default_factory=list)
    active_task_leases: dict[str, TaskLease] = Field(default_factory=dict)
    permission_grants: list[PermissionGrant] = Field(default_factory=list)
    active_skill: ActiveSkill | None = None
    active_workflow: WorkflowInvocation | None = None
    artifacts: list[SessionArtifactRecord] = Field(default_factory=list, max_length=10_000)
    media: list[SessionMediaRecord] = Field(default_factory=list, max_length=1_000)
    checkpoints: list[ContextCheckpoint] = Field(default_factory=list, max_length=10_000)
    active_checkpoint_id: str | None = Field(
        default=None,
        pattern=r"^checkpoint_[0-9a-f]{32}$",
    )
    settings_snapshot: dict[str, Any] = Field(default_factory=dict)
    model_context: SessionModelContext = Field(default_factory=SessionModelContext)
    cumulative_usage: Usage = Field(default_factory=Usage)
    started: bool = False

    _valid_timezone = field_validator("timezone")(validate_timezone_name)

    @model_validator(mode="after")
    def _validate_checkpoint_state(self) -> AgentSession:
        media_ids = [record.id for record in self.media]
        if len(media_ids) != len(set(media_ids)):
            raise ValueError("session media ids must be unique")
        ids = [checkpoint.id for checkpoint in self.checkpoints]
        if len(ids) != len(set(ids)):
            raise ValueError("session checkpoint ids must be unique")
        if self.active_checkpoint_id is not None and self.active_checkpoint_id not in ids:
            raise ValueError("active_checkpoint_id must identify a serialized checkpoint")
        by_id = {checkpoint.id: checkpoint for checkpoint in self.checkpoints}
        for checkpoint in self.checkpoints:
            if checkpoint.retained_from_message > len(self.history):
                raise ValueError("checkpoint boundary exceeds serialized history")
            previous_id = checkpoint.previous_checkpoint_id
            if previous_id is not None and previous_id not in by_id:
                raise ValueError("checkpoint previous_checkpoint_id is not serialized")
        return self

    @classmethod
    def create(
        cls,
        settings: RickySettings,
        *,
        profile_scope: ProfileScope,
        provider: str | None = None,
        model: str | None = None,
        model_info: ModelInfo | None = None,
        timezone: str | None = None,
    ) -> AgentSession:
        """Create a session with a non-secret snapshot of runtime settings."""
        selection = settings.resolve_profile_selection(profile_scope, provider, model)
        runtime_settings = settings.resolve_profile_runtime_settings(profile_scope)
        resolved_user_data_dir = user_data_path(settings)
        profile_definitions = {
            profile: settings.profiles.definitions[profile].model_dump(mode="json")
            for profile in profile_scope.profiles
        }
        profile_data_roots = {
            profile: str(profile_data_path(settings, profile)) for profile in profile_scope.profiles
        }
        configured = next(
            (
                profile
                for profile in runtime_settings.context.models
                if profile.provider == selection.provider and profile.model == selection.model
            ),
            None,
        )
        if configured is not None:
            model_context = SessionModelContext(
                context_window_tokens=configured.context_window_tokens,
                max_output_tokens=configured.max_output_tokens,
                image_token_estimate=(
                    configured.image_token_estimate
                    or runtime_settings.context.media.default_image_token_estimate
                ),
                source="configured",
            )
        elif model_info is not None and model_info.id == selection.model:
            model_context = SessionModelContext(
                context_window_tokens=model_info.context_length,
                max_output_tokens=model_info.max_output_tokens,
                image_token_estimate=runtime_settings.context.media.default_image_token_estimate,
                source="catalog" if model_info.context_length is not None else "unknown",
            )
        else:
            model_context = SessionModelContext(
                image_token_estimate=runtime_settings.context.media.default_image_token_estimate
            )
        return cls(
            provider=selection.provider,
            model=selection.model,
            timezone=runtime_settings.user_timezone if timezone is None else timezone,
            profile_scope=profile_scope,
            model_context=model_context,
            settings_snapshot={
                "default_provider": runtime_settings.default_provider,
                "resolved_selection": selection.model_dump(),
                "request_timeout_seconds": runtime_settings.request_timeout_seconds,
                "max_turn_iterations": runtime_settings.max_turn_iterations,
                "context_char_limit": runtime_settings.context_char_limit,
                "context": runtime_settings.context.model_dump(mode="json"),
                "shell_timeout_seconds": runtime_settings.shell_timeout_seconds,
                "user_data_dir": str(resolved_user_data_dir),
                "profile_definitions": profile_definitions,
                "profile_data_roots": profile_data_roots,
                "google_accounts": {
                    name: account.model_dump(mode="json")
                    for name, account in runtime_settings.google.accounts.items()
                },
            },
        )

    def add_usage(self, usage: Usage) -> None:
        """Accumulate provider usage reported during a turn."""
        self.cumulative_usage = Usage(
            prompt_tokens=self.cumulative_usage.prompt_tokens + usage.prompt_tokens,
            completion_tokens=self.cumulative_usage.completion_tokens + usage.completion_tokens,
        )

    def task_snapshots(self) -> list[dict[str, str]]:
        """Return task state in event-friendly primitive form."""
        return [{"id": task.id, "title": task.title, "status": task.status} for task in self.tasks]

    def active_checkpoint(self) -> ContextCheckpoint | None:
        """Return the selected checkpoint without treating list order as authority."""
        if self.active_checkpoint_id is None:
            return None
        for checkpoint in self.checkpoints:
            if checkpoint.id == self.active_checkpoint_id:
                return checkpoint
        raise ValueError("active_checkpoint_id does not identify a serialized checkpoint")
