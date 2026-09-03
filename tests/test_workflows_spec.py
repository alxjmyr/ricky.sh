"""Workflow authoring and recursive result-schema tests."""

from __future__ import annotations

import json

import pytest

from ricky.workflows.schema import ResultSchema, SchemaValueError, validate_result
from ricky.workflows.spec import (
    ForeachStep,
    WorkflowSpec,
    parse_workflow_toml,
    resolve_trigger_args,
)


def _base_spec() -> dict[str, object]:
    return {
        "version": 2,
        "name": "typed-probe",
        "description": "Exercise typed workflow authoring.",
        "args": {
            "limit": {
                "type": "integer",
                "description": "Maximum records.",
                "default": 10,
                "min": 1,
                "max": 100,
            },
            "enabled": {"type": "boolean", "description": "Run it."},
        },
        "schemas": {
            "classification": {
                "type": "object",
                "required": ["category", "metadata"],
                "properties": {
                    "category": {
                        "type": "string",
                        "values": ["action", "ignore"],
                    },
                    "metadata": {
                        "type": "object",
                        "required": ["score"],
                        "properties": {"score": {"type": "number", "minimum": 0}},
                    },
                },
            }
        },
        "steps": [
            {
                "id": "classify",
                "kind": "model",
                "instruction": "Classify the record.",
                "inputs": {"enabled": {"ref": "trigger.enabled"}},
                "result_schema": "classification",
            }
        ],
    }


def test_spec_and_schema_survive_json_round_trip() -> None:
    spec = WorkflowSpec.model_validate(_base_spec())

    restored = WorkflowSpec.model_validate_json(spec.model_dump_json(by_alias=True))

    assert restored == spec
    assert restored.args["limit"].required is False
    assert restored.args["enabled"].required is True


def test_typed_trigger_args_validate_without_coercion() -> None:
    spec = WorkflowSpec.model_validate(_base_spec())

    assert resolve_trigger_args(spec, {"enabled": True}) == {
        "limit": 10,
        "enabled": True,
    }
    with pytest.raises(ValueError, match="must be an integer"):
        resolve_trigger_args(spec, {"enabled": True, "limit": "10"})
    with pytest.raises(ValueError, match="missing required"):
        resolve_trigger_args(spec, {})
    with pytest.raises(ValueError, match="unknown arg"):
        resolve_trigger_args(spec, {"enabled": True, "other": 1})


def test_recursive_result_schema_rejects_missing_extra_enum_and_wrong_type() -> None:
    spec = WorkflowSpec.model_validate(_base_spec())
    schema = spec.schemas["classification"]
    valid = {"category": "action", "metadata": {"score": 0.8}}

    assert validate_result(schema, valid) == valid
    with pytest.raises(SchemaValueError, match="missing required"):
        validate_result(schema, {"category": "action"})
    with pytest.raises(SchemaValueError, match="extra field"):
        validate_result(schema, {**valid, "surprise": True})
    with pytest.raises(SchemaValueError, match="expected one of"):
        validate_result(schema, {**valid, "category": "later"})
    with pytest.raises(SchemaValueError, match="expected number"):
        validate_result(schema, {"category": "action", "metadata": {"score": "high"}})


def test_result_schema_model_rejects_invalid_kind_fields() -> None:
    with pytest.raises(ValueError, match="items is valid only"):
        ResultSchema.model_validate({"type": "string", "items": {"type": "string"}})
    with pytest.raises(ValueError, match="requires items"):
        ResultSchema.model_validate({"type": "array"})


def test_toml_parser_requires_version_two() -> None:
    text = """
version = 2
name = "probe"
description = "Probe."

[[steps]]
id = "done"
kind = "message"
message = "done"
"""
    assert parse_workflow_toml(text).name == "probe"
    with pytest.raises(ValueError, match="not version 2"):
        parse_workflow_toml(text.replace("version = 2", "version = 3"))


def test_foreach_body_is_a_recursive_typed_graph() -> None:
    raw = _base_spec()
    raw["steps"] = [
        {
            "id": "each",
            "kind": "foreach",
            "collection": [{"id": "a"}],
            "item_key": {"ref": "item.source.id"},
            "body": [
                {
                    "id": "note",
                    "kind": "message",
                    "message": {
                        "format": "item {id}",
                        "values": {"id": {"ref": "item.key"}},
                    },
                }
            ],
        }
    ]

    spec = WorkflowSpec.model_validate(raw)

    assert isinstance(spec.steps[0], ForeachStep)
    assert json.loads(spec.model_dump_json())["steps"][0]["item_key"] == {"ref": "item.source.id"}
