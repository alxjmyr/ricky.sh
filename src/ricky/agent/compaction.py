"""Atomic semantic context compaction at settled history boundaries."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable
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
from ricky.agent.prompts import COMPACTION_PROMPT_V2, COMPACTION_PROMPT_VERSION
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
    """One deterministic settled-prefix selection."""

    previous_boundary: int
    boundary: int
    newly_covered_messages: int
    retained_messages: int
    retained_estimated_tokens: int


def _safe_boundaries(session: AgentSession) -> list[int]:
    """Return settled prefix ends; refuse malformed or unresolved work."""
    pending: set[str] = set()
    boundaries: list[int] = []
    for index, message in enumerate(session.history):
        calls = [part for part in message.content if isinstance(part, ToolCallPart)]
        results = [part for part in message.content if isinstance(part, ToolResultPart)]
        if message.role == "tool" and not results:
            raise CompactionRefusedError("History contains malformed tool results.")
        if calls:
            if pending or message.role != "assistant":
                raise CompactionRefusedError("History contains unresolved or malformed tool work.")
            ids = [call.id for call in calls]
            if len(set(ids)) != len(ids):
                raise CompactionRefusedError("History contains unresolved or malformed tool work.")
            pending.update(ids)
        if results:
            ids = [result.call_id for result in results]
            if message.role != "tool" or len(set(ids)) != len(ids):
                raise CompactionRefusedError("History contains unresolved or malformed tool work.")
            if any(call_id not in pending for call_id in ids):
                raise CompactionRefusedError("History contains unresolved or malformed tool work.")
            pending.difference_update(ids)
        if pending:
            if not calls and not results:
                raise CompactionRefusedError("History contains unresolved tool work.")
            continue
        if message.role == "assistant" or results:
            boundary = index + 1
            # Tool-produced image observations belong to the completed batch.
            while boundary < len(session.history):
                following = session.history[boundary]
                if following.role != "user" or session.is_user_request(following):
                    break
                boundary += 1
            boundaries.append(boundary)
    if pending:
        raise CompactionRefusedError("History contains unresolved tool work.")
    return sorted(set(boundaries))


def _selection(session: AgentSession, boundary: int) -> CompactionSelection:
    previous = session.active_checkpoint()
    base = previous.retained_from_message if previous is not None else 0
    return CompactionSelection(
        previous_boundary=base,
        boundary=boundary,
        newly_covered_messages=boundary - base,
        retained_messages=len(session.history) - boundary,
        retained_estimated_tokens=_messages_estimated_tokens(session, session.history[boundary:]),
    )


def select_compaction_boundary(
    session: AgentSession,
    *,
    keep_recent_tokens: int,
) -> CompactionSelection:
    """Keep a recent tail without splitting a completed tool batch."""
    previous = session.active_checkpoint()
    base = previous.retained_from_message if previous is not None else 0
    boundaries = [
        boundary for boundary in _safe_boundaries(session) if base < boundary < len(session.history)
    ]
    for boundary in reversed(boundaries):
        if _messages_estimated_tokens(session, session.history[boundary:]) >= keep_recent_tokens:
            return _selection(session, boundary)
    raise CompactionRefusedError(
        "No useful old prefix can be compacted while preserving recent settled work."
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
        *,
        target_chars: int | None = None,
        target_tokens: int | None = None,
        max_completion_tokens: int | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Summarize bounded inputs and atomically publish one useful checkpoint."""
        operation_id = f"compaction_{uuid4().hex}"
        prior_id = session.active_checkpoint_id
        usage = Usage()
        started = False
        try:
            settings = ContextSettings.model_validate(session.settings_snapshot.get("context", {}))
            policy = settings.compaction
            if not policy.enabled:
                raise CompactionRefusedError("Context compaction is disabled.")
            for value in (target_chars, target_tokens):
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, int) or value < 1
                ):
                    raise ValueError("Compaction targets must be positive integers.")
            if max_completion_tokens is not None and (
                isinstance(max_completion_tokens, bool)
                or not isinstance(max_completion_tokens, int)
                or max_completion_tokens < 1
            ):
                raise ValueError("Compaction completion limit must be a positive integer.")
            normalized_focus = focus.strip() if focus is not None else None
            normalized_focus = normalized_focus or None
            if normalized_focus is not None and len(normalized_focus) > policy.max_focus_chars:
                raise CompactionRefusedError(
                    "Compaction focus exceeds context.compaction.max_focus_chars "
                    f"({policy.max_focus_chars})."
                )
            revision = _session_revision(session)
            before = self._context_reporter(session)
            char_limit = int(session.settings_snapshot.get("context_char_limit", 120_000))
            target_chars = min(target_chars, char_limit) if target_chars is not None else char_limit
            hard_input = before.budget.hard_input_tokens
            if hard_input is not None:
                target_tokens = (
                    min(target_tokens, hard_input) if target_tokens is not None else hard_input
                )
            previous = session.active_checkpoint()
            base = previous.retained_from_message if previous is not None else 0
            available = [b for b in _safe_boundaries(session) if base < b < len(session.history)]
            try:
                selection = select_compaction_boundary(
                    session, keep_recent_tokens=policy.keep_recent_tokens
                )
            except CompactionRefusedError:
                if not available:
                    raise
                selection = _selection(session, available[0])
            boundaries = [selection.boundary]
            previous = session.active_checkpoint()
            summary = previous.summary if previous is not None else None
            position = selection.previous_boundary
            checkpoint_id = f"checkpoint_{uuid4().hex}"
            for boundary in boundaries:
                selection = _selection(session, boundary)
                source, fragments = _history_source(session, position, boundary)
                offset = 0
                while offset < len(source):
                    request, consumed = _bounded_summary_request(
                        session,
                        source,
                        fragments,
                        offset,
                        summary,
                        normalized_focus,
                        char_limit,
                        max_completion_tokens,
                    )
                    if not started:
                        yield ContextCompactionStartedEvent(
                            operation_id=operation_id,
                            previous_checkpoint_id=prior_id,
                            source_digest=source_history_digest(session.history[:boundary]),
                            covered_message_count=boundary,
                            newly_covered_message_count=selection.newly_covered_messages,
                            retained_message_count=selection.retained_messages,
                            estimated_tokens_before=before.estimated_input_tokens,
                        )
                        started = True
                    done: MessageDone | None = None
                    stream = self._provider.stream(request)
                    try:
                        async for stream_event in stream:
                            if isinstance(stream_event, MessageDone):
                                if done is not None:
                                    raise RuntimeError(
                                        "provider emitted multiple message_done events "
                                        "during compaction"
                                    )
                                done = stream_event
                    finally:
                        close = getattr(stream, "aclose", None)
                        if close is not None:
                            await close()
                    if done is None:
                        raise RuntimeError("provider stream ended without message_done")
                    session.add_usage(done.usage)
                    usage = Usage(
                        prompt_tokens=usage.prompt_tokens + done.usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens + done.usage.completion_tokens,
                    )
                    if any(isinstance(part, ToolCallPart) for part in done.message.content):
                        raise ValueError("compaction response unexpectedly contained a tool call")
                    summary = "\n".join(
                        part.text for part in done.message.content if isinstance(part, TextPart)
                    ).strip()
                    if not summary:
                        raise ValueError("compaction response was empty")
                    if len(summary) > policy.max_summary_chars:
                        raise ValueError(
                            "compaction summary exceeds context.compaction.max_summary_chars"
                        )
                    if revision != _session_revision(session):
                        raise RuntimeError(
                            "session history or checkpoint revision changed during compaction"
                        )
                    offset += consumed
                position = boundary
                assert summary is not None
                provisional = ContextCheckpoint(
                    id=checkpoint_id,
                    summary=summary,
                    covered_message_count=boundary,
                    retained_from_message=boundary,
                    source_digest=source_history_digest(session.history[:boundary]),
                    previous_checkpoint_id=prior_id,
                    estimated_tokens_before=before.estimated_input_tokens,
                    estimated_tokens_after=0,
                    usage=usage,
                    observed=derive_observed_state(session, boundary),
                    focus=normalized_focus,
                    prompt_version=COMPACTION_PROMPT_VERSION,
                )
                candidate = _candidate_session(session, provisional)
                after = self._context_reporter(candidate)
                fits = _meets_budget(after, target_chars, target_tokens, settings)
                reduced = (
                    after.serialized_chars < before.serialized_chars
                    or after.estimated_input_tokens < before.estimated_input_tokens
                )
                if not fits or not reduced:
                    if boundary < available[-1]:
                        next_boundary = available[-1]
                        # Use the actual summary size to preserve as much of the
                        # recent tail as possible. One fallback to the maximal
                        # prefix bounds retries if the next summary grows.
                        if len(boundaries) == 1:
                            for proposed in available:
                                if proposed <= boundary:
                                    continue
                                estimate = provisional.model_copy(
                                    update={
                                        "covered_message_count": proposed,
                                        "retained_from_message": proposed,
                                        "source_digest": source_history_digest(
                                            session.history[:proposed]
                                        ),
                                        "observed": derive_observed_state(session, proposed),
                                    }
                                )
                                report = self._context_reporter(
                                    _candidate_session(session, estimate)
                                )
                                if _meets_budget(report, target_chars, target_tokens, settings):
                                    next_boundary = proposed
                                    break
                        boundaries.append(next_boundary)
                    continue
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
                if revision != _session_revision(session):
                    raise RuntimeError(
                        "session history or checkpoint revision changed during compaction"
                    )
                session.checkpoints = candidate.checkpoints
                session.active_checkpoint_id = candidate.active_checkpoint_id
                yield ContextCompactionFinishedEvent(
                    operation_id=operation_id,
                    checkpoint_id=checkpoint.id,
                    previous_checkpoint_id=prior_id,
                    source_digest=checkpoint.source_digest,
                    covered_message_count=boundary,
                    newly_covered_message_count=selection.newly_covered_messages,
                    retained_message_count=selection.retained_messages,
                    summary_chars=len(summary),
                    estimated_tokens_before=before.estimated_input_tokens,
                    estimated_tokens_after=after.estimated_input_tokens,
                    usage=usage,
                    before_report=before,
                    after_report=after,
                )
                return
            raise CompactionRefusedError(
                "Compaction cannot meet the requested budget with a useful reduction "
                "while preserving recent work."
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
    # Match occurrences in order: providers may reuse a call id in a later batch.
    pending_calls: dict[str, ToolCallPart] = {}
    pairs: list[tuple[ToolCallPart, ToolResultPart]] = []
    for message in covered:
        for part in message.content:
            if isinstance(part, ToolCallPart):
                pending_calls[part.id] = part
            elif isinstance(part, ToolResultPart):
                call = pending_calls.pop(part.call_id, None)
                if call is not None:
                    pairs.append((call, part))
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
    for call, result in pairs:
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


def _meets_budget(
    report: ContextReport,
    target_chars: int,
    target_tokens: int | None,
    settings: ContextSettings,
) -> bool:
    return (
        report.serialized_chars <= target_chars
        and (target_tokens is None or report.estimated_input_tokens <= target_tokens)
        and report.projected_image_count <= settings.media.request_image_limit
        and report.projected_image_bytes <= settings.media.request_image_byte_limit
        and report.projected_image_pixels <= settings.media.request_image_pixel_limit
    )


def _history_source(
    session: AgentSession,
    start: int,
    end: int,
) -> tuple[str, list[dict[str, Any]]]:
    """Attach attribution to every quoted message, including oversized fragments."""
    source = "["
    fragments: list[dict[str, Any]] = []
    for index in range(start, end):
        message = _compaction_message(session.history[index])
        if index > start:
            source += ","
        fragment_start = len(source)
        source += json.dumps(
            message.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        fragments.append(
            {
                "message_index": index,
                "role": message.role,
                "start": fragment_start,
                "end": len(source),
                "tool_calls": [
                    {"id": part.id, "name": part.name}
                    for part in message.content
                    if isinstance(part, ToolCallPart)
                ],
                "tool_results": [
                    {"call_id": part.call_id, "is_error": part.is_error}
                    for part in message.content
                    if isinstance(part, ToolResultPart)
                ],
            }
        )
    return source + "]", fragments


def _bounded_summary_request(
    session: AgentSession,
    source: str,
    fragments: list[dict[str, Any]],
    offset: int,
    summary: str | None,
    focus: str | None,
    char_limit: int,
    max_completion_tokens: int | None = None,
) -> tuple[CompletionRequest, int]:
    """Fit ordered quoted fragments, including a single oversized message."""

    summary_tokens = ContextSettings.model_validate(
        session.settings_snapshot.get("context", {})
    ).compaction.max_summary_tokens
    if max_completion_tokens is not None:
        summary_tokens = min(summary_tokens, max_completion_tokens)

    def build(length: int) -> CompletionRequest:
        payload = {
            "record_type": "quoted_context_compaction_input",
            "focus": focus,
            "previous_active_checkpoint": {"summary": summary} if summary is not None else None,
            "newly_covered_history_fragment": source[offset : offset + length],
            "fragment_offset": offset,
            "message_fragments": [
                {
                    **fragment,
                    "included_start": max(offset, fragment["start"]),
                    "included_end": min(offset + length, fragment["end"]),
                }
                for fragment in fragments
                if fragment["start"] < offset + length and fragment["end"] > offset
            ],
            "fragment_continues": offset + length < len(source),
        }
        return CompletionRequest(
            model=session.model,
            messages=[
                Message.text("system", COMPACTION_PROMPT_V2),
                Message.text("user", json.dumps(payload, ensure_ascii=False, sort_keys=True)),
            ],
            tools=[],
            max_tokens=summary_tokens,
        )

    def fits(request: CompletionRequest) -> bool:
        report = account_completion_request(session, request, _request_contributions(request))
        return report.serialized_chars <= char_limit and (
            report.budget.hard_input_tokens is None
            or report.estimated_input_tokens <= report.budget.hard_input_tokens
        )

    # Fail before dispatch if even the framing and carried summary cannot fit.
    smallest = build(1)
    if not fits(smallest):
        report = account_completion_request(session, smallest, _request_contributions(smallest))
        if report.serialized_chars > char_limit:
            detail = "configured fallback character ceiling"
        else:
            detail = "model's known hard input capacity"
        raise CompactionRefusedError(
            f"The minimum compaction request exceeds the {detail}. "
            "Increase the limit or start a fresh session."
        )
    low, high = 1, len(source) - offset
    while low < high:
        middle = (low + high + 1) // 2
        if fits(build(middle)):
            low = middle
        else:
            high = middle - 1
    return build(low), low


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
