"""Atomic, user-triggered semantic context compaction."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from ricky.agent.context import (
    account_completion_request,
    estimate_context_tokens,
    source_history_digest,
)
from ricky.agent.context_types import ContextReport
from ricky.agent.events import (
    AgentEvent,
    ContextCompactionFailedEvent,
    ContextCompactionFinishedEvent,
    ContextCompactionStartedEvent,
)
from ricky.agent.prompts import COMPACTION_PROMPT_V1, COMPACTION_PROMPT_VERSION
from ricky.agent.session import (
    AgentSession,
    CheckpointObservedState,
    ContextCheckpoint,
)
from ricky.config import ContextSettings
from ricky.llm import (
    CompletionRequest,
    ImagePart,
    Message,
    MessageDone,
    Provider,
    SupportsSessionRotation,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    Usage,
)


class CompactionRefusedError(ValueError):
    """The session cannot be compacted safely under current policy."""


@dataclass(frozen=True)
class CompactionSelection:
    """One deterministic complete-turn prefix selection."""

    previous_boundary: int
    boundary: int
    newly_covered_messages: int
    retained_messages: int
    retained_estimated_tokens: int


def select_compaction_boundary(
    session: AgentSession,
    *,
    keep_recent_tokens: int,
) -> CompactionSelection:
    """Choose a prefix boundary while retaining complete recent user turns."""
    history = session.history
    previous = session.active_checkpoint()
    base = previous.retained_from_message if previous is not None else 0
    if base >= len(history):
        raise CompactionRefusedError("No uncompacted history is available.")
    if history[base].role != "user":
        raise CompactionRefusedError(
            "The uncompacted history does not begin at a valid user-turn boundary."
        )

    starts = [index for index in range(base, len(history)) if history[index].role == "user"]
    if not starts or starts[0] != base:
        raise CompactionRefusedError("No valid complete-turn boundary is available.")

    retained_start = starts[-1]
    while retained_start > base:
        retained_tokens = _messages_estimated_tokens(session, history[retained_start:])
        if retained_tokens >= keep_recent_tokens:
            break
        retained_start = starts[starts.index(retained_start) - 1]

    unsafe_start = _first_uncompactable_turn_start(history, starts, retained_start)
    if unsafe_start is not None:
        retained_start = min(retained_start, unsafe_start)
    if retained_start <= base:
        raise CompactionRefusedError(
            "No useful old prefix can be compacted while preserving recent complete turns."
        )
    if _tool_group_crosses_boundary(history, retained_start):
        raise CompactionRefusedError(
            "No safe boundary exists without separating a tool call from its result."
        )
    return CompactionSelection(
        previous_boundary=base,
        boundary=retained_start,
        newly_covered_messages=retained_start - base,
        retained_messages=len(history) - retained_start,
        retained_estimated_tokens=_messages_estimated_tokens(session, history[retained_start:]),
    )


class ContextCompactor:
    """Build one isolated summary request and atomically commit its checkpoint."""

    def __init__(
        self,
        *,
        provider: Provider,
        context_reporter: Callable[[AgentSession], ContextReport],
    ) -> None:
        self._provider = provider
        self._context_reporter = context_reporter

    async def compact(
        self,
        session: AgentSession,
        focus: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Attempt one compaction, yielding only typed lifecycle events."""
        operation_id = f"compaction_{uuid4().hex}"
        prior_id = session.active_checkpoint_id
        usage = Usage()
        started = False
        try:
            context_settings = ContextSettings.model_validate(
                session.settings_snapshot.get("context", {})
            )
            policy = context_settings.compaction
            if not policy.enabled:
                raise CompactionRefusedError("Manual context compaction is disabled.")
            normalized_focus = focus.strip() if focus is not None else None
            if normalized_focus == "":
                normalized_focus = None
            if normalized_focus is not None and len(normalized_focus) > policy.max_focus_chars:
                raise CompactionRefusedError(
                    "Compaction focus exceeds "
                    f"context.compaction.max_focus_chars ({policy.max_focus_chars})."
                )

            selection = select_compaction_boundary(
                session,
                keep_recent_tokens=policy.keep_recent_tokens,
            )
            revision = _session_revision(session)
            before = self._context_reporter(session)
            request = _summary_request(session, selection, normalized_focus)
            request_report = account_completion_request(
                session,
                request,
                _request_contributions(request),
            )
            char_limit = int(session.settings_snapshot.get("context_char_limit", 120_000))
            if request_report.serialized_chars > char_limit:
                raise CompactionRefusedError(
                    "The compaction request exceeds the configured fallback character "
                    f"ceiling ({request_report.serialized_chars} > {char_limit}). "
                    "Start a fresh session; chunked compaction is not available yet."
                )
            hard_input = request_report.budget.hard_input_tokens
            if hard_input is not None and request_report.estimated_input_tokens > hard_input:
                raise CompactionRefusedError(
                    "The compaction request exceeds the model's known hard input "
                    f"capacity ({request_report.estimated_input_tokens} > {hard_input}). "
                    "Start a fresh session; chunked compaction is not available yet."
                )

            source_digest = source_history_digest(session.history[: selection.boundary])
            yield ContextCompactionStartedEvent(
                operation_id=operation_id,
                previous_checkpoint_id=prior_id,
                source_digest=source_digest,
                covered_message_count=selection.boundary,
                newly_covered_message_count=selection.newly_covered_messages,
                retained_message_count=selection.retained_messages,
                estimated_tokens_before=before.estimated_input_tokens,
            )
            started = True

            done: MessageDone | None = None
            async for stream_event in self._provider.stream(request):
                if isinstance(stream_event, MessageDone):
                    if done is not None:
                        raise RuntimeError(
                            "provider emitted multiple message_done events during compaction"
                        )
                    done = stream_event
            if done is None:
                raise RuntimeError("provider stream ended without message_done")
            usage = done.usage
            session.add_usage(usage)
            if any(isinstance(part, ToolCallPart) for part in done.message.content):
                raise ValueError("compaction response unexpectedly contained a tool call")
            summary = "\n".join(
                part.text for part in done.message.content if isinstance(part, TextPart)
            ).strip()
            if not summary:
                raise ValueError("compaction response was empty")
            if len(summary) > policy.max_summary_chars:
                raise ValueError(
                    "compaction summary exceeds "
                    f"context.compaction.max_summary_chars ({len(summary)} > "
                    f"{policy.max_summary_chars})"
                )
            if revision != _session_revision(session):
                raise RuntimeError(
                    "session history or checkpoint revision changed during compaction"
                )

            checkpoint_id = f"checkpoint_{uuid4().hex}"
            provisional = ContextCheckpoint(
                id=checkpoint_id,
                summary=summary,
                covered_message_count=selection.boundary,
                retained_from_message=selection.boundary,
                source_digest=source_digest,
                previous_checkpoint_id=prior_id,
                estimated_tokens_before=before.estimated_input_tokens,
                estimated_tokens_after=0,
                usage=usage,
                observed=derive_observed_state(session, selection.boundary),
                focus=normalized_focus,
                prompt_version=COMPACTION_PROMPT_VERSION,
            )
            candidate = _candidate_session(session, provisional)
            after = self._context_reporter(candidate)
            checkpoint = provisional.model_copy(
                update={"estimated_tokens_after": after.estimated_input_tokens}
            )
            candidate = _candidate_session(session, checkpoint)
            candidate.model_dump_json()
            if revision != _session_revision(session):
                raise RuntimeError(
                    "session history or checkpoint revision changed during compaction"
                )

            if isinstance(self._provider, SupportsSessionRotation):
                await self._provider.rotate(session.id)
            session.checkpoints = candidate.checkpoints
            session.active_checkpoint_id = candidate.active_checkpoint_id
            yield ContextCompactionFinishedEvent(
                operation_id=operation_id,
                checkpoint_id=checkpoint.id,
                previous_checkpoint_id=prior_id,
                source_digest=checkpoint.source_digest,
                covered_message_count=checkpoint.covered_message_count,
                newly_covered_message_count=selection.newly_covered_messages,
                retained_message_count=selection.retained_messages,
                summary_chars=len(checkpoint.summary),
                estimated_tokens_before=checkpoint.estimated_tokens_before,
                estimated_tokens_after=checkpoint.estimated_tokens_after,
                usage=usage,
                before_report=before,
                after_report=after,
            )
        except asyncio.CancelledError:
            yield ContextCompactionFailedEvent(
                operation_id=operation_id,
                previous_checkpoint_id=prior_id,
                error_type="CancelledError",
                message="Context compaction was cancelled; the active projection is unchanged.",
                provider_request_started=started,
                usage=usage,
            )
        except Exception as exc:  # noqa: BLE001 - failures are typed core events.
            yield ContextCompactionFailedEvent(
                operation_id=operation_id,
                previous_checkpoint_id=prior_id,
                error_type=type(exc).__name__,
                message=str(exc),
                provider_request_started=started,
                usage=usage,
            )


