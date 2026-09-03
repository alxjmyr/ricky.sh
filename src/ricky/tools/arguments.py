"""Schema-directed normalization for provider-produced tool arguments."""

from __future__ import annotations

import json
import types
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic.fields import FieldInfo


@dataclass(frozen=True)
class NormalizedArguments:
    """One syntactically normalized argument object plus safe evidence."""

    args: dict[str, object]
    paths: tuple[str, ...]


def normalize_arguments(
    params_model: type[BaseModel],
    args: Mapping[str, object],
) -> NormalizedArguments:
    """Apply syntactic schema-directed normalization without semantic coercion."""

    normalized, paths = _normalize_model(dict(args), params_model, ())
    return NormalizedArguments(args=normalized, paths=tuple(paths))


def _normalize_model(
    value: dict[str, object],
    model: type[BaseModel],
    path: tuple[str, ...],
) -> tuple[dict[str, object], list[str]]:
    normalized = dict(value)
    changed: list[str] = []
    for name, field in model.model_fields.items():
        key = _field_key(value, name, field)
        if key is None:
            continue
        item, item_paths = _normalize_value(value[key], field.annotation, (*path, key))
        normalized[key] = item
        changed.extend(item_paths)
    return normalized, changed


def _field_key(value: Mapping[str, object], name: str, field: FieldInfo) -> str | None:
    candidates = [name]
    if isinstance(field.alias, str):
        candidates.insert(0, field.alias)
    if isinstance(field.validation_alias, str):
        candidates.insert(0, field.validation_alias)
    return next((candidate for candidate in candidates if candidate in value), None)


def _normalize_value(
    value: object,
    annotation: object,
    path: tuple[str, ...],
) -> tuple[object, list[str]]:
    candidates = _annotation_candidates(annotation)
    shape = _common_structured_shape(candidates)
    changed: list[str] = []
    if isinstance(value, str) and shape is not None:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value, changed
        if shape == "object" and not isinstance(decoded, dict):
            return value, changed
        if shape == "array" and not isinstance(decoded, list):
            return value, changed
        value = decoded
        changed.append(_render_path(path))

    matching = [candidate for candidate in candidates if _matches_shape(value, candidate)]
    if len(matching) == 1:
        nested, nested_paths = _normalize_for_annotation(value, matching[0], path)
        return nested, [*changed, *nested_paths]
    if len(matching) > 1:
        resolved = _resolve_union_candidate(value, matching, path)
        if resolved is not None:
            nested, nested_paths = resolved
            return nested, [*changed, *nested_paths]
    return value, changed


def _normalize_for_annotation(
    value: object,
    annotation: object,
    path: tuple[str, ...],
) -> tuple[object, list[str]]:
    annotation = _unwrap_annotated(annotation)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if not isinstance(value, dict):
            return value, []
        return _normalize_model(value, annotation, path)

    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin in {list, set, frozenset, Sequence} and isinstance(value, list):
        item_type = arguments[0] if arguments else Any
        normalized_items: list[object] = []
        changed: list[str] = []
        for index, item in enumerate(value):
            normalized, item_paths = _normalize_value(item, item_type, (*path, str(index)))
            normalized_items.append(normalized)
            changed.extend(item_paths)
        return normalized_items, changed
    if origin is tuple and isinstance(value, list):
        normalized_items = []
        changed = []
        repeated = len(arguments) == 2 and arguments[1] is Ellipsis
        for index, item in enumerate(value):
            if repeated:
                item_type = arguments[0]
            else:
                item_type = arguments[index] if index < len(arguments) else Any
            normalized, item_paths = _normalize_value(item, item_type, (*path, str(index)))
            normalized_items.append(normalized)
            changed.extend(item_paths)
        return normalized_items, changed
    if origin in {dict, Mapping} and isinstance(value, dict):
        value_type = arguments[1] if len(arguments) == 2 else Any
        normalized_values: dict[object, object] = {}
        changed = []
        for key, item in value.items():
            normalized, item_paths = _normalize_value(
                item,
                value_type,
                (*path, str(key)),
            )
            normalized_values[key] = normalized
            changed.extend(item_paths)
        return normalized_values, changed
    return value, []


def _resolve_union_candidate(
    value: object,
    candidates: list[object],
    path: tuple[str, ...],
) -> tuple[object, list[str]] | None:
    valid: list[tuple[object, list[str]]] = []
    for candidate in candidates:
        normalized, changed = _normalize_for_annotation(value, candidate, path)
        try:
            TypeAdapter(candidate).validate_python(normalized)
        except ValidationError:
            continue
        valid.append((normalized, changed))
    return valid[0] if len(valid) == 1 else None


def _annotation_candidates(annotation: object) -> list[object]:
    annotation = _unwrap_annotated(annotation)
    origin = get_origin(annotation)
    if origin in {Union, types.UnionType}:
        return [item for item in get_args(annotation) if item is not type(None)]
    return [annotation]


def _unwrap_annotated(annotation: object) -> object:
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def _common_structured_shape(candidates: list[object]) -> str | None:
    shapes = {_structured_shape(candidate) for candidate in candidates}
    shapes.discard(None)
    if len(shapes) != 1 or any(_structured_shape(candidate) is None for candidate in candidates):
        return None
    return next(iter(shapes))


def _structured_shape(annotation: object) -> str | None:
    annotation = _unwrap_annotated(annotation)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "object"
    origin = get_origin(annotation)
    if origin in {dict, Mapping}:
        return "object"
    if origin in {list, tuple, set, frozenset, Sequence}:
        return "array"
    return None


def _matches_shape(value: object, annotation: object) -> bool:
    shape = _structured_shape(annotation)
    if shape == "object":
        return isinstance(value, dict)
    if shape == "array":
        return isinstance(value, list)
    return False


def _render_path(path: tuple[str, ...]) -> str:
    return ".".join(path) if path else "$"
