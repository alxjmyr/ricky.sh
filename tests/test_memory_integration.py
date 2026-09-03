"""Memory context, loop, session, and CLI integration tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ricky.agent import AgentLoop, AgentSession, assemble_context
from ricky.agent.events import AgentEvent, PermissionRequestedEvent
from ricky.config import RickySettings
from ricky.llm import (
    CompletionRequest,
    Message,
    MessageDone,
    StreamEvent,
    TextPart,
    ToolCallPart,
    Usage,
)
from ricky.memory import MemoryStore, memory_tools
from ricky.memory.types import MemoryNoteInput
from ricky.permissions import PermissionResponse
from ricky.tools import ToolRegistry


class FakeProvider:
    name = "fake"

    def __init__(self, scripts: list[list[StreamEvent | BaseException]]) -> None:
        self.scripts = scripts
        self.requests: list[CompletionRequest] = []

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        for event in self.scripts.pop(0):
            if isinstance(event, BaseException):
                raise event
            yield event

    async def aclose(self) -> None:
        pass


def _tool_message(call_id: str, name: str, args: dict[str, object]) -> MessageDone:
    return MessageDone(
        message=Message(
            role="assistant",
            content=[ToolCallPart(id=call_id, name=name, args=args)],
        ),
        usage=Usage(prompt_tokens=1, completion_tokens=1),
        stop_reason="tool_calls",
    )


def _final_message(text: str) -> MessageDone:
    return MessageDone(
        message=Message(role="assistant", content=[TextPart(text=text)]),
        usage=Usage(prompt_tokens=1, completion_tokens=1),
        stop_reason="stop",
    )


async def _events(loop: AgentLoop, session: AgentSession, prompt: str) -> list[AgentEvent]:
    return [event async for event in loop.run_turn(session, prompt)]


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings(user_data_dir=str(tmp_path / "global"))


def test_assembler_injects_memory_index_only_when_nonempty(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    profile_scope = settings.resolve_profile_scope("personal")
    session = AgentSession.create(settings, profile_scope=profile_scope)
    empty = MemoryStore.create(settings, scope=profile_scope)
    registry = ToolRegistry(memory_tools(empty))

    without_notes = assemble_context(
        session,
        registry,
        turn_id="turn_empty",
        iteration=1,
        memory=empty,
    )
    empty_sections = [section.name for section in without_notes.event.sections]
    assert "memory" not in empty_sections

    empty.write(
        MemoryNoteInput(
            slug="preferences",
            title="Preferences",
            profile="shared",
            summary="Alex prefers terse answers",
            body="Prefer concise answers.",
        )
    )
    with_notes = assemble_context(
        session,
        registry,
        turn_id="turn_memory",
        iteration=1,
        memory=empty,
    )

    assert [section.name for section in with_notes.event.sections] == [
        "system",
        "history",
        "memory",
    ]
    request_text = with_notes.request.model_dump_json()
    assert "shared/preferences" in request_text
    assert "Prefer concise answers." not in request_text


@pytest.mark.asyncio
async def test_normalized_default_profile_grant_covers_explicit_same_profile(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    profile_scope = settings.resolve_profile_scope("personal")
    session = AgentSession.create(
        settings,
        profile_scope=profile_scope,
        provider="openrouter",
    )
    store = MemoryStore.create(settings, scope=session.profile_scope)
    registry = ToolRegistry(memory_tools(store))
    provider = FakeProvider(
        [
            [
                _tool_message(
                    "remember_one",
                    "remember",
                    {
                        "title": "Answer Style",
                        "summary": "Alex prefers terse answers",
                        "body": "Prefer concise answers.",
                    },
                )
            ],
            [
                _tool_message(
                    "remember_two",
                    "remember",
                    {
                        "slug": "answer-style",
                        "title": "Answer Style",
                        "summary": "Alex strongly prefers terse answers",
                        "body": "Use short answers by default.",
                        "profile": "personal",
                    },
                )
            ],
            [_final_message("remembered")],
        ]
    )
    permission_events: list[PermissionRequestedEvent] = []

    async def responder(event: PermissionRequestedEvent) -> PermissionResponse:
        permission_events.append(event)
        return PermissionResponse(decision="allow", grant="scoped")

    loop = AgentLoop(
        provider=provider,
        registry=registry,
        settings=settings,
        permission_responder=responder,
        memory=store,
        cwd=tmp_path,
    )

    await _events(loop, session, "remember my preference")

    assert len(permission_events) == 1
    assert permission_events[0].args["profile"] == "personal"
    assert permission_events[0].args["slug"] == "answer-style"
    assert session.permission_grants[0].params_equal == {"profile": "personal"}
    note = store.get(profile="personal", slug="answer-style")
    assert note is not None
    assert note.body == "Use short answers by default."


@pytest.mark.asyncio
async def test_personal_scope_cannot_recall_or_send_work_note(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    work_scope = settings.resolve_profile_scope("work")
    work = MemoryStore.create(settings, scope=work_scope)
    work.write(
        MemoryNoteInput(
            slug="acme-renewal",
            title="Acme Renewal",
            type="account",
            profile="work",
            summary="Confidential renewal",
            body="WORK_SECRET_OWNER_IS_JANE",
        )
    )
    personal_scope = settings.resolve_profile_scope("personal")
    personal = MemoryStore.create(settings, scope=personal_scope)
    session = AgentSession.create(
        settings,
        profile_scope=personal_scope,
        provider="openrouter",
    )
    provider = FakeProvider(
        [
            [_tool_message("recall_work", "recall", {"slugs": ["acme-renewal"]})],
            [_final_message("No memory found.")],
        ]
    )
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(memory_tools(personal)),
        settings=settings,
        memory=personal,
        cwd=tmp_path,
    )

    await _events(loop, session, "Who owns the Acme renewal?")

    serialized_requests = "\n".join(request.model_dump_json() for request in provider.requests)
    assert "WORK_SECRET_OWNER_IS_JANE" not in serialized_requests
    assert "Confidential renewal" not in serialized_requests
    assert "[no matching notes]" in serialized_requests


@pytest.mark.asyncio
async def test_invalid_memory_args_are_model_readable_before_permission_processing(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    profile_scope = settings.resolve_profile_scope("personal")
    session = AgentSession.create(
        settings,
        profile_scope=profile_scope,
        provider="openrouter",
    )
    store = MemoryStore.create(settings, scope=session.profile_scope)
    provider = FakeProvider(
        [
            [
                _tool_message(
                    "invalid_remember",
                    "remember",
                    {"title": "Missing required fields"},
                )
            ],
            [_tool_message("invalid_forget", "forget", {})],
            [_final_message("corrected")],
        ]
    )
    permission_calls = 0

    async def responder(_event: PermissionRequestedEvent) -> PermissionResponse:
        nonlocal permission_calls
        permission_calls += 1
        return PermissionResponse(decision="allow")

    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(memory_tools(store)),
        settings=settings,
        permission_responder=responder,
        memory=store,
        cwd=tmp_path,
    )

    events = await _events(loop, session, "make malformed memory calls")

    assert permission_calls == 0
    assert "permission_requested" not in [event.kind for event in events]
    assert "agent_error" not in [event.kind for event in events]
    serialized_requests = "\n".join(request.model_dump_json() for request in provider.requests)
    assert "Invalid arguments for remember" in serialized_requests
    assert "Invalid arguments for forget" in serialized_requests


def test_session_carries_validated_profile_scope_and_profile_roots(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    scope = settings.resolve_profile_scope("personal", access_profiles=["work"])
    session = AgentSession.create(
        settings,
        profile_scope=scope,
        provider="claude_code",
    )

    assert session.profile_scope == scope
    assert session.profile_scope.profiles == ("shared", "personal", "work")
    roots = session.settings_snapshot["profile_data_roots"]
    assert isinstance(roots, dict)
    assert roots["personal"].endswith("/global/profiles/personal")
    assert roots["work"].endswith("/global/profiles/work")

    with pytest.raises(ValueError, match="unknown or disabled profile"):
        settings.resolve_profile_scope("future-profile")


def test_config_memory_is_read_only_and_shows_profile_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ricky.interfaces.cli.app import app

    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='probe'\n", encoding="utf-8")
    user_root = tmp_path / "memory-home"
    work_dir = user_root / "profiles" / "work" / "memory"
    work_dir.mkdir(parents=True)
    (work_dir / "account-acme.md").write_text("not parsed by count", encoding="utf-8")
    monkeypatch.chdir(project)
    monkeypatch.setenv("RICKY_USER_DATA_DIR", str(user_root))

    result = CliRunner().invoke(app, ["config", "memory"])

    assert result.exit_code == 0
    assert "work root" in result.stdout
    assert "work notes" in result.stdout
    assert not (work_dir / "INDEX.md").exists()