def derive_observed_state(
    session: AgentSession,
    boundary: int,
) -> CheckpointObservedState:
    """Derive only mechanically knowable facts from typed canonical state."""
    covered = session.history[:boundary]
    calls = [
        part for message in covered for part in message.content if isinstance(part, ToolCallPart)
    ]
    results = {
        part.call_id: part
        for message in covered
        for part in message.content
        if isinstance(part, ToolResultPart)
    }
    manifest_ids = {record.id for record in session.artifacts}
    artifact_ids = _ordered_unique(
        part.artifact.id
        for message in covered
        for part in message.content
        if (
            isinstance(part, ToolResultPart)
            and part.artifact is not None
            and part.artifact.id in manifest_ids
        )
    )
    files_read: list[str] = []
    files_modified: list[str] = []
    tool_errors: list[str] = []
    for call in calls:
        result = results.get(call.id)
        path = call.args.get("path")
        if isinstance(path, str) and path:
            if (
                call.name in {"read_file", "list_dir", "grep_search"}
                and result is not None
                and not result.is_error
            ):
                files_read.append(path)
            elif (
                call.name in {"write_file", "edit_file"}
                and result is not None
                and not result.is_error
            ):
                files_modified.append(path)
        if result is not None and result.is_error:
            tool_errors.append(f"{call.name}:{call.id}")
    return CheckpointObservedState(
        artifact_ids=artifact_ids,
        tool_names=_ordered_unique(call.name for call in calls),
        files_read=_ordered_unique(files_read),
        files_modified=_ordered_unique(files_modified),
        tool_errors=_ordered_unique(tool_errors),
        task_list=[task.model_copy(deep=True) for task in session.tasks],
        active_skill_name=(
            session.active_skill.qualified_name if session.active_skill is not None else None
        ),
    )


