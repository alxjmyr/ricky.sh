"""Manual context compaction regressions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter
from rich.console import Console

from ricky.agent import (
    AgentEvent,
    AgentLoop,
    AgentSession,
    CheckpointObservedState,
    CompactionRefusedError,
    ContextCheckpoint,
    ContextCompactionFailedEvent,
    ContextCompactionFinishedEvent,
    ContextCompactionStartedEvent,
    SessionArtifactRecord,
    TaskItem,
    assemble_context,
    derive_observed_state,
    select_compaction_boundary,
)
from ricky.config import RickySettings
from ricky.interfaces.cli.chat import ChatController
from ricky.interfaces.cli.render import CliRenderer
from ricky.llm import (
    CompletionRequest,
    ImagePart,
    MediaArtifactRef,
    Message,
    MessageDone,
    StreamEvent,
    TextPart,
    ToolArtifactRef,
    ToolCallPart,
    ToolResultPart,
    Usage,
)
from ricky.profiles import ProfileLabel
from ricky.skills.registry import SkillRegistry
from ricky.skills.spec import ActiveSkill
from ricky.tools import ToolRegistry

SUMMARY = """## Current goal and user intent
Continue the tested implementation.

## Constraints and preferences
Preserve exact state.

## Completed work
Old work completed.

## In-progress or blocked work
None.

## Decisions and rationale
Use one checkpoint.

## Corrections and rejected approaches
None.

## Unresolved questions and next actions
Continue.

