"""Workflow result schemas and strict JSON value validation."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

SchemaType = Literal["object", "array", "string", "integer", "number", "boolean"]


class ResultSchema(BaseModel):
    """The Ricky-owned recursive subset used for model step results."""

    model_config = ConfigDict(extra="forbid")

    type: SchemaType
    nullable: bool = False
    required: list[str] = Field(default_factory=list)
    properties: dict[str, ResultSchema] = Field(default_factory=dict)
    items: ResultSchema | None = None
    values: list[str] | None = None
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = Field(default=None, ge=0)
    max_length: int | None = Field(default=None, ge=0)
    extra: Literal["forbid"] = "forbid"

    @model_validator(mode="after")
    def _validate_shape(self) -> ResultSchema:
        if self.type == "object":
            unknown = sorted(set(self.required) - set(self.properties))
            if unknown:
                raise ValueError("required property names are not declared: " + ", ".join(unknown))
        elif self.required or self.properties:
            raise ValueError("required and properties are valid only for object schemas")

        if self.type == "array" and self.items is None:
            raise ValueError("an array schema requires items")
        if self.type != "array" and self.items is not None:
            raise ValueError("items is valid only for array schemas")

        if self.values is not None and self.type != "string":
            raise ValueError("values is valid only for string schemas")
        if self.values is not None and len(set(self.values)) != len(self.values):
            raise ValueError("schema values must be unique")

        if (self.minimum is not None or self.maximum is not None) and self.type not in {
            "integer",
            "number",
        }:
            raise ValueError("minimum and maximum are valid only for numeric schemas")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum cannot exceed maximum")

        if (self.min_length is not None or self.max_length is not None) and self.type not in {
            "string",
            "array",
        }:
            raise ValueError("length constraints are valid only for string and array schemas")
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("min_length cannot exceed max_length")
        return self


ResultSchema.model_rebuild()


class SchemaValueError(ValueError):
    """A result did not match its declared Ricky schema."""


def validate_schema_depth(schema: ResultSchema, *, max_depth: int) -> None:
    """Reject a schema that exceeds the configured recursive depth."""

    def visit(current: ResultSchema, depth: int) -> None:
        if depth > max_depth:
            raise ValueError(f"result schema depth exceeds configured maximum {max_depth}")
        for child in current.properties.values():
            visit(child, depth + 1)
        if current.items is not None:
            visit(current.items, depth + 1)

    visit(schema, 1)


def validate_result(schema: ResultSchema, value: Any) -> JsonValue:
    """Validate one value and return a JSON-round-trip-safe copy."""

    _validate_node(schema, value, path="$")
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise SchemaValueError(f"$: value is not JSON-safe: {exc}") from exc


def _validate_node(schema: ResultSchema, value: Any, *, path: str) -> None:
    if value is None:
        if schema.nullable:
            return
        raise SchemaValueError(f"{path}: null is not allowed")

    if schema.type == "object":
        if not isinstance(value, dict):
            raise SchemaValueError(f"{path}: expected object")
        non_string_keys = [key for key in value if not isinstance(key, str)]
        if non_string_keys:
            raise SchemaValueError(f"{path}: object keys must be strings")
        missing = [name for name in schema.required if name not in value]
        if missing:
            raise SchemaValueError(f"{path}: missing required field(s): {', '.join(missing)}")
        extras = sorted(set(value) - set(schema.properties))
        if extras:
            raise SchemaValueError(f"{path}: extra field(s) are forbidden: {', '.join(extras)}")
        for name, child in schema.properties.items():
            if name in value:
                _validate_node(child, value[name], path=f"{path}.{name}")
        return

    if schema.type == "array":
        if not isinstance(value, list):
            raise SchemaValueError(f"{path}: expected array")
        _check_length(schema, value, path=path)
        assert schema.items is not None
        for index, item in enumerate(value):
            _validate_node(schema.items, item, path=f"{path}[{index}]")
        return

    if schema.type == "string":
        if not isinstance(value, str):
            raise SchemaValueError(f"{path}: expected string")
        _check_length(schema, value, path=path)
        if schema.values is not None and value not in schema.values:
            allowed = ", ".join(repr(item) for item in schema.values)
            raise SchemaValueError(f"{path}: expected one of {allowed}")
        return

    if schema.type == "boolean":
        if not isinstance(value, bool):
            raise SchemaValueError(f"{path}: expected boolean")
        return

    if schema.type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SchemaValueError(f"{path}: expected integer")
        _check_number(schema, value, path=path)
        return

    if schema.type == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise SchemaValueError(f"{path}: expected number")
        _check_number(schema, value, path=path)
        return

    raise SchemaValueError(f"{path}: unsupported schema type {schema.type!r}")


def _check_length(schema: ResultSchema, value: str | list[Any], *, path: str) -> None:
    if schema.min_length is not None and len(value) < schema.min_length:
        raise SchemaValueError(f"{path}: length is less than {schema.min_length}")
    if schema.max_length is not None and len(value) > schema.max_length:
        raise SchemaValueError(f"{path}: length exceeds {schema.max_length}")


def _check_number(schema: ResultSchema, value: int | float, *, path: str) -> None:
    if schema.minimum is not None and value < schema.minimum:
        raise SchemaValueError(f"{path}: value is less than {schema.minimum}")
    if schema.maximum is not None and value > schema.maximum:
        raise SchemaValueError(f"{path}: value exceeds {schema.maximum}")


def schema_type_at_path(schema: ResultSchema, path: list[str]) -> ResultSchema | None:
    """Return the declared schema at one object-property path."""

    current = schema
    for part in path:
        if current.type != "object":
            return None
        current = current.properties.get(part)
        if current is None:
            return None
    return current
