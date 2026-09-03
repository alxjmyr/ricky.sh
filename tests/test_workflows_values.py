"""Workflow value-expression tests."""

from __future__ import annotations

import pytest

from ricky.workflows.run import StepRecord, WorkflowError
from ricky.workflows.values import (
    ReferenceResolutionError,
    ValueExpr,
    iter_references,
    resolve_value,
)


def test_reference_preserves_json_value_types() -> None:
    context = {
        "trigger": {
            "object": {"a": 1},
            "list": [1, 2],
            "number": 3.5,
            "boolean": True,
            "string": "x",
        }
    }

    for name, expected in context["trigger"].items():
        value = ValueExpr.model_validate({"ref": f"trigger.{name}"})
        assert resolve_value(value, context) == expected


def test_literal_and_formatted_values_round_trip() -> None:
    literal = ValueExpr.model_validate({"nested": [1, False]})
    formatted = ValueExpr.model_validate(
        {
            "format": "{name}:{count}",
            "values": {
                "name": {"ref": "trigger.name"},
                "count": {"ref": "trigger.count"},
            },
        }
    )

    assert ValueExpr.model_validate_json(literal.model_dump_json()) == literal
    assert ValueExpr.model_validate_json(formatted.model_dump_json()) == formatted
    assert resolve_value(formatted, {"trigger": {"name": "jobs", "count": 2}}) == "jobs:2"
    assert list(iter_references(formatted)) == ["trigger.name", "trigger.count"]


def test_missing_reference_fails_closed() -> None:
    value = ValueExpr.model_validate({"ref": "steps.read.output.value"})

    with pytest.raises(ReferenceResolutionError, match="missing"):
        resolve_value(value, {"steps": {"read": {"status": "failed"}}})


def test_lazy_record_reference_serializes_only_the_selected_field() -> None:
    record = StepRecord(
        step_id="read",
        execution_address="read",
        kind="tool",
        status="failed",
        output={"large": "x" * 5_000},
        error=WorkflowError(category="tool_error", message="failed"),
    )

    status = resolve_value(
        ValueExpr.model_validate({"ref": "steps.read.status"}),
        {"steps": {"read": record}},
    )
    error = resolve_value(
        ValueExpr.model_validate({"ref": "steps.read.error"}),
        {"steps": {"read": record}},
    )

    assert status == "failed"
    assert error == {
        "category": "tool_error",
        "message": "failed",
        "retryable": False,
        "detail": None,
    }


def test_formatted_string_rejects_attribute_access_and_unused_values() -> None:
    with pytest.raises(ValueError, match="attribute access"):
        ValueExpr.model_validate(
            {"format": "{user.name}", "values": {"user": {"ref": "trigger.user"}}}
        )
    with pytest.raises(ValueError, match="unused"):
        ValueExpr.model_validate({"format": "hello", "values": {"x": 1}})