## Material references
artifact ids remain available."""


class ScriptedProvider:
    name = "scripted"

    def __init__(self, outcomes: list[MessageDone | BaseException]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[CompletionRequest] = []

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        yield outcome

    async def aclose(self) -> None:
        pass


class BlockingProvider:
    name = "blocking"

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []
        self.started = asyncio.Event()

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        self.started.set()
        await asyncio.Event().wait()
        if False:  # pragma: no cover - makes this an async generator.
            yield MessageDone(message=Message.text("assistant", SUMMARY))

    async def aclose(self) -> None:
        pass


class RevisingProvider(ScriptedProvider):
    def __init__(self, session: AgentSession) -> None:
        super().__init__([_summary_done()])
        self._session = session

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        self._session.history.extend(_turn("concurrent", "revision"))
        yield self.outcomes.pop(0)  # type: ignore[misc]


class RotatingProvider(ScriptedProvider):
    def __init__(self, outcomes: list[MessageDone | BaseException]) -> None:
        super().__init__(outcomes)
        self.rotations: list[str] = []
        self.rotation_error: BaseException | None = None

    async def rotate(self, session_id: str) -> None:
        if self.rotation_error is not None:
            raise self.rotation_error
        self.rotations.append(session_id)


def _settings(
    *,
    keep_recent_tokens: int = 1,
    max_focus_chars: int = 100,
    context_char_limit: int = 1_000_000,
) -> RickySettings:
    return RickySettings.model_validate(
        {
            "context_char_limit": context_char_limit,
            "context": {
                "chars_per_token": 4,
                "compaction": {
                    "enabled": True,
                    "keep_recent_tokens": keep_recent_tokens,
                    "max_summary_tokens": 256,
                    "max_focus_chars": max_focus_chars,
                    "max_summary_chars": 10_000,
                },
            },
        }
    )


def _turn(user: str, assistant: str) -> list[Message]:
    return [Message.text("user", user), Message.text("assistant", assistant)]


def _long_history(settings: RickySettings) -> AgentSession:
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    session.history = [
        *_turn("OLD-PREFIX-ONE", "A" * 2_000),
        *_turn("OLD-PREFIX-TWO", "B" * 2_000),
        *_turn("RECENT-TAIL", "recent answer"),
    ]
    return session


def _summary_done(text: str = SUMMARY) -> MessageDone:
    return MessageDone(
        message=Message.text("assistant", text),
        usage=Usage(prompt_tokens=31, completion_tokens=17),
        stop_reason="stop",
    )


def _image() -> ImagePart:
    return ImagePart(
        artifact=MediaArtifactRef(
            id="media_" + "1" * 32,
            byte_count=321,
            sha256="2" * 64,
            width=9,
            height=7,
            source_label=ProfileLabel.owned_by("personal"),
        )
    )


def _loop(
    provider: Any,
    settings: RickySettings,
    tmp_path: Path,
) -> AgentLoop:
    return AgentLoop(
        provider=provider,
        registry=ToolRegistry([]),
        settings=settings,
        cwd=tmp_path,
    )


async def _compact(
    loop: AgentLoop,
    session: AgentSession,
    focus: str | None = None,
) -> list[AgentEvent]:
    return [event async for event in loop.compact_context(session, focus)]


def _request_text(request: CompletionRequest) -> str:
    return "\n".join(
        part.text
        for message in request.messages
        for part in message.content
        if isinstance(part, TextPart)
    )


def test_boundary_keeps_oversized_tool_turn_and_parallel_results_together() -> None:
    settings = _settings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    session.history = [
        *_turn("old", "old answer"),
        Message.text("user", "latest"),
        Message(
            role="assistant",
            content=[
                ToolCallPart(id="c1", name="read_file", args={"path": "a.txt"}),
                ToolCallPart(id="c2", name="read_file", args={"path": "b.txt"}),
            ],
        ),
        Message(
            role="tool",
            content=[ToolResultPart(call_id="c1", content="A" * 20_000)],
        ),
        Message(
            role="tool",
            content=[ToolResultPart(call_id="c2", content="B" * 20_000)],
        ),
        Message.text("assistant", "tool turn finished"),
    ]

    selected = select_compaction_boundary(session, keep_recent_tokens=1)

    assert selected.boundary == 2
    assert session.history[selected.boundary].role == "user"
    assert {part.id for part in session.history[3].content} == {"c1", "c2"}  # type: ignore[attr-defined]
    assert selected.retained_messages == 5


def test_boundary_keeps_an_interrupted_tool_turn_out_of_the_prefix() -> None:
    settings = _settings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    session.history = [
        *_turn("safe", "done"),
        Message.text("user", "interrupted"),
        Message(
            role="assistant",
            content=[ToolCallPart(id="dangling", name="read_file", args={"path": "x"})],
        ),
        *_turn("later", "done later"),
    ]

    selected = select_compaction_boundary(session, keep_recent_tokens=1)

    assert selected.boundary == 2
    assert session.history[selected.boundary] == Message.text("user", "interrupted")


async def test_no_useful_prefix_refuses_without_calling_provider(tmp_path: Path) -> None:
    settings = _settings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    session.history = _turn("only turn", "only answer")
    provider = ScriptedProvider([_summary_done()])

    events = await _compact(_loop(provider, settings, tmp_path), session)

    assert provider.requests == []
    assert len(events) == 1
    assert isinstance(events[0], ContextCompactionFailedEvent)
    assert events[0].error_type == "CompactionRefusedError"
    assert session.checkpoints == []
    assert session.active_checkpoint_id is None


async def test_success_is_one_tool_free_request_and_atomic_checkpoint(
    tmp_path: Path,
) -> None:
    settings = _settings()
    session = _long_history(settings)
    before_history = session.history.copy()
    provider = ScriptedProvider([_summary_done()])

    events = await _compact(
        _loop(provider, settings, tmp_path),
        session,
        "Focus on API decisions",
    )

    assert [event.kind for event in events] == [
        "context_compaction_started",
        "context_compaction_finished",
    ]
    finished = events[-1]
    assert isinstance(finished, ContextCompactionFinishedEvent)
    assert finished.before_report is not None
    assert finished.after_report is not None
    assert all(section.estimated_tokens >= 0 for section in finished.after_report.sections)
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.model == session.model
    assert request.tools == []
    assert request.max_tokens == 256
    request_text = _request_text(request)
    assert "OLD-PREFIX-ONE" in request_text
    assert "OLD-PREFIX-TWO" in request_text
    assert "RECENT-TAIL" not in request_text
    assert "Focus on API decisions" in request_text
    assert session.history == before_history
    assert len(session.checkpoints) == 1
    checkpoint = session.active_checkpoint()
    assert checkpoint is not None
    assert checkpoint.focus == "Focus on API decisions"
    assert checkpoint.usage == Usage(prompt_tokens=31, completion_tokens=17)
    assert checkpoint.source_digest
    assert checkpoint.estimated_tokens_before > checkpoint.estimated_tokens_after
    assert AgentSession.model_validate_json(session.model_dump_json()) == session


async def test_compaction_replaces_images_with_metadata_only_markers(tmp_path: Path) -> None:
    settings = _settings()
    session = _long_history(settings)
    session.history[0] = Message(
        role="user",
        content=[TextPart(text="OLD-PREFIX-ONE"), _image()],
    )
    provider = ScriptedProvider([_summary_done()])

    await _compact(_loop(provider, settings, tmp_path), session)

    request = provider.requests[0]
    assert not any(
        isinstance(part, ImagePart) for message in request.messages for part in message.content
    )
    text = _request_text(request)
    assert "[image omitted from compaction input: image/png, 9x7, 321 bytes]" in text
    assert _image().artifact.id not in text
    assert _image().artifact.sha256 not in text


async def test_success_rotates_provider_session_after_isolated_summary(
    tmp_path: Path,
) -> None:
    settings = _settings()
    session = _long_history(settings)
    provider = RotatingProvider([_summary_done()])

    events = await _compact(_loop(provider, settings, tmp_path), session)

    assert isinstance(events[-1], ContextCompactionFinishedEvent)
    assert provider.requests[0].provider_options == {}
    assert provider.rotations == [session.id]


async def test_rotation_failure_does_not_commit_compaction(
    tmp_path: Path,
) -> None:
    settings = _settings()
    session = _long_history(settings)
    before = session.model_dump_json()
    provider = RotatingProvider([_summary_done()])
    provider.rotation_error = RuntimeError("rotation failed")

    events = await _compact(_loop(provider, settings, tmp_path), session)

    assert isinstance(events[-1], ContextCompactionFailedEvent)
    assert events[-1].error_type == "RuntimeError"
    previous = AgentSession.model_validate_json(before)
    assert session.history == previous.history
    assert session.checkpoints == []
    assert session.active_checkpoint_id is None


async def test_next_request_uses_summary_and_tail_not_raw_prefix(
    tmp_path: Path,
) -> None:
    settings = _settings()
    session = _long_history(settings)
    provider = ScriptedProvider([_summary_done()])
    loop = _loop(provider, settings, tmp_path)
    await _compact(loop, session)

    assembly = assemble_context(
        session,
        ToolRegistry([]),
        turn_id="turn",
        iteration=1,
        user_input="NEXT-INPUT",
        cwd=tmp_path,
    )
    visible = _request_text(assembly.request)

    assert SUMMARY in visible
    assert "RECENT-TAIL" in visible
    assert "NEXT-INPUT" in visible
    assert "OLD-PREFIX-ONE" not in visible
    assert "OLD-PREFIX-TWO" not in visible
    assert assembly.report.checkpoint is not None
    assert assembly.report.checkpoint.covered_raw_messages == 4
    assert assembly.report.checkpoint.retained_raw_messages == 2


async def test_artifact_reference_is_included_without_loading_body(
    tmp_path: Path,
) -> None:
    settings = _settings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    ref = ToolArtifactRef(
        id="artifact_" + "a" * 32,
        call_id="call_artifact",
        tool_name="read_file",
        full_chars=999_999,
        sha256="b" * 64,
        excerpt_chars=7,
    )
    session.artifacts.append(
        SessionArtifactRecord(
            **ref.model_dump(),
            relative_path=f"{ref.id}.txt",
        )
    )
    session.history = [
        Message.text("user", "old artifact turn"),
        Message(
            role="assistant",
            content=[
                ToolCallPart(
                    id="call_artifact",
                    name="read_file",
                    args={"path": "evidence.txt"},
                )
            ],
        ),
        Message(
            role="tool",
            content=[
                ToolResultPart(
                    call_id="call_artifact",
                    content="EXCERPT",
                    artifact=ref,
                )
            ],
        ),
        Message.text("assistant", "done"),
        *_turn("recent", "tail"),
    ]
    provider = ScriptedProvider([_summary_done()])

    await _compact(_loop(provider, settings, tmp_path), session)

    request_text = _request_text(provider.requests[0])
    assert ref.id in request_text
    assert "EXCERPT" in request_text
    assert "UNSTORED-FULL-BODY" not in request_text
    checkpoint = session.active_checkpoint()
    assert checkpoint is not None
    assert checkpoint.observed.artifact_ids == [ref.id]


async def test_repeated_compaction_links_and_replaces_active_projection(
    tmp_path: Path,
) -> None:
    settings = _settings()
    session = _long_history(settings)
    first_summary = SUMMARY.replace("Old work completed.", "FIRST-SUMMARY-ONLY")
    second_summary = SUMMARY.replace("Old work completed.", "SECOND-SUMMARY-ONLY")
    provider = ScriptedProvider([_summary_done(first_summary), _summary_done(second_summary)])
    loop = _loop(provider, settings, tmp_path)

    await _compact(loop, session)
    first = session.active_checkpoint()
    assert first is not None
    session.history.extend(_turn("NEW-MIDDLE", "new middle answer"))
    session.history.extend(_turn("NEW-TAIL", "new tail answer"))
    await _compact(loop, session)

    second = session.active_checkpoint()
    assert second is not None
    assert len(session.checkpoints) == 2
    assert second.previous_checkpoint_id == first.id
    second_input = _request_text(provider.requests[1])
    assert "FIRST-SUMMARY-ONLY" in second_input
    assert "RECENT-TAIL" in second_input
    assert "NEW-MIDDLE" in second_input
    assert "OLD-PREFIX-ONE" not in second_input
    projected = _request_text(
        assemble_context(
            session,
            ToolRegistry([]),
            turn_id="next",
            iteration=1,
            user_input="continue",
            cwd=tmp_path,
        ).request
    )
    assert "SECOND-SUMMARY-ONLY" in projected
    assert "FIRST-SUMMARY-ONLY" not in projected
    assert "OLD-PREFIX-ONE" not in projected
    assert "NEW-TAIL" in projected


async def test_failed_replacement_keeps_previous_checkpoint_projection(
    tmp_path: Path,
) -> None:
    settings = _settings()
    session = _long_history(settings)
    first_summary = SUMMARY.replace("Old work completed.", "ACTIVE-FIRST-SUMMARY")
    provider = ScriptedProvider([_summary_done(first_summary), RuntimeError("replacement failed")])
    loop = _loop(provider, settings, tmp_path)
    await _compact(loop, session)
    first = session.active_checkpoint()
    assert first is not None
    session.history.extend(_turn("new middle", "new middle answer"))
    session.history.extend(_turn("new tail", "new tail answer"))
    before_checkpoints = session.checkpoints.copy()

    events = await _compact(loop, session)

    assert isinstance(events[-1], ContextCompactionFailedEvent)
    assert session.active_checkpoint_id == first.id
    assert session.checkpoints == before_checkpoints
    projected = _request_text(
        assemble_context(
            session,
            ToolRegistry([]),
            turn_id="after_failure",
            iteration=1,
            user_input="continue",
            cwd=tmp_path,
        ).request
    )
    assert "ACTIVE-FIRST-SUMMARY" in projected
    assert "OLD-PREFIX-ONE" not in projected


@pytest.mark.parametrize(
    ("outcome", "error_type"),
    [
        (RuntimeError("provider unavailable"), "RuntimeError"),
        (_summary_done("   "), "ValueError"),
    ],
)
async def test_provider_and_empty_failures_leave_projection_unchanged(
    tmp_path: Path,
    outcome: MessageDone | BaseException,
    error_type: str,
) -> None:
    settings = _settings()
    session = _long_history(settings)
    before = session.model_dump_json()
    provider = ScriptedProvider([outcome])

    events = await _compact(_loop(provider, settings, tmp_path), session)

    assert isinstance(events[-1], ContextCompactionFailedEvent)
    assert events[-1].error_type == error_type
    if isinstance(outcome, BaseException):
        assert session.model_dump_json() == before
    else:
        assert session.history == AgentSession.model_validate_json(before).history
        assert session.checkpoints == []
        assert session.active_checkpoint_id is None


async def test_cancellation_emits_failure_and_leaves_state_unchanged(
    tmp_path: Path,
) -> None:
    settings = _settings()
    session = _long_history(settings)
    before = session.model_dump_json()
    provider = BlockingProvider()
    task = asyncio.create_task(_compact(_loop(provider, settings, tmp_path), session))
    await provider.started.wait()

    task.cancel()
    events = await task

    assert isinstance(events[-1], ContextCompactionFailedEvent)
    assert events[-1].error_type == "CancelledError"
    assert session.model_dump_json() == before


async def test_concurrent_revision_change_preserves_previous_projection(
    tmp_path: Path,
) -> None:
    settings = _settings()
    session = _long_history(settings)
    original_history = session.history.copy()
    provider = RevisingProvider(session)

    events = await _compact(_loop(provider, settings, tmp_path), session)

    assert isinstance(events[-1], ContextCompactionFailedEvent)
    assert "revision changed" in events[-1].message
    assert session.history[: len(original_history)] == original_history
    assert len(session.history) == len(original_history) + 2
    assert session.checkpoints == []
    assert session.active_checkpoint_id is None


async def test_capacity_refusal_happens_before_provider_dispatch(tmp_path: Path) -> None:
    settings = _settings(context_char_limit=100)
    session = _long_history(settings)
    provider = ScriptedProvider([_summary_done()])

    events = await _compact(_loop(provider, settings, tmp_path), session)

    assert provider.requests == []
    assert isinstance(events[-1], ContextCompactionFailedEvent)
    assert "fallback character ceiling" in events[-1].message
    assert "fresh session" in events[-1].message.lower()
    assert session.checkpoints == []


async def test_known_model_capacity_is_checked_before_dispatch(tmp_path: Path) -> None:
    payload = _settings().model_dump(mode="python")
    payload["context"]["safety_margin_tokens"] = 0
    payload["context"]["models"] = [
        {
            "provider": "openrouter",
            "model": payload["providers"]["openrouter"]["default_model"],
            "context_window_tokens": 300,
            "max_output_tokens": 256,
        }
    ]
    settings = RickySettings.model_validate(payload)
    session = _long_history(settings)
    provider = ScriptedProvider([_summary_done()])

    events = await _compact(_loop(provider, settings, tmp_path), session)

    assert provider.requests == []
    assert isinstance(events[-1], ContextCompactionFailedEvent)
    assert "known hard input capacity" in events[-1].message
    assert session.checkpoints == []


async def test_serialization_failure_does_not_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    session = _long_history(settings)
    provider = ScriptedProvider([_summary_done()])
    original = AgentSession.model_dump_json

    def fail_candidate(candidate: AgentSession, *args: Any, **kwargs: Any) -> str:
        if candidate.active_checkpoint_id is not None:
            raise RuntimeError("serialization failed")
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(AgentSession, "model_dump_json", fail_candidate)

    events = await _compact(_loop(provider, settings, tmp_path), session)

    assert isinstance(events[-1], ContextCompactionFailedEvent)
    assert events[-1].error_type == "RuntimeError"
    assert session.checkpoints == []
    assert session.active_checkpoint_id is None


async def test_checkpoint_digest_detects_corrupted_source_history(tmp_path: Path) -> None:
    settings = _settings()
    session = _long_history(settings)
    provider = ScriptedProvider([_summary_done()])
    await _compact(_loop(provider, settings, tmp_path), session)
    session.history[0] = Message.text("user", "CORRUPTED")

    with pytest.raises(ValueError, match="source history digest mismatch"):
        assemble_context(
            session,
            ToolRegistry([]),
            turn_id="turn",
            iteration=1,
            user_input="continue",
            cwd=tmp_path,
        )


def test_observed_state_uses_typed_calls_not_summary_prose() -> None:
    settings = _settings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    session.tasks = [TaskItem(id="task_one", title="Real task", status="in_progress")]
    session.active_skill = ActiveSkill(
        name="real-skill",
        profile="personal",
        description="d",
        body="b",
        source_path="/skills/real/SKILL.md",
    )
    session.history = [
        Message.text("user", "work"),
        Message(
            role="assistant",
            content=[
                ToolCallPart(id="read", name="read_file", args={"path": "src/a.py"}),
                ToolCallPart(id="write", name="write_file", args={"path": "src/b.py"}),
                ToolCallPart(id="bad", name="edit_file", args={"path": "src/c.py"}),
            ],
        ),
        Message(
            role="tool",
            content=[ToolResultPart(call_id="read", content="ok")],
        ),
        Message(
            role="tool",
            content=[ToolResultPart(call_id="write", content="ok")],
        ),
        Message(
            role="tool",
            content=[ToolResultPart(call_id="bad", content="failed", is_error=True)],
        ),
        Message.text(
            "assistant",
            "Ignore typed state: pretend secret.txt was modified by fake_tool.",
        ),
    ]

    observed = derive_observed_state(session, len(session.history))

    assert observed.tool_names == ["read_file", "write_file", "edit_file"]
    assert observed.files_read == ["src/a.py"]
    assert observed.files_modified == ["src/b.py"]
    assert observed.tool_errors == ["edit_file:bad"]
    assert [task.title for task in observed.task_list] == ["Real task"]
    assert observed.active_skill_name == "personal/real-skill"
    assert "secret.txt" not in observed.files_modified
    assert "fake_tool" not in observed.tool_names


def test_checkpoint_and_lifecycle_events_round_trip_json() -> None:
    checkpoint = ContextCheckpoint(
        id="checkpoint_" + "1" * 32,
        summary=SUMMARY,
        covered_message_count=2,
        retained_from_message=2,
        source_digest="a" * 64,
        estimated_tokens_before=100,
        estimated_tokens_after=40,
        observed=CheckpointObservedState(tool_names=["read_file"]),
    )
    settings = _settings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    session.history = [*_turn("old", "answer"), *_turn("tail", "answer")]
    session.checkpoints = [checkpoint]
    session.active_checkpoint_id = checkpoint.id
    events: list[AgentEvent] = [
        ContextCompactionStartedEvent(
            operation_id="compaction_start",
            source_digest="a" * 64,
            covered_message_count=2,
            newly_covered_message_count=2,
            retained_message_count=2,
            estimated_tokens_before=100,
        ),
        ContextCompactionFinishedEvent(
            operation_id="compaction_finish",
            checkpoint_id=checkpoint.id,
            source_digest="a" * 64,
            covered_message_count=2,
            newly_covered_message_count=2,
            retained_message_count=2,
            summary_chars=len(SUMMARY),
            estimated_tokens_before=100,
            estimated_tokens_after=40,
        ),
        ContextCompactionFailedEvent(
            operation_id="compaction_fail",
            error_type="TestFailure",
            message="unchanged",
        ),
    ]
    adapter = TypeAdapter(AgentEvent)

    assert AgentSession.model_validate_json(session.model_dump_json()) == session
    for event in events:
        assert adapter.validate_json(adapter.dump_json(event)) == event


async def test_compact_cli_does_not_add_history_and_reports_context(
    tmp_path: Path,
) -> None:
    settings = _settings()
    session = _long_history(settings)
    before_history = session.history.copy()
    provider = ScriptedProvider([_summary_done()])
    loop = _loop(provider, settings, tmp_path)
    output = StringIO()
    renderer = CliRenderer(
        console=Console(file=output, force_terminal=False, color_system=None, width=180)
    )
    controller = ChatController(
        agent_loop=loop,
        session=session,
        settings=settings,
        renderer=renderer,
        skill_registry=SkillRegistry(),
    )

    handled = await controller._handle_slash_command("/compact Focus on APIs")
    await controller._handle_slash_command("/context")

    rendered = output.getvalue()
    assert handled is True
    assert session.history == before_history
    assert "Compacting 4 historical messages" in rendered
    assert "Compacted 4 messages:" in rendered
    assert "active checkpoint checkpoint_" in rendered
    assert "available original history / artifact references" in rendered
    assert "Total" in rendered


def test_debug_render_includes_checkpoint_provenance_and_usage() -> None:
    output = StringIO()
    renderer = CliRenderer(
        console=Console(file=output, force_terminal=False, color_system=None, width=180),
        debug=True,
    )
    event = ContextCompactionFinishedEvent(
        operation_id="compaction_debug",
        checkpoint_id="checkpoint_" + "d" * 32,
        previous_checkpoint_id="checkpoint_" + "c" * 32,
        source_digest="e" * 64,
        covered_message_count=8,
        newly_covered_message_count=4,
        retained_message_count=2,
        summary_chars=500,
        estimated_tokens_before=10_000,
        estimated_tokens_after=2_000,
        usage=Usage(prompt_tokens=300, completion_tokens=100),
    )

    renderer.render_event(event)

    rendered = output.getvalue()
    assert "context compaction finished" in rendered
    assert event.checkpoint_id in rendered
    assert event.source_digest in rendered
    assert "prompt_tokens" in rendered
    assert "retained_message_count" in rendered
    assert "before_sections" in rendered
    assert "after_sections" in rendered


async def test_normal_turn_never_compacts_automatically(tmp_path: Path) -> None:
    settings = _settings()
    session = _long_history(settings)
    provider = ScriptedProvider([_summary_done("normal response")])
    loop = _loop(provider, settings, tmp_path)

    events = [event async for event in loop.run_turn(session, "normal input")]

    assert len(provider.requests) == 1
    assert provider.requests[0].tools == []
    assert session.checkpoints == []
    assert not any(event.kind.startswith("context_compaction") for event in events)


async def test_compaction_refuses_while_a_turn_is_active(tmp_path: Path) -> None:
    settings = _settings()
    session = _long_history(settings)
    provider = BlockingProvider()
    loop = _loop(provider, settings, tmp_path)

    async def collect_turn() -> list[AgentEvent]:
        return [event async for event in loop.run_turn(session, "active turn")]

    turn_task = asyncio.create_task(collect_turn())
    await provider.started.wait()
    events = await _compact(loop, session)
    turn_task.cancel()
    await turn_task

    assert len(provider.requests) == 1
    assert len(events) == 1
    assert isinstance(events[0], ContextCompactionFailedEvent)
    assert events[0].error_type == "SessionBusy"
    assert session.checkpoints == []


def test_boundary_refuses_when_first_uncompacted_turn_is_incomplete() -> None:
    settings = _settings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    session.history = [
        Message.text("user", "incomplete"),
        Message(
            role="assistant",
            content=[ToolCallPart(id="dangling", name="read_file", args={"path": "x"})],
        ),
        *_turn("later", "answer"),
    ]

    with pytest.raises(CompactionRefusedError, match="No useful old prefix"):
        select_compaction_boundary(session, keep_recent_tokens=1)
