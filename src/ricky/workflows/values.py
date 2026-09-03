"""Typed value expressions and fail-closed Workflow reference resolution."""

from __future__ import annotations

import json
import re
import string
from collections.abc import Iterator, Mapping
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    model_serializer,
    model_validator,
)

REFERENCE_PATTERN = re.compile(r"^(trigger|steps|item)(?:\.[a-zA-Z0-9_-]+)+$")
FORMAT_FIELD_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class LiteralExpr(BaseModel):
    """One JSON-safe literal."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["literal"] = "literal"
    value: JsonValue


class ReferenceExpr(BaseModel):
    """One typed workflow-state reference."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["reference"] = "reference"
    ref: str = Field(pattern=REFERENCE_PATTERN.pattern)


class FormatExpr(BaseModel):
    """One code-free named-field string format."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["format"] = "format"
    format: str
    values: dict[str, ValueExpr] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_fields(self) -> FormatExpr:
        fields: set[str] = set()
        try:
            parsed = string.Formatter().parse(self.format)
            for _, field_name, format_spec, conversion in parsed:
                if field_name is None:
                    continue
                if FORMAT_FIELD_PATTERN.fullmatch(field_name) is None:
                    raise ValueError(
                        "format fields must be simple named fields without attribute access"
                    )
                if format_spec or conversion:
                    raise ValueError("format specs and conversions are not supported")
                fields.add(field_name)
        except ValueError as exc:
            raise ValueError(f"invalid formatted string: {exc}") from exc
        missing = sorted(fields - set(self.values))
        unused = sorted(set(self.values) - fields)
        if missing:
            raise ValueError("formatted string is missing value(s): " + ", ".join(missing))
        if unused:
            raise ValueError("formatted string has unused value(s): " + ", ".join(unused))
        return self


class ListExpr(BaseModel):
    """A list whose members are value expressions."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["list"] = "list"
    values: list[ValueExpr]


class ObjectExpr(BaseModel):
    """An object whose values are value expressions."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["object"] = "object"
    values: dict[str, ValueExpr]


ValueNode = Annotated[
    LiteralExpr | ReferenceExpr | FormatExpr | ListExpr | ObjectExpr,
    Field(discriminator="kind"),
]


class ValueExpr(RootModel[ValueNode]):
    """A TOML literal, reference object, or formatted-string object."""

    @model_validator(mode="before")
    @classmethod
    def _from_author_value(cls, value: Any) -> Any:
        if isinstance(value, cls):
            return value.root
        if isinstance(value, dict) and "root" in value and len(value) == 1:
            value = value["root"]
        if isinstance(value, dict) and value.get("kind") in {
            "literal",
            "reference",
            "format",
            "list",
            "object",
        }:
            return value
        if isinstance(value, dict) and set(value) == {"ref"}:
            return {"kind": "reference", "ref": value["ref"]}
        if isinstance(value, dict) and set(value) == {"format", "values"}:
            return {
                "kind": "format",
                "format": value["format"],
                "values": value["values"],
            }
        if isinstance(value, list):
            return {"kind": "list", "values": value}
        if isinstance(value, dict):
            return {"kind": "object", "values": value}
        return {"kind": "literal", "value": value}

    @model_serializer(mode="plain")
    def _to_author_value(self) -> JsonValue | dict[str, Any]:
        node = self.root
        if isinstance(node, LiteralExpr):
            return node.value
        if isinstance(node, ReferenceExpr):
            return {"ref": node.ref}
        if isinstance(node, FormatExpr):
            return {
                "format": node.format,
                "values": {name: value.model_dump() for name, value in node.values.items()},
            }
        if isinstance(node, ListExpr):
            return [value.model_dump() for value in node.values]
        return {name: value.model_dump() for name, value in node.values.items()}


FormatExpr.model_rebuild()
ListExpr.model_rebuild()
ObjectExpr.model_rebuild()
ValueExpr.model_rebuild()


class ReferenceResolutionError(ValueError):
    """A declared reference was unavailable at runtime."""


def parse_reference(reference: str) -> list[str]:
    """Validate and split one reference path."""

    if REFERENCE_PATTERN.fullmatch(reference) is None:
        raise ValueError(f"invalid workflow reference: {reference!r}")
    return reference.split(".")


def iter_references(value: ValueExpr) -> Iterator[str]:
    """Yield every reference in one value expression."""

    node = value.root
    if isinstance(node, ReferenceExpr):
        yield node.ref
    elif isinstance(node, FormatExpr | ObjectExpr):
        for child in node.values.values():
            yield from iter_references(child)
    elif isinstance(node, ListExpr):
        for child in node.values:
            yield from iter_references(child)


def resolve_value(value: ValueExpr, context: Mapping[str, Any]) -> JsonValue:
    """Resolve one expression without coercing reference value types."""

    node = value.root
    if isinstance(node, LiteralExpr):
        return _json_copy(node.value)
    if isinstance(node, ReferenceExpr):
        return _json_copy(resolve_reference(node.ref, context))
    if isinstance(node, FormatExpr):
        rendered = {
            name: _format_value(resolve_value(child, context))
            for name, child in node.values.items()
        }
        return node.format.format_map(rendered)
    if isinstance(node, ListExpr):
        return [resolve_value(child, context) for child in node.values]
    return {name: resolve_value(child, context) for name, child in node.values.items()}


def resolve_mapping(
    values: Mapping[str, ValueExpr], context: Mapping[str, Any]
) -> dict[str, JsonValue]:
    """Resolve a named expression map."""

    return {name: resolve_value(value, context) for name, value in values.items()}


def resolve_reference(reference: str, context: Mapping[str, Any]) -> Any:
    """Resolve one path and fail closed on every missing segment."""

    parts = parse_reference(reference)
    current: Any = context
    traversed: list[str] = []
    for part in parts:
        traversed.append(part)
        if isinstance(current, BaseModel):
            if part not in type(current).model_fields:
                location = ".".join(traversed)
                raise ReferenceResolutionError(
                    f"reference {reference!r} is missing at {location!r}"
                )
            current = getattr(current, part)
            continue
        if not isinstance(current, Mapping) or part not in current:
            location = ".".join(traversed)
            raise ReferenceResolutionError(f"reference {reference!r} is missing at {location!r}")
        current = current[part]
    return current


def json_character_count(value: Any) -> int:
    """Return the stable compact JSON size of one value."""

    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _json_copy(value: Any) -> JsonValue:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ReferenceResolutionError(f"workflow value is not JSON-safe: {exc}") from exc


def _format_value(value: JsonValue) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
