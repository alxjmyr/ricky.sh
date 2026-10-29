"""Automatic compaction at the ordinary agent request boundary."""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar, cast

import pytest
from pydantic import BaseModel, ConfigDict

from ricky.agent import AgentLoop, AgentSession, assemble_context
from ricky.agent.events import (
    AgentEvent,
    ContextCompactionFailedEvent,
    ContextCompactionFinishedEvent,
    TurnFinishedEvent,
)
from ricky.config import RickySettings
from ricky.llm import (
    CompletionRequest,
    ImagePart,
    MediaArtifactRef,
    Message,
    MessageDone,
    StreamEvent,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    Usage,
    UserContent,
)
from ricky.profiles import ProfileLabel
from ricky.tools import Risk, ToolContext, ToolRegistry, ToolResult


def _done(text: str = "Finished.", *, tokens: int = 3) -> MessageDone:
    return MessageDone(
        message=Message.text("assistant", text),
        usage=Usage(prompt_tokens=tokens, completion_tokens=tokens),
        stop_reason="stop",
    )


class Provider:
    name = "scripted"

    def __init__(
        self,
        responses: list[MessageDone],
        *,
        summary_error: BaseException | None = None,
        on_summary: Callable[[], None] | None = None,
    ) -> None:
        self.responses = list(responses)
        self.summary_error = summary_error
        self.on_summary = on_summary
        self.requests: list[CompletionRequest] = []

    @property
    def summaries(self) -> list[CompletionRequest]:
        return [request for request in self.requests if request.session_id is None]

    @property
    def ordinary(self) -> list[CompletionRequest]:
        return [request for request in self.requests if request.session_id is not None]

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        if request.session_id is None:
            assert request.tools == []
            if self.on_summary is not None:
                self.on_summary()
            if self.summary_error is not None:
                raise self.summary_error
            yield _done("Older observations are complete; preserve the current request.", tokens=7)
        else:
            yield self.responses.pop(0)

    async def aclose(self) -> None:
        pass


def _settings(*, auto_enabled: bool = True) -> RickySettings:
    return RickySettings.model_validate(
        {
            "context_char_limit": 1_000_000,
            "context": {
                "compaction": {
                    "auto_enabled": auto_enabled,
                    "keep_recent_tokens": 1,
                    "max_summary_tokens": 256,
                },
                "tool_results": {"enabled": False},
            },
        }
    )


def _history(settings: RickySettings) -> AgentSession:
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    session.history = [
        Message.text("user", "Inspect the old page."),
        Message.text("assistant", "OLD " * 6_000),
        Message.text("user", "Keep the recent decision."),
        Message.text("assistant", "The task remains read-only."),
    ]
    return session


def _pressure(
    session: AgentSession,
    tmp_path: Path,
    prompt: str | UserContent,
    *,
    ratio: float = 0.95,
    extra_system_sections: dict[str, str] | None = None,
    extra_context_sections: dict[str, str] | None = None,
) -> int:
    report = assemble_context(
        session,
        ToolRegistry([]),
        turn_id="measure",
        iteration=1,
        user_input=prompt,
        cwd=tmp_path,
        extra_system_sections=extra_system_sections,
        extra_context_sections=extra_context_sections,
    ).report
    limit = math.ceil(report.serialized_chars / ratio)
    session.settings_snapshot["context_char_limit"] = limit
    return limit


def _loop(provider: Provider, settings: RickySettings, tmp_path: Path) -> AgentLoop:
    return AgentLoop(provider=provider, registry=ToolRegistry([]), settings=settings, cwd=tmp_path)


def _finished(events: list[AgentEvent]) -> TurnFinishedEvent:
    assert isinstance(events[-1], TurnFinishedEvent)
    return events[-1]


