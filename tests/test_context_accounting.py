"""Context accounting and inspection regressions."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter
from rich.console import Console

from ricky.agent import (
    AgentLoop,
    AgentSession,
    ContextAssembledEvent,
    assemble_context,
)
from ricky.agent.context import serialize_canonical_request
from ricky.agent.events import AgentEvent
from ricky.config import RickySettings
from ricky.interfaces.cli.chat import ChatController
from ricky.interfaces.cli.render import CliRenderer
from ricky.llm import (
    ImagePart,
    MediaArtifactRef,
    Message,
    ModelInfo,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolResultPart,
    UserContent,
)
from ricky.profiles import ProfileLabel
from ricky.skills.spec import ActiveSkill
from ricky.tools import ToolRegistry, builtin_tools


class _Memory:
    index_char_limit = 1_000

    def render_index(self, _limit: int) -> str:
        return "memory index"


class _NoCallProvider:
    name = "no-call"

    def __init__(self) -> None:
        self.requests = 0

    async def stream(self, _request: object) -> Any:
        self.requests += 1
        if False:
            yield None

    async def aclose(self) -> None:
        pass


def _image(index: int, *, byte_count: int = 100, width: int = 10, height: int = 5) -> ImagePart:
    return ImagePart(
        artifact=MediaArtifactRef(
            id=f"media_{index:032x}",
            byte_count=byte_count,
            sha256=f"{index:064x}",
            width=width,
            height=height,
            source_label=ProfileLabel.owned_by("personal"),
        )
    )


def test_context_projects_latest_two_images_and_accounts_shared_limits() -> None:
    settings = RickySettings.model_validate(
        {
            "context": {
                "media": {
                    "request_image_limit": 2,
                    "request_image_byte_limit": 1_000,
                    "request_image_pixel_limit": 1_000,
                    "default_image_token_estimate": 1_234,
                }
            }
        }
    )
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    session.history = [
        Message(role="user", content=[TextPart(text="first"), _image(1)]),
        Message(role="assistant", content=[TextPart(text="noted")]),
        Message(role="user", content=[_image(2)]),
        Message(role="assistant", content=[TextPart(text="noted")]),
        Message(role="user", content=[_image(3)]),
    ]

    assembly = assemble_context(
        session,
        ToolRegistry([]),
        turn_id="turn_images",
        iteration=1,
    )
    projected = [
        part
        for message in assembly.request.messages
        for part in message.content
        if isinstance(part, ImagePart)
    ]
    request_text = "\n".join(
        part.text
        for message in assembly.request.messages
        for part in message.content
        if isinstance(part, TextPart)
    )

    assert [part.artifact.id for part in projected] == [
        _image(2).artifact.id,
        _image(3).artifact.id,
    ]
    assert "older image omitted from this request" in request_text
    assert assembly.report.retained_image_count == 3
    assert assembly.report.projected_image_count == 2
    assert assembly.report.projected_image_bytes == 200
    assert assembly.report.projected_image_pixels == 100
    assert assembly.report.estimated_image_tokens == 2_468
    sections = {section.name: section for section in assembly.report.sections}
    assert sections["conversation_images"].estimated_tokens >= 2_468

    pending = UserContent(parts=[_image(4), _image(5)])
    pending_assembly = assemble_context(
        session,
        ToolRegistry([]),
        turn_id="turn_pending_images",
        iteration=1,
        user_input=pending,
    )
    assert pending_assembly.report.projected_image_count == 2
    assert pending_assembly.report.retained_image_count == 3
    assert all(
        part.artifact.id in {_image(4).artifact.id, _image(5).artifact.id}
        for message in pending_assembly.request.messages
        for part in message.content
        if isinstance(part, ImagePart)
    )


@pytest.mark.parametrize(
    ("byte_limit", "pixel_limit", "image_kwargs"),
    [
        (130, 1_000, {"byte_count": 60, "width": 10, "height": 1}),
        (1_000, 130, {"byte_count": 10, "width": 10, "height": 6}),
    ],
    ids=["bytes", "pixels"],
)
def test_context_omits_older_history_images_at_each_request_budget(
    byte_limit: int,
    pixel_limit: int,
    image_kwargs: dict[str, int],
) -> None:
    settings = RickySettings.model_validate(
        {
            "context": {
                "media": {
                    "request_image_limit": 3,
                    "request_image_byte_limit": byte_limit,
                    "request_image_pixel_limit": pixel_limit,
                }
            }
        }
    )
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    session.history = [
        Message(role="user", content=[_image(index, **image_kwargs)]) for index in range(1, 4)
    ]

    assembly = assemble_context(
        session,
        ToolRegistry([]),
        turn_id=f"history_{byte_limit}_{pixel_limit}",
        iteration=1,
    )
    projected = [
        part.artifact.id
        for message in assembly.request.messages
        for part in message.content
        if isinstance(part, ImagePart)
    ]

    assert projected == [_image(2).artifact.id, _image(3).artifact.id]
    assert assembly.report.projected_image_count == 2
    assert assembly.report.projected_image_bytes <= byte_limit
    assert assembly.report.projected_image_pixels <= pixel_limit


def test_pending_images_reserve_byte_and_pixel_budgets_before_history_projection() -> None:
    settings = RickySettings.model_validate(
        {
            "context": {
                "media": {
                    "request_image_limit": 3,
                    "request_image_byte_limit": 150,
                    "request_image_pixel_limit": 100,
                }
            }
        }
    )
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    session.history = [
        Message(
            role="user",
            content=[_image(1, byte_count=60, width=10, height=5)],
        )
    ]
    pending = _image(2, byte_count=100, width=10, height=8)

    assembly = assemble_context(
        session,
        ToolRegistry([]),
        turn_id="pending_reserves_media_budgets",
        iteration=1,
        user_input=UserContent(parts=[pending]),
    )
    projected = [
        part.artifact.id
        for message in assembly.request.messages
        for part in message.content
        if isinstance(part, ImagePart)
    ]

    assert projected == [pending.artifact.id]
    assert assembly.report.projected_image_bytes == 100
    assert assembly.report.projected_image_pixels == 80


def test_context_rejects_image_count_byte_and_pixel_limit_overflow() -> None:
    base = {
        "context": {
            "media": {
                "request_image_limit": 2,
                "request_image_byte_limit": 150,
                "request_image_pixel_limit": 75,
            }
        }
    }
    settings = RickySettings.model_validate(base)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())

    with pytest.raises(ValueError, match="request image limit"):
        assemble_context(
            session,
            ToolRegistry([]),
            turn_id="count",
            iteration=1,
            user_input=UserContent(parts=[_image(1), _image(2), _image(3)]),
        )
    with pytest.raises(ValueError, match="request image byte limit"):
        assemble_context(
            session,
            ToolRegistry([]),
            turn_id="bytes",
            iteration=1,
            user_input=UserContent(parts=[_image(1, byte_count=151)]),
        )
    with pytest.raises(ValueError, match="request image pixel limit"):
        assemble_context(
            session,
            ToolRegistry([]),
            turn_id="pixels",
            iteration=1,
            user_input=UserContent(parts=[_image(1, byte_count=1, width=10, height=8)]),
        )


def _detailed_session(settings: RickySettings) -> AgentSession:
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
    )
    session.history = [
        Message(role="user", content=[TextPart(text="prior question")]),
        Message(
            role="assistant",
            content=[
                ThinkingPart(text="private reasoning"),
                TextPart(text="prior answer"),
                ToolCallPart(id="call_1", name="read_file", args={"path": "README.md"}),
            ],
        ),
        Message(
            role="tool",
            content=[ToolResultPart(call_id="call_1", content="file contents")],
        ),
    ]
    session.active_skill = ActiveSkill(
        name="review",
        profile="personal",
        description="Review carefully",
        body="Check every claim.",
        source_path="/tmp/review/SKILL.md",
    )
    return session


def test_complete_request_accounting_is_deterministic_and_non_overlapping() -> None:
    settings = RickySettings()
    session = _detailed_session(settings)
    registry = ToolRegistry(builtin_tools())

    first = assemble_context(
        session,
        registry,
        turn_id="turn_1",
        iteration=1,
        user_input="new question",
        memory=_Memory(),  # type: ignore[arg-type]
        extra_system_sections={"job briefing": "bounded job context"},
    )
    second = assemble_context(
        session,
        registry,
        turn_id="turn_1",
        iteration=1,
        user_input="new question",
        memory=_Memory(),  # type: ignore[arg-type]
        extra_system_sections={"job briefing": "bounded job context"},
    )

    assert first.request.model_dump_json() == second.request.model_dump_json()
    assert first.report == second.report
    assert first.report.serialized_chars == len(serialize_canonical_request(first.request))
    assert sum(section.chars for section in first.report.sections) == first.report.serialized_chars

    sections = {section.name: section for section in first.report.sections}
    for name in (
        "conversation_text",
        "assistant_thinking",
        "tool_calls",
        "tool_results",
        "memory_index",
        "active_skill",
        "job briefing",
        "pending_user_input",
        "advertised_tool_definitions",
        "canonical_request_envelope_overhead",
    ):
        assert sections[name].chars > 0
    assert sections["advertised_tool_definitions"].item_count == len(registry.specs())
    assert sections["canonical_request_envelope_overhead"].chars > 0
    assert sections["memory_index"].item_count == 1
    assert sections["active_skill"].item_count == 1
    assert sections["job briefing"].item_count == 1


def test_known_and_unknown_model_capacity_are_reported_honestly() -> None:
    known = RickySettings.model_validate(
        {
            "context": {
                "chars_per_token": 4,
                "response_reserve_tokens": 500,
                "safety_margin_tokens": 100,
                "models": [
                    {
                        "provider": "openrouter",
                        "model": "known-model",
                        "context_window_tokens": 10_000,
                        "max_output_tokens": 300,
                        "image_token_estimate": 2_048,
                    }
                ],
            }
        }
    )
    known_session = AgentSession.create(
        known,
        profile_scope=known.resolve_profile_scope(),
        model="known-model",
    )
    known_report = assemble_context(
        known_session,
        ToolRegistry([]),
        turn_id="turn",
        iteration=1,
        user_input="hello",
    ).report

    assert known_session.model_context.source == "configured"
    assert known_session.model_context.image_token_estimate == 2_048
    assert known_report.budget.output_reserve_tokens == 300
    assert known_report.budget.hard_input_tokens == 9_600
    assert known_report.budget.remaining_tokens == (9_600 - known_report.estimated_input_tokens)

    explicit = assemble_context(
        known_session,
        ToolRegistry([]),
        turn_id="turn",
        iteration=1,
        user_input="hello",
        max_completion_tokens=700,
    ).report
    assert explicit.budget.output_reserve_tokens == 700
    assert explicit.budget.hard_input_tokens == 9_200

    unknown_report = assemble_context(
        AgentSession.create(
            RickySettings(),
            profile_scope=RickySettings().resolve_profile_scope(),
            model="unknown-model",
        ),
        ToolRegistry([]),
        turn_id="turn",
        iteration=1,
        user_input="hello",
    ).report
    assert unknown_report.budget.capacity_source == "unknown"
    assert unknown_report.budget.context_window_tokens is None
    assert unknown_report.budget.hard_input_tokens is None
    assert unknown_report.budget.remaining_tokens is None
    assert unknown_report.estimated_input_tokens > 0


def test_configured_capacity_wins_over_an_available_catalog_snapshot() -> None:
    settings = RickySettings.model_validate(
        {
            "context": {
                "models": [
                    {
                        "provider": "openrouter",
                        "model": "model-a",
                        "context_window_tokens": 10_000,
                    }
                ]
            }
        }
    )
    catalog = ModelInfo(id="model-a", context_length=20_000, max_output_tokens=2_000)

    configured = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        model="model-a",
        model_info=catalog,
    )
    defaults = RickySettings()
    catalog_only = AgentSession.create(
        defaults,
        profile_scope=defaults.resolve_profile_scope(),
        model="model-a",
        model_info=catalog,
    )

    assert (configured.model_context.source, configured.model_context.context_window_tokens) == (
        "configured",
        10_000,
    )
    catalog_snapshot = (
        catalog_only.model_context.source,
        catalog_only.model_context.context_window_tokens,
    )
    assert catalog_snapshot == (
        "catalog",
        20_000,
    )


def test_session_and_expanded_context_event_round_trip_json() -> None:
    settings = RickySettings.model_validate(
        {
            "context": {
                "models": [
                    {
                        "provider": "openrouter",
                        "model": "round-trip",
                        "context_window_tokens": 32_000,
                    }
                ]
            }
        }
    )
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        model="round-trip",
    )
    assembly = assemble_context(
        session,
        ToolRegistry([]),
        turn_id="turn",
        iteration=1,
        user_input="hello",
    )
    adapter = TypeAdapter(AgentEvent)

    assert AgentSession.model_validate_json(session.model_dump_json()) == session
    restored = adapter.validate_json(adapter.dump_json(assembly.event))
    assert isinstance(restored, ContextAssembledEvent)
    assert restored.report == assembly.report


async def test_context_command_is_read_only_and_renders_without_debug(tmp_path: Path) -> None:
    settings = RickySettings()
    session = _detailed_session(settings)
    provider = _NoCallProvider()
    loop = AgentLoop(
        provider=provider,  # type: ignore[arg-type]
        registry=ToolRegistry([]),
        settings=settings,
        cwd=tmp_path,
    )
    output = StringIO()
    renderer = CliRenderer(
        console=Console(file=output, force_terminal=False, color_system=None, width=140)
    )
    controller = ChatController(
        agent_loop=loop,
        session=session,
        settings=settings,
        renderer=renderer,
        skill_registry=None,  # type: ignore[arg-type]
    )
    before = session.model_dump_json()

    handled = await controller._handle_slash_command("/context")

    rendered = output.getvalue()
    assert handled is True
    assert provider.requests == 0
    assert session.model_dump_json() == before
    assert renderer.debug is False
    assert "Context" in rendered
    assert "Total" in rendered
    assert "excludes your next user message" in rendered
    assert "unknown" in rendered


def test_actual_context_event_is_quiet_normally_and_complete_in_debug() -> None:
    assembly = assemble_context(
        AgentSession.create(
            RickySettings(),
            profile_scope=RickySettings().resolve_profile_scope(),
        ),
        ToolRegistry([]),
        turn_id="turn",
        iteration=1,
        user_input="hello",
    )
    normal_output = StringIO()
    debug_output = StringIO()
    normal = CliRenderer(
        console=Console(file=normal_output, force_terminal=False, color_system=None)
    )
    debug = CliRenderer(
        console=Console(file=debug_output, force_terminal=False, color_system=None),
        debug=True,
    )

    normal.render_event(assembly.event)
    debug.render_event(assembly.event)

    assert normal_output.getvalue() == ""
    rendered = debug_output.getvalue()
    assert "Context assembled" in rendered
    assert "advertised_tool_definitions" in rendered
    assert "Total" in rendered
    assert "response reserve" in rendered
