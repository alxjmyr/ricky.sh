"""Cacheable request prefixes without stale context or lost provider deltas."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ricky.agent import AgentSession, assemble_context
from ricky.config import RickySettings
from ricky.llm import CompletionRequest, Message, TextPart, ToolCallPart, ToolResultPart
from ricky.llm.anthropic import to_anthropic_request
from ricky.llm.claude_code import (
    _conversation_digest,
    to_claude_delta_prompt,
    to_claude_prompt,
)
from ricky.llm.openrouter import to_openrouter_request
from ricky.skills.spec import ActiveSkill
from ricky.tools import ToolRegistry

FIRST = datetime(2026, 8, 13, 20, 42, 17, tzinfo=UTC)
SECOND = datetime(2026, 8, 13, 20, 43, 18, tzinfo=UTC)


def _session() -> AgentSession:
    settings = RickySettings(user_timezone="America/Chicago")
    return AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())


@pytest.mark.parametrize("translate", [to_openrouter_request, to_anthropic_request])
def test_wire_prefix_survives_clock_activity_and_history_growth(translate) -> None:
    session = _session()
    registry = ToolRegistry([])
    first = assemble_context(
        session,
        registry,
        turn_id="first",
        iteration=1,
        user_input="inspect",
        extra_system_sections={"gateway": "Delegate background work; require approval."},
        extra_context_sections={"gateway_activity": "task revision 1"},
        now=FIRST,
    )
    session.history.extend(
        [
            Message.text("user", "inspect"),
            Message(role="assistant", content=[ToolCallPart(id="a", name="read", args={})]),
            Message(role="tool", content=[ToolResultPart(call_id="a", content="observed result")]),
        ]
    )
    canonical_history = session.model_dump_json()
    second = assemble_context(
        session,
        registry,
        turn_id="first",
        iteration=2,
        extra_system_sections={"gateway": "Delegate background work; require approval."},
        extra_context_sections={"gateway_activity": "task revision 2"},
        now=SECOND,
    )
    before = translate(first.request)
    after = translate(second.request)
    stable_prefix = before["messages"][:-1]
    assert after["messages"][: len(stable_prefix)] == stable_prefix
    assert after.get("system") == before.get("system")
    assert "Current datetime:" not in str(after.get("system", ""))
    assert all(
        "Current datetime:" not in str(message)
        for message in after["messages"]
        if message["role"] == "system"
    )
    assert "20:43:18Z" in str(after["messages"][-1])
    assert "15:43:18-05:00" in str(after["messages"][-1])
    assert "task revision 2" in str(after["messages"][-1])
    assert "task revision 1" not in str(after)
    assert "observed result" in str(after["messages"][-2])
    assert session.model_dump_json() == canonical_history
    assert first.request.session_id == second.request.session_id == session.id
    assert (
        sum(section.chars for section in second.report.sections) == second.report.serialized_chars
    )
    assert any(
        section.name == "gateway_activity" and section.chars for section in second.report.sections
    )


def test_context_refreshes_memory_skill_persona_and_policy_without_mutating_history(
    tmp_path: Path,
) -> None:
    class Memory:
        index_char_limit = 1_000
        text = "memory version one"

        def render_index(self, _limit: int) -> str:
            return self.text

    session = _session()
    session.history = [Message.text("user", "earlier"), Message.text("assistant", "answer")]
    history = list(session.history)
    soul = tmp_path / "SOUL.md"
    soul.write_text("persona one")
    session.settings_snapshot["profile_data_roots"] = {"shared": str(tmp_path)}
    memory = Memory()
    session.active_skill = ActiveSkill(
        name="review",
        profile="shared",
        description="Review",
        body="skill one",
        source_path=str(tmp_path / "SKILL.md"),
    )

    def request(policy: str) -> CompletionRequest:
        return assemble_context(
            session,
            ToolRegistry([]),
            turn_id="turn",
            iteration=1,
            memory=memory,  # type: ignore[arg-type]
            extra_system_sections={"approval_mode": policy},
            now=FIRST,
        ).request

    first = request("approval required")
    memory.text = "memory version two"
    soul.write_text("persona two")
    session.active_skill = session.active_skill.model_copy(update={"body": "skill two"})
    second = request("send-it active; denials still apply")
    system = "\n".join(
        part.text
        for message in second.messages
        if message.role == "system"
        for part in message.content
        if isinstance(part, TextPart)
    )
    for text in ("memory version two", "persona two", "skill two", "denials still apply"):
        assert text in system
    for text in ("memory version one", "persona one", "skill one", "approval required"):
        assert text not in system
    assert first.messages != second.messages
    assert second.messages[-2:] == history
    assert session.history == history
    session.active_skill = None
    assert "skill two" not in request("approval required").model_dump_json()


@pytest.mark.parametrize("repeated", [False, True])
def test_claude_resume_keeps_tool_results_or_empty_recovery_with_fresh_clock(
    repeated: bool,
) -> None:
    request = CompletionRequest(
        model="sonnet",
        session_id="session_cache",
        messages=[
            Message.text("system", "Follow permissions."),
            Message.text("user", "inspect the unique fixture"),
            Message(role="assistant", content=[ToolCallPart(id="a", name="read", args={})]),
            Message(role="tool", content=[ToolResultPart(call_id="a", content="first result")]),
            Message(role="tool", content=[ToolResultPart(call_id="b", content="second result")]),
        ],
        runtime_context=[TextPart(text="Current datetime: first")],
    )
    changed = request.model_copy(
        update={"runtime_context": [TextPart(text="Current datetime: next")]}
    )
    assert _conversation_digest(request) == _conversation_digest(changed)
    first_system, full = to_claude_prompt(request)
    system, delta = to_claude_delta_prompt(changed, repeated_conversation=repeated)
    assert system == first_system == "Follow permissions."
    assert "inspect the unique fixture" in full
    assert "first result" in full and "second result" in full
    assert full.endswith("Current datetime: first")
    assert "inspect the unique fixture" not in delta
    assert delta.endswith("Current datetime: next")
    if repeated:
        assert "provide a non-empty final answer" in delta
        assert "first result" not in delta
    else:
        assert "first result" in delta and "second result" in delta
        assert "Inspect the latest tool result(s)" in delta


def test_claude_new_user_delta_and_affinity_survive_clock_changes() -> None:
    request = CompletionRequest(
        model="sonnet",
        session_id="session_one",
        messages=[
            Message.text("user", "old"),
            Message.text("assistant", "answer"),
            Message.text("user", "new"),
        ],
        runtime_context=[TextPart(text="current data")],
    )
    _, delta = to_claude_delta_prompt(request)
    assert delta == "new\n\ncurrent data"
    first = to_openrouter_request(request)
    changed = request.model_copy(update={"model": "another-model"})
    assert to_openrouter_request(changed)["model"] == "another-model"
    assert to_openrouter_request(changed)["session_id"] == first["session_id"]
    fresh = request.model_copy(update={"session_id": "session_two"})
    assert to_openrouter_request(fresh)["session_id"] != first["session_id"]
    assert "provider" not in first  # No endpoint pinning or failover override.
    anonymous = request.model_copy(update={"session_id": None})
    assert "session_id" not in to_openrouter_request(anonymous)
