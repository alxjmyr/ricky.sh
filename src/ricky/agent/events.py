"""Typed event stream emitted by the agent core."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

from ricky.agent.context_types import ContextBudget, ContextReport, ContextSection
from ricky.llm import Usage
from ricky.permissions.types import GrantOption
from ricky.tool_contracts import ToolRuntimeFailure


def _now() -> datetime:
    return datetime.now(UTC)


class EventBase(BaseModel):
    """Base fields shared by all agent events."""

    timestamp: datetime = Field(default_factory=_now)


class SessionStartedEvent(EventBase):
    kind: Literal["session_started"] = "session_started"
    session_id: str
    provider: str
    model: str


class TurnStartedEvent(EventBase):
    kind: Literal["turn_started"] = "turn_started"
    turn_id: str
    user_input: str


class ContextAssembledEvent(EventBase):
    """One actual request's authoritative context accounting report."""

    kind: Literal["context_assembled"] = "context_assembled"
    turn_id: str
    iteration: int
    model: str
    report: ContextReport

    def __init__(
        self,
        *,
        turn_id: str,
        iteration: int,
        model: str,
        report: ContextReport | None = None,
        sections: list[ContextSection] | None = None,
        message_count: int = 0,
        tool_count: int = 0,
        char_count: int = 0,
        timestamp: datetime | None = None,
        kind: Literal["context_assembled"] = "context_assembled",
    ) -> None:
        """Construct the report shape while accepting the previous public arguments."""
        if report is None:
            report = _legacy_context_report(
                sections=sections or [],
                serialized_chars=char_count,
                message_count=message_count,
                tool_count=tool_count,
            )
        values: dict[str, Any] = {
            "kind": kind,
            "turn_id": turn_id,
            "iteration": iteration,
            "model": model,
            "report": report,
        }
        if timestamp is not None:
            values["timestamp"] = timestamp
        super().__init__(**values)

    @model_validator(mode="before")
    @classmethod
    def _upgrade_legacy_shape(cls, value: Any) -> Any:
        """Accept old serialized fields while serializing only the report."""
        if not isinstance(value, dict) or "report" in value:
            return value
        value["report"] = _legacy_context_report(
            sections=[
                ContextSection.model_validate(section) for section in value.pop("sections", [])
            ],
            serialized_chars=int(value.pop("char_count", 0)),
            message_count=int(value.pop("message_count", 0)),
            tool_count=int(value.pop("tool_count", 0)),
        )
        return value

    @property
    def sections(self) -> list[ContextSection]:
        """Backward-compatible coarse view; new consumers use the report."""
        by_name = {section.name: section for section in self.report.sections}
        system = by_name.get("base_system_prompt", ContextSection(name="system", chars=0))
        history_names = (
            "conversation_text",
            "assistant_thinking",
            "tool_calls",
            "tool_results",
        )
        history_parts = [by_name[name] for name in history_names if name in by_name]
        sections = [
            system.model_copy(update={"name": "system"}),
            ContextSection(
                name="history",
                chars=sum(section.chars for section in history_parts),
                estimated_tokens=sum(section.estimated_tokens for section in history_parts),
                item_count=sum(section.item_count for section in history_parts),
            ),
        ]
        for old_name, new_name in (
            ("memory_index", "memory"),
            ("active_skill", "active_skill"),
        ):
            section = by_name.get(old_name)
            if section is not None and section.item_count:
                sections.append(section.model_copy(update={"name": new_name}))
        fixed = {
            "base_system_prompt",
            "checkpoint_summary",
            *history_names,
            "memory_index",
            "active_skill",
            "pending_user_input",
            "conversation_images",
            "pending_user_images",
            "advertised_tool_definitions",
            "canonical_request_envelope_overhead",
        }
        sections.extend(section for section in self.report.sections if section.name not in fixed)
        for image_name in ("conversation_images", "pending_user_images"):
            image_section = by_name.get(image_name)
            if image_section is not None and image_section.item_count:
                sections.append(image_section)
        pending = by_name.get("pending_user_input")
        if pending is not None and self.report.pending_user_input_included:
            sections.append(pending.model_copy(update={"name": "user_input"}))
        return sections

    @property
    def message_count(self) -> int:
        return self.report.message_count

    @property
    def tool_count(self) -> int:
        return self.report.tool_count

    @property
    def char_count(self) -> int:
        """Backward-compatible alias for the canonical serialized character count."""
        return self.report.serialized_chars