@pytest.mark.parametrize("ratio,expected", [(0.89, False), (0.91, True)])
async def test_default_trigger_and_target_use_complete_character_budget(
    tmp_path: Path, ratio: float, expected: bool
) -> None:
    settings = _settings()
    assert settings.context.compaction.auto_enabled
    assert settings.context.compaction.auto_trigger_ratio == 0.90
    assert settings.context.compaction.auto_target_ratio == 0.65
    session = _history(settings)
    limit = _pressure(session, tmp_path, "Continue.", ratio=ratio)
    provider = Provider([_done()])

    events = [
        event async for event in _loop(provider, settings, tmp_path).run_turn(session, "Continue.")
    ]

    assert bool(provider.summaries) is expected
    assert _finished(events).error is None
    assert len(provider.ordinary) == 1
    if expected:
        finished = next(
            event for event in events if isinstance(event, ContextCompactionFinishedEvent)
        )
        assert finished.after_report is not None
        assert finished.after_report.pending_user_input_included
        assert finished.after_report.serialized_chars <= math.floor(limit * 0.65)
        assert _finished(events).usage == Usage(prompt_tokens=10, completion_tokens=10)
        assert session.cumulative_usage == _finished(events).usage


async def test_disabling_automatic_compaction_preserves_manual_operation(tmp_path: Path) -> None:
    settings = _settings(auto_enabled=False)
    session = _history(settings)
    _pressure(session, tmp_path, "Continue.")
    provider = Provider([_done()])
    loop = _loop(provider, settings, tmp_path)

    events = [event async for event in loop.run_turn(session, "Continue.")]

    assert _finished(events).error is None
    assert provider.summaries == []
    assert session.checkpoints == []
    manual = [event async for event in loop.compact_context(session)]
    assert isinstance(manual[-1], ContextCompactionFinishedEvent)


@pytest.mark.parametrize("source", ["pending", "system", "runtime"])
async def test_trigger_accounts_for_pending_input_and_request_sections(
    tmp_path: Path, source: str
) -> None:
    settings = _settings()
    session = _history(settings)
    prompt = "Continue. " + ("new input " * 700 if source == "pending" else "")
    system = {"task_instructions": "constraints " * 700} if source == "system" else {}
    runtime = {"task_activity": "observation " * 700} if source == "runtime" else {}
    limit = _pressure(
        session,
        tmp_path,
        prompt,
        extra_system_sections=system,
        extra_context_sections=runtime,
    )
    without = assemble_context(
        session, ToolRegistry([]), turn_id="baseline", iteration=1, cwd=tmp_path
    ).report
    assert without.serialized_chars < limit * 0.90
    provider = Provider([_done()])

    events = [
        event
        async for event in _loop(provider, settings, tmp_path).run_turn(
            session,
            prompt,
            extra_system_sections=system,
            extra_context_sections=runtime,
        )
    ]

    assert _finished(events).error is None
    assert provider.summaries
    normal = provider.ordinary[-1]
    assert Message.text("user", prompt) in normal.messages
    assert len(session.checkpoints) == 1
    finished = next(event for event in events if isinstance(event, ContextCompactionFinishedEvent))
    assert finished.after_report is not None
    assert finished.after_report.serialized_chars <= math.floor(limit * 0.65)


async def test_token_capacity_and_per_request_output_cap_drive_compaction(tmp_path: Path) -> None:
    settings = _settings()
    session = _history(settings)
    initial = assemble_context(
        session,
        ToolRegistry([]),
        turn_id="measure",
        iteration=1,
        user_input="Continue.",
        cwd=tmp_path,
        max_completion_tokens=64,
    ).report
    hard_input = math.ceil(initial.estimated_input_tokens / 0.95)
    session.model_context = session.model_context.model_copy(
        update={
            "context_window_tokens": hard_input + 64 + settings.context.safety_margin_tokens,
            "source": "configured",
        }
    )
    provider = Provider([_done()])

    events = [
        event
        async for event in _loop(provider, settings, tmp_path).run_turn(
            session, "Continue.", max_completion_tokens_per_request=64
        )
    ]

    assert _finished(events).error is None
    assert provider.summaries
    assert all(request.max_tokens == 64 for request in provider.requests)
    finished = next(event for event in events if isinstance(event, ContextCompactionFinishedEvent))
    assert finished.after_report is not None
    assert finished.after_report.estimated_input_tokens <= math.floor(hard_input * 0.65)
    assert finished.after_report.budget.hard_input_tokens == hard_input


class ObserveParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    page: str


class ObserveTool:
    name: ClassVar[str] = "observe_page"
    description: ClassVar[str] = "Read one page observation."
    Params: ClassVar[type[BaseModel]] = ObserveParams
    risk: ClassVar[Risk] = "read_only"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(params, ObserveParams)
        self.calls.append(params.page)
        return ToolResult(content=f"PAGE {params.page}: " + "evidence " * 1_500)


