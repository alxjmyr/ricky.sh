"""Leaf types and validation for execution-neutral tool declarations."""

from __future__ import annotations

import inspect
import json
import re
from typing import Any, Literal, cast, get_args

from pydantic import BaseModel, ConfigDict, Field, RootModel

Risk = Literal["read_only", "mutating", "destructive"]
EffectKind = Literal["none", "ricky_state", "external"]
EffectAttemptReason = Literal["invalid_preflight", "denied"]
UnattendedUse = Literal["allowed", "forbidden"]
ReviewMode = Literal["policy", "fresh"]

_CAPABILITY_ID = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+$")
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class ToolContractError(ValueError):
    """One tool declaration violates the shared authoring contract."""


class ToolMetadata(BaseModel):
    """Validated execution-neutral facts declared by one tool implementation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    risk: Risk
    capability_id: str | None
    effect_kind: EffectKind
    unattended: UnattendedUse
    state_guard_id: str | None = Field(default=None, max_length=200)
    review_mode: ReviewMode = "policy"


def inspect_tool_review_mode(tool: Any) -> ReviewMode:
    """Resolve and validate one tool's optional interactive-review mode."""

    name = str(getattr(tool, "name", type(tool).__name__))
    value = getattr(tool, "review_mode", "policy")
    if value not in ("policy", "fresh"):
        raise ToolContractError(
            f"tool {name} has invalid review mode; expected 'policy' or 'fresh'"
        )
    return cast(ReviewMode, value)


def inspect_tool_surface(tool: Any) -> None:
    """Validate the provider-facing callable surface at registry construction."""

    name = getattr(tool, "name", None)
    if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
        raise ToolContractError(
            f"tool {type(tool).__name__} has invalid name; use lowercase letters, "
            "digits, and underscores"
        )
    description = getattr(tool, "description", None)
    if not isinstance(description, str) or not description.strip():
        raise ToolContractError(f"tool {name} must declare a non-empty description")
    params = getattr(tool, "Params", None)
    if not isinstance(params, type) or not issubclass(params, BaseModel):
        raise ToolContractError(f"tool {name} Params must be a Pydantic BaseModel class")
    _inspect_json_object_schema(name, "Params", params)
    _inspect_parameter_models(name, params)
    result = getattr(tool, "Result", None)
    if result is not None:
        if not isinstance(result, type) or not issubclass(result, BaseModel):
            raise ToolContractError(f"tool {name} Result must be a Pydantic BaseModel class")
        _inspect_json_object_schema(name, "Result", result)
    run = getattr(tool, "run", None)
    if not callable(run) or not inspect.iscoroutinefunction(run):
        raise ToolContractError(f"tool {name} run must be async")
    version = getattr(tool, "contract_version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ToolContractError(f"tool {name} contract_version must be a positive integer")


def _inspect_json_object_schema(name: str, label: str, model: type[BaseModel]) -> None:
    try:
        schema = model.model_json_schema()
        json.dumps(schema, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ToolContractError(
            f"tool {name} {label} schema is not JSON-compatible: {exc}"
        ) from exc
    if not _schema_describes_object(schema, schema):
        raise ToolContractError(f"tool {name} {label} schema must describe one JSON object")


def _inspect_parameter_models(name: str, params: type[BaseModel]) -> None:
    """Require one strict argument boundary and closed nested object models."""

    if params.model_config.get("strict") is not True:
        raise ToolContractError(f"tool {name} Params must set ConfigDict(strict=True)")
    if not issubclass(params, RootModel) and params.model_config.get("extra") != "forbid":
        raise ToolContractError(f"tool {name} Params must set ConfigDict(extra='forbid')")

    seen: set[type[BaseModel]] = {params}
    pending = [params]
    while pending:
        model = pending.pop()
        for field in model.model_fields.values():
            for nested in _annotation_models(field.annotation):
                if nested in seen:
                    continue
                seen.add(nested)
                pending.append(nested)
                if (
                    not issubclass(nested, RootModel)
                    and nested.model_config.get("extra") != "forbid"
                ):
                    raise ToolContractError(
                        f"tool {name} nested parameter model {nested.__name__} "
                        "must set ConfigDict(extra='forbid')"
                    )


def _annotation_models(annotation: Any) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    models: list[type[BaseModel]] = []
    for argument in get_args(annotation):
        models.extend(_annotation_models(argument))
    return models


def _schema_describes_object(schema: dict[str, Any], root: dict[str, Any]) -> bool:
    if schema.get("type") == "object":
        return True
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        target: Any = root.get("$defs", {}).get(reference.removeprefix("#/$defs/"))
        return isinstance(target, dict) and _schema_describes_object(target, root)
    for keyword in ("oneOf", "anyOf", "allOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list) and branches:
            return all(
                isinstance(branch, dict) and _schema_describes_object(branch, root)
                for branch in branches
            )
    return False


def inspect_tool_contract(tool: Any, *, state_guards: Any = None) -> ToolMetadata:
    """Read and strictly validate one complete tool metadata declaration."""

    name = str(getattr(tool, "name", type(tool).__name__))
    missing = [
        field
        for field in ("risk", "capability_id", "effect_kind", "unattended", "state_guard_id")
        if not hasattr(tool, field)
    ]
    if missing:
        raise ToolContractError(f"tool {name} is missing metadata: {', '.join(missing)}")
    inspect_tool_surface(tool)
    review_mode = inspect_tool_review_mode(tool)
    try:
        metadata = ToolMetadata.model_validate(
            {
                **{
                    field: getattr(tool, field)
                    for field in (
                        "risk",
                        "capability_id",
                        "effect_kind",
                        "unattended",
                        "state_guard_id",
                    )
                },
                "review_mode": review_mode,
            }
        )
    except ValueError as exc:
        raise ToolContractError(f"tool {name} has invalid metadata: {exc}") from exc
    if (
        metadata.capability_id is not None
        and _CAPABILITY_ID.fullmatch(metadata.capability_id) is None
    ):
        raise ToolContractError(f"tool {name} has invalid capability id: {metadata.capability_id}")
    if metadata.risk == "read_only" and metadata.effect_kind == "external":
        raise ToolContractError(f"read-only tool {name} cannot declare an external effect")
    if metadata.risk != "read_only" and metadata.effect_kind == "none":
        raise ToolContractError(f"mutating tool {name} must declare an observable effect kind")
    if metadata.effect_kind == "external" and not callable(getattr(tool, "effect_identity", None)):
        raise ToolContractError(f"external-effect tool {name} lacks an effect identity contract")
    if metadata.state_guard_id is not None:
        if metadata.effect_kind != "ricky_state":
            raise ToolContractError(
                f"tool {name} names a state guard but is not a Ricky-state mutation"
            )
        if state_guards is None or not state_guards.has(metadata.state_guard_id):
            raise ToolContractError(
                f"tool {name} requires unavailable state guard: {metadata.state_guard_id}"
            )
    return metadata
