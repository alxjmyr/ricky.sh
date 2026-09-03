"""Boundary-model and configuration tests for durable tasks."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ricky.agent.prompts import SYSTEM_PROMPT_V1
from ricky.agent.session import AgentSession
from ricky.config import DurableTaskSettings, RickySettings
from ricky.durable_tasks.types import DurableTask, TaskSearchQuery


def _task(**updates: object) -> DurableTask:
    values: dict[str, object] = {
        "id": "task_0123456789abcdef0123456789abcdef",
        "profile": "personal",
        "title": "Prepare follow-up",
        "objective": "Send a useful follow-up",
        "closure_criteria": "The message is sent",
        "execution_mode": "joint",
        "status": "open",
        "priority": 0,
        "revision": 1,
        "created_at": datetime(2026, 7, 25, tzinfo=UTC),
        "updated_at": datetime(2026, 7, 25, tzinfo=UTC),
    }
    values.update(updates)
    return DurableTask.model_validate(values)


def test_models_and_session_survive_json_round_trip() -> None:
    task = _task()
    query = TaskSearchQuery(text="follow-up", execution_modes=["joint"])
    session = AgentSession(
        provider="openrouter",
        model="synthetic",
        profile_scope=RickySettings().resolve_profile_scope(),
        active_task_leases={},
    )

    assert DurableTask.model_validate_json(task.model_dump_json()) == task
    assert TaskSearchQuery.model_validate_json(query.model_dump_json()) == query
    assert AgentSession.model_validate_json(session.model_dump_json()) == session


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"status": "waiting", "waiting_on": None}, "waiting"),
        ({"status": "waiting", "waiting_on": "user", "next_action": None}, "waiting"),
        ({"status": "open", "waiting_on": "user"}, "waiting_on"),
        ({"status": "completed"}, "completed"),
        ({"status": "cancelled"}, "cancelled"),
        ({"id": "task_readable-name"}, "id"),
        ({"created_at": datetime(2026, 7, 25)}, "timezone-aware"),
    ],
)
def test_task_state_invariants_fail_closed(updates: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _task(**updates)


def test_durable_task_settings_reject_unsafe_root() -> None:
    with pytest.raises(ValidationError, match="must stay below user_data_dir"):
        RickySettings(durable_tasks=DurableTaskSettings(dir="../outside"))


def test_prompt_keeps_durable_session_and_user_owned_tasks_distinct() -> None:
    assert "responsibility that must survive this agent session" in SYSTEM_PROMPT_V1
    assert "update_tasks list is only your internal plan" in SYSTEM_PROMPT_V1
    assert "User-owned tasks change only on Alex's direct instruction" in SYSTEM_PROMPT_V1
    assert "never infer their completion" in SYSTEM_PROMPT_V1