def _batch(index: int) -> MessageDone:
    return MessageDone(
        message=Message(
            role="assistant",
            content=[
                ToolCallPart(
                    id=f"{index}-{part}", name="observe_page", args={"page": f"{index}-{part}"}
                )
                for part in range(2)
            ],
        ),
        usage=Usage(prompt_tokens=3, completion_tokens=3),
        stop_reason="tool_calls",
    )


async def test_automatic_compaction_waits_for_complete_batch_and_never_replays_tools(
    tmp_path: Path,
) -> None:
    settings = _settings()
    settings.context_char_limit = 65_000
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    tool = ObserveTool()

    def assert_settled() -> None:
        assert tool.calls == ["0-0", "0-1", "1-0", "1-1"]
        assert (
            sum(
                isinstance(part, ToolResultPart)
                for message in session.history
                for part in message.content
            )
            == 4
        )

    provider = Provider([_batch(0), _batch(1), _done()], on_summary=assert_settled)
    loop = AgentLoop(
        provider=provider, registry=ToolRegistry([tool]), settings=settings, cwd=tmp_path
    )

    events = [
        event async for event in loop.run_turn(session, "Inspect pages; do not submit an order.")
    ]

    assert _finished(events).error is None
    assert provider.summaries
    assert len(provider.ordinary) == 3
    assert tool.calls == ["0-0", "0-1", "1-0", "1-1"]
    assert session.history[0] in provider.ordinary[-1].messages
    assert _finished(events).usage == Usage(prompt_tokens=16, completion_tokens=16)


async def test_long_browser_style_turn_can_compact_multiple_times_without_replay(
    tmp_path: Path,
) -> None:
    settings = _settings()
    settings.context_char_limit = 65_000
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    tool = ObserveTool()
    provider = Provider([*[_batch(index) for index in range(5)], _done()])
    loop = AgentLoop(
        provider=provider, registry=ToolRegistry([tool]), settings=settings, cwd=tmp_path
    )
    prompt = "Inspect all pages; never submit an order."

    events = [event async for event in loop.run_turn(session, prompt)]

    assert _finished(events).error is None
    assert len(provider.summaries) >= 2
    assert len(provider.ordinary) == 6
    assert tool.calls == [f"{index}-{part}" for index in range(5) for part in range(2)]
    assert provider.ordinary[-1].messages.count(Message.text("user", prompt)) == 1
    for previous, current in zip(session.checkpoints, session.checkpoints[1:], strict=False):
        assert current.previous_checkpoint_id == previous.id
        assert current.retained_from_message > previous.retained_from_message
    expected_tokens = 6 * 3 + len(provider.summaries) * 7
    assert _finished(events).usage == Usage(
        prompt_tokens=expected_tokens, completion_tokens=expected_tokens
    )
    assert session.cumulative_usage == _finished(events).usage


async def test_closing_at_compaction_start_releases_session_before_any_provider_call(
    tmp_path: Path,
) -> None:
    settings = _settings()
    session = _history(settings)
    _pressure(session, tmp_path, "Continue.")
    projection_fields = {"history", "checkpoints", "active_checkpoint_id", "cumulative_usage"}
    original = session.model_dump_json(include=projection_fields)
    provider = Provider([_done()])
    loop = _loop(provider, settings, tmp_path)
    iterator = cast(AsyncGenerator[AgentEvent, None], loop.run_turn(session, "Continue."))
    try:
        async for event in iterator:
            if event.kind == "context_compaction_started":
                break
        else:
            pytest.fail("automatic compaction did not start")
    finally:
        await asyncio.wait_for(iterator.aclose(), 2)

    assert provider.requests == []
    assert session.model_dump_json(include=projection_fields) == original
    events = [event async for event in loop.run_turn(session, "Continue.")]
    assert _finished(events).error is None
    assert len(provider.summaries) == 1
    assert len(provider.ordinary) == 1


