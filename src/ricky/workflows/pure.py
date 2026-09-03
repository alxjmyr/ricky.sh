"""Deterministic Workflow condition, data, check, and message execution."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pydantic import BaseModel, JsonValue

from ricky.workflows.operators import DataOperatorRegistry
from ricky.workflows.spec import (
    CheckStep,
    Condition,
    DataStep,
    MessageStep,
    ShellCheck,
)
from ricky.workflows.values import resolve_mapping, resolve_reference, resolve_value


class CheckOutput(BaseModel):
    passed: bool
    detail: str


class MessageOutput(BaseModel):
    text: str


def evaluate_condition(condition: Condition, context: dict[str, Any]) -> bool:
    """Evaluate one pure condition after its dependencies are terminal."""

    actual = resolve_reference(condition.ref, context)
    if condition.operator == "equals":
        return actual == condition.value
    if condition.operator == "not_equals":
        return actual != condition.value
    if condition.operator == "in":
        assert isinstance(condition.value, list)
        return actual in condition.value
    if condition.operator == "exists":
        return actual is not None
    if condition.operator == "is_true":
        if not isinstance(actual, bool):
            raise ValueError(f"condition reference {condition.ref!r} is not boolean")
        return actual
    if condition.operator == "is_false":
        if not isinstance(actual, bool):
            raise ValueError(f"condition reference {condition.ref!r} is not boolean")
        return not actual
    raise ValueError(f"unknown condition operator: {condition.operator}")


def execute_data_step(
    step: DataStep,
    context: dict[str, Any],
    operators: DataOperatorRegistry,
) -> JsonValue:
    """Resolve typed args and execute one app-owned pure operator."""

    args = resolve_mapping(step.args, context)
    result = operators.run(step.operator, args)
    return result.model_dump(mode="json")


async def execute_check_step(
    step: CheckStep,
    context: dict[str, Any],
    *,
    cwd: Path,
    default_timeout_seconds: float,
) -> JsonValue:
    """Execute one deterministic predicate."""

    if isinstance(step.check, Condition):
        passed = evaluate_condition(step.check, context)
        detail = f"condition {step.check.ref} {step.check.operator}: {passed}"
        return CheckOutput(passed=passed, detail=detail).model_dump(mode="json")
    passed, detail = await _execute_shell(
        step.check,
        cwd=cwd,
        default_timeout_seconds=default_timeout_seconds,
    )
    return CheckOutput(passed=passed, detail=detail).model_dump(mode="json")


def execute_message_step(step: MessageStep, context: dict[str, Any]) -> JsonValue:
    """Render one user-facing message without a model or external effect."""

    value = resolve_value(step.message, context)
    if not isinstance(value, str):
        raise ValueError("a message expression must resolve to a string")
    return MessageOutput(text=value).model_dump(mode="json")


async def _execute_shell(
    check: ShellCheck,
    *,
    cwd: Path,
    default_timeout_seconds: float,
) -> tuple[bool, str]:
    process = await asyncio.create_subprocess_shell(
        check.command,
        cwd=cwd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    timeout = check.timeout_seconds or default_timeout_seconds
    try:
        async with asyncio.timeout(timeout):
            exit_code = await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()
        return False, f"shell check timed out after {timeout:g}s"
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    return (
        exit_code == check.expect_exit,
        f"shell exit {exit_code}; expected {check.expect_exit}",
    )
