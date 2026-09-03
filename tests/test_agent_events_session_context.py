"""Tests for agent events, session state, and context assembly."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr, TypeAdapter

from ricky.agent import AgentSession, ContextAssembledEvent, TextDeltaEvent, assemble_context
from ricky.agent.events import (
    AgentEvent,
    ToolCallNormalizedEvent,
    ToolCallRejectedEvent,
    UserInteractionRequiredEvent,
)
from ricky.agent.prompts import SYSTEM_PROMPT_V1
from ricky.config import (
    ContextSettings,
    GoogleAccountSettings,
    GoogleSettings,
    ModelContextProfile,
    ProfileConfigSettings,
    RickySettings,
)
from ricky.llm import TextPart
from ricky.tools import ToolRegistry, builtin_tools


def _session(
    settings: RickySettings | None = None,
    *,
    timezone: str | None = None,
) -> AgentSession:
    resolved = settings or RickySettings()
    return AgentSession.create(
        resolved,
        profile_scope=resolved.resolve_profile_scope(),
        timezone=timezone,
    )


def test_agent_event_union_round_trip_json() -> None:
    adapter = TypeAdapter(AgentEvent)
    events = (
        TextDeltaEvent(turn_id="turn_1", delta="hello"),
        UserInteractionRequiredEvent(
            turn_id="turn_1",
            interaction_kind="guardrail_input",
            correlation_id="draft_test:1",
            prompt="Which date?",
        ),
        ToolCallNormalizedEvent(
            turn_id="turn_1",
            call_id="call_1",
            tool_name="probe",
            paths=["attachments.0"],
        ),
        ToolCallRejectedEvent(
            turn_id="turn_1",
            call_id="call_2",
            tool_name="probe",
            reason="invalid_arguments",
            repairable=True,
            input_digest="a" * 64,
        ),
    )

    assert tuple(adapter.validate_json(adapter.dump_json(event)) for event in events) == events


def test_session_round_trip_excludes_secret_values() -> None:
    settings = RickySettings(openrouter_api_key=SecretStr("secret-value"))
    session = _session(settings)

    dumped = session.model_dump_json()
    restored = AgentSession.model_validate_json(dumped)

    assert restored == session
    assert "secret-value" not in dumped
    assert set(session.settings_snapshot["profile_data_roots"]) == {"shared", "personal"}
    assert set(session.settings_snapshot["profile_definitions"]) == {"shared", "personal"}


def test_context_assembly_is_deterministic() -> None:
    settings = RickySettings()
    session = _session(settings)
    registry = ToolRegistry(builtin_tools())

    first = assemble_context(
        session,
        registry,
        turn_id="turn_1",
        iteration=1,
        user_input="read README.md",
    )
    second = assemble_context(
        session,
        registry,
        turn_id="turn_1",
        iteration=1,
        user_input="read README.md",
    )

    assert first.request == second.request
    assert isinstance(first.event, ContextAssembledEvent)
    assert first.event.message_count == 2
    assert first.event.tool_count == len(registry.specs())
    assert [section.name for section in first.event.sections] == [
        "system",
        "history",
        "user_input",
    ]


def test_context_lists_only_scope_accessible_profile_qualified_google_accounts() -> None:
    settings = RickySettings(
        profile_configs={
            "personal": ProfileConfigSettings(
                google=GoogleSettings(
                    accounts={"personal": GoogleAccountSettings(email="alex@example.com")}
                )
            ),
            "work": ProfileConfigSettings(
                google=GoogleSettings(
                    accounts={"work": GoogleAccountSettings(email="alex@company.example")}
                )
            ),
        }
    )
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope("personal"),
    )

    assembly = assemble_context(
        session,
        ToolRegistry([]),
        turn_id="turn_1",
        iteration=1,
    )

    system_part = assembly.request.messages[0].content[0]
    assert isinstance(system_part, TextPart)
    assert session.settings_snapshot["google_accounts"] == {
        "personal/personal": {"email": "alex@example.com"}
    }
    assert "- personal/personal — alex@example.com" in system_part.text
    assert "work/work" not in system_part.text
    assert "do not shorten it to a local\n  account name" in system_part.text


def test_session_timezone_defaults_from_config_and_can_be_overridden() -> None:
    settings = RickySettings(user_timezone="America/Chicago")

    configured = _session(settings)
    overridden = _session(settings, timezone="Europe/Paris")

    assert configured.timezone == "America/Chicago"
    assert overridden.timezone == "Europe/Paris"
    assert AgentSession.model_validate_json(overridden.model_dump_json()) == overridden


def test_session_snapshot_uses_resolved_profile_runtime_settings(tmp_path: Path) -> None:
    settings = RickySettings(
        user_data_dir=str(tmp_path / "user-data"),
        project_data_dir=str(tmp_path / "project-data"),
        user_timezone="UTC",
        request_timeout_seconds=120,
        max_turn_iterations=25,
        context_char_limit=120_000,
        shell_timeout_seconds=90,
        context=ContextSettings(
            response_reserve_tokens=8_000,
            safety_margin_tokens=777,
            models=[
                ModelContextProfile(
                    provider="openrouter",
                    model="anthropic/claude-sonnet-4",
                    context_window_tokens=100_000,
                    max_output_tokens=4_000,
                )
            ],
        ),
        profile_configs={
            "personal": ProfileConfigSettings.model_validate(
                {
                    "user_timezone": "America/Chicago",
                    "request_timeout_seconds": 30,
                    "max_turn_iterations": 5,
                    "context_char_limit": 40_000,
                    "shell_timeout_seconds": 20,
                    "context": {
                        "response_reserve_tokens": 2_000,
                        "models": [
                            {
                                "provider": "openrouter",
                                "model": "anthropic/claude-sonnet-4",
                                "context_window_tokens": 50_000,
                                "max_output_tokens": 2_000,
                            }
                        ],
                    },
                }
            )
        },
    )

    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
    )
    restored = AgentSession.model_validate_json(session.model_dump_json())

    assert session.timezone == "America/Chicago"
    assert session.settings_snapshot["request_timeout_seconds"] == 30
    assert session.settings_snapshot["max_turn_iterations"] == 5
    assert session.settings_snapshot["context_char_limit"] == 40_000
    assert session.settings_snapshot["shell_timeout_seconds"] == 20
    assert session.settings_snapshot["context"]["response_reserve_tokens"] == 2_000
    assert session.settings_snapshot["context"]["safety_margin_tokens"] == 777
    assert session.model_context.context_window_tokens == 50_000
    assert session.model_context.max_output_tokens == 2_000
    assert restored == session
    assert not Path(settings.project_data_dir).exists()


def test_invalid_timezone_names_fail_validation() -> None:
    with pytest.raises(ValueError, match="unknown IANA timezone"):
        RickySettings(user_timezone="Central Time")

    with pytest.raises(ValueError, match="unknown IANA timezone"):
        _session(timezone="Central Time")


def test_context_includes_current_utc_and_session_local_datetime() -> None:
    session = _session(timezone="America/Chicago")
    now = datetime(2026, 8, 13, 20, 42, 17, tzinfo=UTC)

    assembly = assemble_context(
        session,
        ToolRegistry([]),
        turn_id="turn_1",
        iteration=1,
        now=now,
    )

    clock_part = assembly.request.messages[0].content[1]
    assert isinstance(clock_part, TextPart)
    assert "UTC: 2026-08-13T20:42:17Z" in clock_part.text
    assert "Session local: 2026-08-13T15:42:17-05:00" in clock_part.text
    assert "Session timezone: America/Chicago" in clock_part.text


def test_context_rejects_naive_datetime() -> None:
    session = _session()

    with pytest.raises(ValueError, match="context datetime must be timezone-aware"):
        assemble_context(
            session,
            ToolRegistry([]),
            turn_id="turn_1",
            iteration=1,
            now=datetime(2026, 8, 13, 20, 42, 17),
        )


def test_context_assembly_fails_loudly_past_configured_char_limit() -> None:
    settings = RickySettings(context_char_limit=10)
    session = _session(settings)
    registry = ToolRegistry(builtin_tools())

    with pytest.raises(ValueError, match="context character limit exceeded"):
        assemble_context(
            session,
            registry,
            turn_id="turn_1",
            iteration=1,
            user_input="x" * 50,
        )


def test_context_assembly_starts_with_shared_then_primary_soul_and_keeps_harness_prompt(
    tmp_path: Path,
) -> None:
    user_data_dir = tmp_path / "ricky-home"
    shared_dir = user_data_dir / "profiles" / "shared"
    personal_dir = user_data_dir / "profiles" / "personal"
    work_dir = user_data_dir / "profiles" / "work"
    for directory in (shared_dir, personal_dir, work_dir):
        directory.mkdir(parents=True)
    shared_soul = "You are Juniper, a meticulous research assistant."
    personal_soul = "Be candid in personal conversations."
    (shared_dir / "SOUL.md").write_text(shared_soul, encoding="utf-8")
    (personal_dir / "SOUL.md").write_text(personal_soul, encoding="utf-8")
    (work_dir / "SOUL.md").write_text("Work persona must stay out.", encoding="utf-8")
    settings = RickySettings(user_data_dir=str(user_data_dir))
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(access_profiles=["work"]),
    )
    assert set(session.settings_snapshot["profile_data_roots"]) == {
        "shared",
        "personal",
        "work",
    }

    assembly = assemble_context(
        session,
        ToolRegistry([]),
        turn_id="turn_1",
        iteration=1,
    )

    system_part = assembly.request.messages[0].content[0]
    assert isinstance(system_part, TextPart)
    system_text = system_part.text
    harness_prompt = SYSTEM_PROMPT_V1.format(
        environment=f"cwd: {Path.cwd().resolve()}",
        user_data_dir=str(user_data_dir.resolve()),
        profiles=(
            "- shared: Context and resources intentionally available in every Ricky session.\n"
            "  - Routing hint: Cross-context preferences and universally applicable user facts.\n"
            "- personal (primary; default for new data): The user's personal life, accounts, "
            "responsibilities, and preferences.\n"
            "  - Routing hint: Family, home, personal finance, health, and non-work commitments.\n"
            "- work: The user's employer and professional work context.\n"
            "  - Routing hint: Employer data, coworkers, work accounts, and professional tasks."
        ),
        resources="No profile-owned account resources are currently available.",
        skills="No skills are currently available.",
        workflows="No workflows are currently available.",
    ).lstrip("\n")
    assert system_text == f"{shared_soul}\n\n{personal_soul}\n\n{harness_prompt}"
    assert "Work persona must stay out." not in system_text
    assert "current user or project workspace" in system_text
    assert "not necessarily the\n  Ricky harness source" in system_text
    assert "Modify workspace files only when the user's request explicitly calls" in system_text
    assert "Never use cwd as Ricky-owned scratch space" in system_text
    assert f"under {user_data_dir.resolve()}/tmp/" in system_text
    assert "Never place temporary or ad hoc tools in <cwd>/scripts/" in system_text


def test_context_assembly_keeps_harness_prompt_when_soul_is_missing(tmp_path: Path) -> None:
    settings = RickySettings(user_data_dir=str(tmp_path / "missing-ricky-home"))
    session = _session(settings)

    assembly = assemble_context(
        session,
        ToolRegistry([]),
        turn_id="turn_1",
        iteration=1,
    )

    system_part = assembly.request.messages[0].content[0]
    assert isinstance(system_part, TextPart)
    system_text = system_part.text
    assert system_text.startswith("\nEnvironment:")