class ContextCompactionStartedEvent(EventBase):
    """A validated compaction request is about to be dispatched."""

    kind: Literal["context_compaction_started"] = "context_compaction_started"
    operation_id: str
    previous_checkpoint_id: str | None = None
    source_digest: str
    covered_message_count: int = Field(ge=1)
    newly_covered_message_count: int = Field(ge=1)
    retained_message_count: int = Field(ge=0)
    estimated_tokens_before: int = Field(ge=0)


class ContextCompactionFinishedEvent(EventBase):
    """One checkpoint was committed and selected atomically."""

    kind: Literal["context_compaction_finished"] = "context_compaction_finished"
    operation_id: str
    checkpoint_id: str
    previous_checkpoint_id: str | None = None
    source_digest: str
    covered_message_count: int = Field(ge=1)
    newly_covered_message_count: int = Field(ge=1)
    retained_message_count: int = Field(ge=0)
    summary_chars: int = Field(ge=1)
    estimated_tokens_before: int = Field(ge=0)
    estimated_tokens_after: int = Field(ge=0)
    usage: Usage = Field(default_factory=Usage)
    before_report: ContextReport | None = None
    after_report: ContextReport | None = None


class ContextCompactionFailedEvent(EventBase):
    """A compaction attempt failed without changing the active projection."""

    kind: Literal["context_compaction_failed"] = "context_compaction_failed"
    operation_id: str
    previous_checkpoint_id: str | None = None
    error_type: str
    message: str
    provider_request_started: bool = False
    usage: Usage = Field(default_factory=Usage)


def _legacy_context_report(
    *,
    sections: list[ContextSection],
    serialized_chars: int,
    message_count: int,
    tool_count: int,
) -> ContextReport:
    return ContextReport(
        sections=sections,
        serialized_chars=serialized_chars,
        estimated_input_tokens=0,
        message_count=message_count,
        tool_count=tool_count,
        budget=ContextBudget(
            context_window_tokens=None,
            output_reserve_tokens=0,
            safety_margin_tokens=0,
            hard_input_tokens=None,
            remaining_tokens=None,
            capacity_source="unknown",
        ),
        pending_user_input_included=True,
    )


class LlmRequestStartedEvent(EventBase):
    kind: Literal["llm_request_started"] = "llm_request_started"
    turn_id: str
    iteration: int
    model: str
    message_count: int
    tool_count: int


class LlmResponseFinishedEvent(EventBase):
    kind: Literal["llm_response_finished"] = "llm_response_finished"
    turn_id: str
    iteration: int
    stop_reason: str | None = None
    text_chars: int
    thinking_chars: int
    tool_call_count: int
    empty: bool


class TextDeltaEvent(EventBase):
    kind: Literal["text_delta"] = "text_delta"
    turn_id: str
    delta: str


class ThinkingDeltaEvent(EventBase):
    kind: Literal["thinking_delta"] = "thinking_delta"
    turn_id: str
    delta: str


class ToolCallRequestedEvent(EventBase):
    kind: Literal["tool_call_requested"] = "tool_call_requested"
    turn_id: str
    call_id: str
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)


class ToolCallNormalizedEvent(EventBase):
    """Safe evidence that syntactic provider JSON was schema-normalized."""

    kind: Literal["tool_call_normalized"] = "tool_call_normalized"
    turn_id: str
    call_id: str
    tool_name: str
    paths: list[str] = Field(default_factory=list)


class ToolCallRejectedEvent(EventBase):
    """Terminal pre-dispatch outcome for an unknown or invalid tool call."""

    kind: Literal["tool_call_rejected"] = "tool_call_rejected"
    turn_id: str
    call_id: str
    tool_name: str
    reason: Literal["unknown_tool", "malformed_json", "invalid_arguments"]
    repairable: bool
    external_effect: bool = False
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class PermissionRequestedEvent(EventBase):
    kind: Literal["permission_requested"] = "permission_requested"
    turn_id: str
    call_id: str
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    reason: str
    summary: str | None = None
    offered_grants: list[GrantOption] = Field(default_factory=list)


class PermissionDecidedEvent(EventBase):
    kind: Literal["permission_decided"] = "permission_decided"
    turn_id: str
    call_id: str
    tool_name: str
    decision: Literal["allow", "deny"]
    reason: str
    remembered: bool = False
    grant_label: str | None = None