@pytest.mark.parametrize("fits", [True, False])
async def test_failed_compaction_continues_only_if_upcoming_request_fits(
    tmp_path: Path, fits: bool
) -> None:
    settings = _settings()
    session = _history(settings)
    _pressure(session, tmp_path, "Continue.", ratio=0.95 if fits else 1.10)
    original = session.model_dump_json(include={"history", "checkpoints", "active_checkpoint_id"})
    provider = Provider([_done()], summary_error=RuntimeError("summary unavailable"))

    events = [
        event async for event in _loop(provider, settings, tmp_path).run_turn(session, "Continue.")
    ]

    assert sum(isinstance(event, ContextCompactionFailedEvent) for event in events) == 1
    assert len(provider.ordinary) == int(fits)
    assert bool(_finished(events).error) is not fits
    assert session.checkpoints == []
    if not fits:
        assert (
            session.model_dump_json(include={"history", "checkpoints", "active_checkpoint_id"})
            == original
        )


async def test_failed_compaction_is_not_repeated_for_empty_responses_or_clock_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ricky.agent.context as context_module
    import ricky.agent.loop as loop_module

    class SteppingClock:
        tick = 0

        @classmethod
        def now(cls, tz: object = None) -> datetime:
            cls.tick += 1
            return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=cls.tick)

    monkeypatch.setattr(context_module, "datetime", SteppingClock)
    monkeypatch.setattr(loop_module, "datetime", SteppingClock)
    settings = _settings()
    session = _history(settings)
    _pressure(session, tmp_path, "Continue.")
    provider = Provider([_done(""), _done(""), _done()], summary_error=RuntimeError("unavailable"))

    events = [
        event async for event in _loop(provider, settings, tmp_path).run_turn(session, "Continue.")
    ]

    assert _finished(events).error is None
    assert len(provider.summaries) == 1
    assert len(provider.ordinary) == 3
    assert provider.ordinary[0].runtime_context != provider.ordinary[-1].runtime_context


async def test_cancellation_during_automatic_summary_stops_turn_without_dispatch(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    closed = asyncio.Event()

    class BlockingSummary(Provider):
        async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
            self.requests.append(request)
            assert request.session_id is None
            entered.set()
            try:
                await asyncio.Event().wait()
                yield _done("unreachable")
            finally:
                closed.set()

    settings = _settings()
    session = _history(settings)
    _pressure(session, tmp_path, "Continue.")
    original = session.model_dump_json(include={"history", "checkpoints", "active_checkpoint_id"})
    provider = BlockingSummary([])
    loop = _loop(provider, settings, tmp_path)

    async def collect() -> list[AgentEvent]:
        return [event async for event in loop.run_turn(session, "Continue.")]

    task = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        events = await asyncio.wait_for(task, 2)
        assert _finished(events).interrupted
        assert closed.is_set()
        assert provider.ordinary == []
        assert (
            session.model_dump_json(include={"history", "checkpoints", "active_checkpoint_id"})
            == original
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_image_preflight_allows_text_pressure_recovery_without_dropping_upload(
    tmp_path: Path,
) -> None:
    settings = _settings()
    session = _history(settings)
    image = ImagePart(
        artifact=MediaArtifactRef(
            id="media_" + "1" * 32,
            sha256="2" * 64,
            byte_count=321,
            width=9,
            height=7,
            source_label=ProfileLabel.owned_by("personal"),
        )
    )
    prompt = UserContent(parts=[TextPart(text="Inspect this upload."), image])
    _pressure(session, tmp_path, prompt, ratio=1.10)
    provider = Provider([_done()])
    loop = _loop(provider, settings, tmp_path)

    await loop.validate_image_input(session, prompt)
    events = [event async for event in loop.run_turn(session, prompt)]

    assert _finished(events).error is None
    assert provider.summaries
    assert image in provider.ordinary[-1].messages[-1].content
    assert (
        sum(
            isinstance(part, ImagePart)
            for message in provider.ordinary[-1].messages
            for part in message.content
        )
        == 1
    )


async def test_image_preflight_rejects_inherently_oversized_new_input(tmp_path: Path) -> None:
    settings = _settings()
    settings.context_char_limit = 12_000
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    image = ImagePart(
        artifact=MediaArtifactRef(
            id="media_" + "1" * 32,
            sha256="2" * 64,
            byte_count=321,
            width=9,
            height=7,
            source_label=ProfileLabel.owned_by("personal"),
        )
    )
    prompt = UserContent(parts=[TextPart(text="new input " * 2_000), image])
    provider = Provider([_done()])

    with pytest.raises(ValueError, match="context character limit"):
        await _loop(provider, settings, tmp_path).validate_image_input(session, prompt)

    assert provider.requests == []
    assert session.history == []
