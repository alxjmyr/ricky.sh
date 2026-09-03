"""Workflow run records and pure executor tests."""

from __future__ import annotations

from pathlib import Path

from ricky.profiles import ProfileResourceRef, ProfileScope
from ricky.workflows.operators import default_operator_registry
from ricky.workflows.pure import (
    execute_check_step,
    execute_data_step,
    execute_message_step,
)
from ricky.workflows.run import (
    EffectJournalEntry,
    ItemRunRecord,
    StepRecord,
    WorkflowError,
    WorkflowRun,
    WorkflowSourceIdentity,
)
from ricky.workflows.spec import CheckStep, DataStep, MessageStep


def test_run_models_survive_json_round_trip_with_namespaced_item_records() -> None:
    item = ItemRunRecord(
        foreach_step_id="each",
        key="item-1",
        index=0,
        source={"id": "item-1"},
        steps={
            "note": StepRecord(
                step_id="note",
                execution_address="each/item-1/note",
                kind="message",
                status="completed",
                output={"text": "done"},
            )
        },
    )
    run = WorkflowRun(
        profile_scope=ProfileScope.create("personal"),
        workflow_name="probe",
        source=WorkflowSourceIdentity(
            resource=ProfileResourceRef(profile="personal", name="fixture"),
            path="fixture/probe",
            scope="fixture",
            content_digest="abc",
        ),
        provider="fake",
        model="fake-model",
        trigger={"limit": 2},
        graph_fingerprint="fingerprint",
        steps={
            "each": StepRecord(
                step_id="each",
                execution_address="each",
                kind="foreach",
                status="completed",
            )
        },
        item_runs={"each": [item]},
        effect_journal=[
            EffectJournalEntry(
                step_id="effect",
                execution_address="effect",
                tool_name="fake_effect",
                normalized_args={"id": "item-1"},
                risk="mutating",
            )
        ],
    )

    restored = WorkflowRun.model_validate_json(run.model_dump_json())

    assert restored == run
    assert restored.item_runs["each"][0].steps["note"].output == {"text": "done"}


def test_workflow_error_is_typed_and_json_safe() -> None:
    error = WorkflowError(
        category="invalid_output",
        message="missing field",
        retryable=True,
        detail="$.category",
    )

    assert WorkflowError.model_validate_json(error.model_dump_json()) == error


async def test_pure_data_check_and_message_steps_use_explicit_context(tmp_path: Path) -> None:
    context = {
        "trigger": {"enabled": True},
        "steps": {
            "left": {"status": "completed", "output": {"a": 1}},
            "right": {"status": "completed", "output": {"b": 2}},
        },
    }
    data = DataStep.model_validate(
        {
            "id": "merge",
            "kind": "data",
            "operator": "merge",
            "args": {
                "values": [
                    {"ref": "steps.left.output"},
                    {"ref": "steps.right.output"},
                ],
                "mode": "objects",
            },
        }
    )
    check = CheckStep.model_validate(
        {
            "id": "check",
            "kind": "check",
            "check": {"ref": "trigger.enabled", "is_true": True},
        }
    )
    message = MessageStep.model_validate(
        {
            "id": "report",
            "kind": "message",
            "message": {
                "format": "result {value}",
                "values": {"value": {"ref": "steps.left.output.a"}},
            },
        }
    )

    assert execute_data_step(data, context, default_operator_registry()) == {
        "value": {"a": 1, "b": 2}
    }
    assert await execute_check_step(
        check,
        context,
        cwd=tmp_path,
        default_timeout_seconds=1,
    ) == {"passed": True, "detail": "condition trigger.enabled is_true: True"}
    assert execute_message_step(message, context) == {"text": "result 1"}


async def test_shell_check_returns_typed_result(tmp_path: Path) -> None:
    step = CheckStep.model_validate(
        {
            "id": "shell",
            "kind": "check",
            "check": {"kind": "shell", "command": "exit 3", "expect_exit": 3},
        }
    )

    result = await execute_check_step(
        step,
        {},
        cwd=tmp_path,
        default_timeout_seconds=1,
    )

    assert result == {"passed": True, "detail": "shell exit 3; expected 3"}
