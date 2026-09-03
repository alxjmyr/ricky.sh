"""Safe, line-oriented live observability for the foreground gateway."""

from __future__ import annotations

from typing import TextIO

from ricky.agent.events import (
    AgentErrorEvent,
    AgentEvent,
    ContextAssembledEvent,
    ContextCompactionFailedEvent,
    ContextCompactionFinishedEvent,
    ContextCompactionStartedEvent,
    LlmRequestStartedEvent,
    LlmResponseFinishedEvent,
    PermissionDecidedEvent,
    PermissionRequestedEvent,
    SessionStartedEvent,
    SkillActivatedEvent,
    TasksUpdatedEvent,
    ToolCallFinishedEvent,
    ToolCallNormalizedEvent,
    ToolCallRejectedEvent,
    ToolCallRequestedEvent,
    ToolCallStartedEvent,
    ToolResultOffloadFailedEvent,
    TurnFinishedEvent,
    TurnStartedEvent,
    UserInteractionRequiredEvent,
    WorkflowEvent,
)
from ricky.gateway.service import ServiceEvent


class GatewayEventRenderer:
    """Write bounded gateway and agent activity as one flushed line per event.

    This intentionally omits user text, model text/thinking, tool arguments,
    and tool-result bodies. The stream is suitable for a terminal and for the
    managed service's private append-only log file.
    """

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def render_service(self, event: ServiceEvent) -> None:
        record = f" record={event.record_id}" if event.record_id is not None else ""
        self._write(event.at.isoformat(), "gateway", event.kind, event.loop + record, event.summary)

    def render_agent(self, event: AgentEvent) -> None:
        activity = _agent_activity(event)
        if activity is None:
            return
        subject, summary = activity
        self._write(event.timestamp.isoformat(), "agent", event.kind, subject, summary)

    def _write(
        self,
        timestamp: str,
        source: str,
        kind: str,
        subject: str,
        summary: str,
    ) -> None:
        clean_summary = " ".join(summary.split())[:1_000]
        self._stream.write(f"{timestamp} {source} {kind} {subject}: {clean_summary}\n")
        self._stream.flush()


def _agent_activity(event: AgentEvent) -> tuple[str, str] | None:
    if isinstance(event, SessionStartedEvent):
        return event.session_id, f"started provider={event.provider} model={event.model}"
    if isinstance(event, TurnStartedEvent):
        return event.turn_id, "started"
    if isinstance(event, ContextAssembledEvent):
        return (
            event.turn_id,
            f"iteration={event.iteration} messages={event.message_count} tools={event.tool_count} "
            f"input_tokens={event.report.estimated_input_tokens}",
        )
    if isinstance(event, ContextCompactionStartedEvent):
        return event.operation_id, f"started covered_messages={event.covered_message_count}"
    if isinstance(event, ContextCompactionFinishedEvent):
        return event.operation_id, f"finished checkpoint={event.checkpoint_id}"
    if isinstance(event, ContextCompactionFailedEvent):
        return event.operation_id, f"failed {event.error_type}: {event.message}"
    if isinstance(event, LlmRequestStartedEvent):
        return (
            event.turn_id,
            f"request iteration={event.iteration} model={event.model} "
            f"messages={event.message_count} tools={event.tool_count}",
        )
    if isinstance(event, LlmResponseFinishedEvent):
        return (
            event.turn_id,
            f"response iteration={event.iteration} stop={event.stop_reason or '-'} "
            f"text_chars={event.text_chars} tool_calls={event.tool_call_count}",
        )
    if isinstance(event, ToolCallRequestedEvent):
        return event.turn_id, f"requested tool={event.tool_name} call={event.call_id}"
    if isinstance(event, ToolCallNormalizedEvent):
        return event.turn_id, (
            f"normalized tool={event.tool_name} call={event.call_id} paths={len(event.paths)}"
        )
    if isinstance(event, ToolCallRejectedEvent):
        return event.turn_id, (
            f"rejected tool={event.tool_name} call={event.call_id} reason={event.reason}"
        )
    if isinstance(event, PermissionRequestedEvent):
        return event.turn_id, f"permission requested tool={event.tool_name} call={event.call_id}"
    if isinstance(event, PermissionDecidedEvent):
        return event.turn_id, (
            f"permission {event.decision} tool={event.tool_name} call={event.call_id}"
        )
    if isinstance(event, ToolCallStartedEvent):
        return event.turn_id, f"started tool={event.tool_name} call={event.call_id}"
    if isinstance(event, ToolCallFinishedEvent):
        outcome = "error" if event.is_error else "ok"
        effect = (
            f" effect={event.effect_kind} disposition={event.effect_disposition or '-'}"
            if event.effect_kind == "external"
            else ""
        )
        return event.turn_id, (
            f"finished tool={event.tool_name} call={event.call_id} outcome={outcome}{effect}"
        )
    if isinstance(event, ToolResultOffloadFailedEvent):
        return event.turn_id, (
            f"tool result offload failed tool={event.tool_name} ({event.error_type})"
        )
    if isinstance(event, TasksUpdatedEvent):
        return event.turn_id, f"updated tasks={len(event.tasks)}"
    if isinstance(event, UserInteractionRequiredEvent):
        return event.turn_id, (
            f"waiting kind={event.interaction_kind} correlation={event.correlation_id}"
        )
    if isinstance(event, SkillActivatedEvent):
        return event.session_id, f"activated skill={event.skill_name}"
    if isinstance(event, WorkflowEvent):
        step = f" step={event.step_id}" if event.step_id is not None else ""
        return event.run_id, f"workflow={event.workflow_name} action={event.action}{step}"
    if isinstance(event, TurnFinishedEvent):
        outcome = "interrupted" if event.interrupted else ("failed" if event.error else "finished")
        return event.turn_id, f"{outcome} iterations={event.iterations}"
    if isinstance(event, AgentErrorEvent):
        return event.turn_id, f"error {event.error_type}: {event.message}"
    return None
