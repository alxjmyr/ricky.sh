"""Regression tests for Ricky's system-level operating guidance."""

from ricky.agent.prompts import SYSTEM_PROMPT_V1


def test_system_prompt_requires_persistence_and_verified_completion() -> None:
    assert "Continue until the user's requested outcome is complete" in SYSTEM_PROMPT_V1
    assert "Do not end a turn with a promise of future work" in SYSTEM_PROMPT_V1
    assert "If work remains, perform the next" in SYSTEM_PROMPT_V1
    assert "action in this turn" in SYSTEM_PROMPT_V1
    assert "verify the requested outcome" in SYSTEM_PROMPT_V1


def test_system_prompt_keeps_multi_step_tasks_open_until_done_or_blocked() -> None:
    assert "create and maintain a task list with update_tasks" in SYSTEM_PROMPT_V1
    assert "exactly one task in progress" in SYSTEM_PROMPT_V1
    assert "include requested validation" in SYSTEM_PROMPT_V1
    assert "unless you are reporting a blocker" in SYSTEM_PROMPT_V1