class ToolCallStartedEvent(EventBase):
    kind: Literal["tool_call_started"] = "tool_call_started"
    turn_id: str
    call_id: str
    tool_name: str


class ToolCallFinishedEvent(EventBase):
    kind: Literal["tool_call_finished"] = "tool_call_finished"
    turn_id: str
    call_id: str
    tool_name: str
    is_error: bool
    runtime_failure: ToolRuntimeFailure | None = None
    content_chars: int
    content: str | None = None
    data_chars: int = 0
    result_model: str | None = None
    artifact_id: str | None = None
    full_content_chars: int | None = Field(default=None, ge=0)
    visible_content_chars: int | None = Field(default=None, ge=0)
    offloaded: bool = False
    effect_kind: Literal["none", "ricky_state", "external"] | None = None
    effect_disposition: Literal["performed", "not_performed", "in_doubt"] | None = None
    effect_attempt_reason: Literal["invalid_preflight", "denied"] | None = None
    effect_action_id: str | None = None
    input_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ToolResultOffloadFailedEvent(EventBase):
    """A successful tool result was bounded after its artifact write failed."""

    kind: Literal["tool_result_offload_failed"] = "tool_result_offload_failed"
    turn_id: str
    call_id: str
    tool_name: str
    full_content_chars: int = Field(ge=0)
    visible_content_chars: int = Field(ge=0)
    error_type: str


class TasksUpdatedEvent(EventBase):
    kind: Literal["tasks_updated"] = "tasks_updated"
    turn_id: str
    tasks: list[dict[str, str]]


class UserInteractionRequiredEvent(EventBase):
    """A trusted tool ended the turn with one exact user-facing prompt."""

    kind: Literal["user_interaction_required"] = "user_interaction_required"
    turn_id: str
    interaction_kind: Literal["guardrail_input", "confirmation"]
    correlation_id: str
    prompt: str = Field(min_length=1, max_length=8_000)


class SkillActivatedEvent(EventBase):
    kind: Literal["skill_activated"] = "skill_activated"
    session_id: str
    skill_name: str
    args: str = ""
    source_path: str
    turn_id: str | None = None
    replaced_skill: str | None = None


class WorkflowEvent(EventBase):
    """One auditable scheduler, checkpoint, item, or effect event."""

    kind: Literal["workflow"] = "workflow"
    action: Literal[
        "run_created",
        "run_resumed",
        "graph_compiled",
        "scheduler_pass",
        "step_ready",
        "step_started",
        "step_attempt_failed",
        "step_retry_scheduled",
        "step_completed",
        "step_skipped",
        "step_blocked",
        "step_interrupted",
        "step_in_doubt",
        "item_started",
        "item_completed",
        "checkpoint_written",
        "checkpoint_failed",
        "effect_prepared",
        "effect_dispatched",
        "effect_reconciled",
        "context_debug",
        "message_emitted",
        "run_completed",
    ]
    run_id: str
    workflow_name: str
    step_id: str | None = None
    execution_address: str | None = None
    item_key: str | int | float | bool | None = None
    attempt: int | None = None
    reason: str | None = None
    references: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class TurnFinishedEvent(EventBase):
    kind: Literal["turn_finished"] = "turn_finished"
    turn_id: str
    iterations: int
    interrupted: bool = False
    error: str | None = None
    usage: Usage = Field(default_factory=Usage)


class AgentErrorEvent(EventBase):
    kind: Literal["agent_error"] = "agent_error"
    turn_id: str
    message: str
    error_type: str


AgentEvent = Annotated[
    SessionStartedEvent
    | TurnStartedEvent
    | ContextAssembledEvent
    | ContextCompactionStartedEvent
    | ContextCompactionFinishedEvent
    | ContextCompactionFailedEvent
    | LlmRequestStartedEvent
    | LlmResponseFinishedEvent
    | TextDeltaEvent
    | ThinkingDeltaEvent
    | ToolCallRequestedEvent
    | ToolCallNormalizedEvent
    | ToolCallRejectedEvent
    | PermissionRequestedEvent
    | PermissionDecidedEvent
    | ToolCallStartedEvent
    | ToolCallFinishedEvent
    | ToolResultOffloadFailedEvent
    | TasksUpdatedEvent
    | UserInteractionRequiredEvent
    | SkillActivatedEvent
    | WorkflowEvent
    | TurnFinishedEvent
    | AgentErrorEvent,
    Field(discriminator="kind"),
]