def _summary_request(
    session: AgentSession,
    selection: CompactionSelection,
    focus: str | None,
) -> CompletionRequest:
    previous = session.active_checkpoint()
    payload: dict[str, Any] = {
        "record_type": "quoted_context_compaction_input",
        "focus": focus,
        "previous_active_checkpoint": (
            {
                "id": previous.id,
                "summary": previous.summary,
                "observed": previous.observed.model_dump(mode="json"),
            }
            if previous is not None
            else None
        ),
        "newly_covered_history": [
            _compaction_message(message).model_dump(mode="json")
            for message in session.history[selection.previous_boundary : selection.boundary]
        ],
    }
    return CompletionRequest(
        model=session.model,
        messages=[
            Message.text("system", COMPACTION_PROMPT_V1),
            Message.text(
                "user",
                json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
            ),
        ],
        tools=[],
        max_tokens=ContextSettings.model_validate(
            session.settings_snapshot.get("context", {})
        ).compaction.max_summary_tokens,
    )


def _candidate_session(
    session: AgentSession,
    checkpoint: ContextCheckpoint,
) -> AgentSession:
    payload = session.model_dump(mode="python")
    payload["checkpoints"] = [*session.checkpoints, checkpoint]
    payload["active_checkpoint_id"] = checkpoint.id
    return AgentSession.model_validate(payload)


def _session_revision(session: AgentSession) -> tuple[str, int, str | None]:
    return (
        source_history_digest(session.history),
        len(session.checkpoints),
        session.active_checkpoint_id,
    )


def _messages_estimated_tokens(
    session: AgentSession,
    messages: list[Message],
) -> int:
    chars = len(
        json.dumps(
            [message.model_dump(mode="json") for message in messages],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    images = sum(isinstance(part, ImagePart) for message in messages for part in message.content)
    return estimate_context_tokens(session, chars) + (
        images * session.model_context.image_token_estimate
    )


def _compaction_message(message: Message) -> Message:
    """Replace image references with fixed metadata-only summary input markers."""
    content = []
    for part in message.content:
        if isinstance(part, ImagePart):
            artifact = part.artifact
            content.append(
                TextPart(
                    text=(
                        "[image omitted from compaction input: "
                        f"{artifact.media_type}, {artifact.width}x{artifact.height}, "
                        f"{artifact.byte_count} bytes]"
                    )
                )
            )
        else:
            content.append(part)
    return message.model_copy(update={"content": content}, deep=True)


def _first_uncompactable_turn_start(
    history: list[Message],
    starts: list[int],
    proposed_boundary: int,
) -> int | None:
    for position, start in enumerate(starts):
        if start >= proposed_boundary:
            break
        end = starts[position + 1] if position + 1 < len(starts) else len(history)
        turn = history[start:end]
        if not any(message.role == "assistant" for message in turn):
            return start
        call_ids = {
            part.id
            for message in turn
            for part in message.content
            if isinstance(part, ToolCallPart)
        }
        result_ids = {
            part.call_id
            for message in turn
            for part in message.content
            if isinstance(part, ToolResultPart)
        }
        if call_ids != result_ids:
            return start
    return None


def _tool_group_crosses_boundary(history: list[Message], boundary: int) -> bool:
    calls_before = {
        part.id
        for message in history[:boundary]
        for part in message.content
        if isinstance(part, ToolCallPart)
    }
    calls_after = {
        part.id
        for message in history[boundary:]
        for part in message.content
        if isinstance(part, ToolCallPart)
    }
    results_before = {
        part.call_id
        for message in history[:boundary]
        for part in message.content
        if isinstance(part, ToolResultPart)
    }
    results_after = {
        part.call_id
        for message in history[boundary:]
        for part in message.content
        if isinstance(part, ToolResultPart)
    }
    return bool((calls_before & results_after) or (calls_after & results_before))


def _request_contributions(
    request: CompletionRequest,
) -> list[tuple[str, int, int]]:
    names = ("compaction_prompt", "compaction_source")
    return [
        (
            names[index],
            sum(_serialized_chars(part) for part in message.content),
            len(message.content),
        )
        for index, message in enumerate(request.messages)
    ]


def _serialized_chars(value: object) -> int:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[union-attr]
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _ordered_unique(values: Any) -> list[str]:
    return list(dict.fromkeys(values))


__all__ = [
    "CompactionRefusedError",
    "CompactionSelection",
    "ContextCompactor",
    "derive_observed_state",
    "select_compaction_boundary",
]
