"""App-owned pure data operators for Workflow."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValueResult(BaseModel):
    value: JsonValue


class PartitionResult(BaseModel):
    buckets: dict[str, list[JsonValue]]
    unmatched: list[JsonValue]


class DataOperator(Protocol):
    """One pure, registered data operation."""

    name: str
    Params: type[BaseModel]
    Result: type[BaseModel]

    def run(self, params: BaseModel) -> BaseModel:
        """Run without I/O, settings, or session access."""
        ...


class ProjectParams(_Params):
    value: JsonValue
    fields: list[str] = Field(min_length=1)


class ProjectOperator:
    name: str = "project"
    Params: type[BaseModel] = ProjectParams
    Result: type[BaseModel] = ValueResult

    def run(self, params: BaseModel) -> BaseModel:
        parsed = ProjectParams.model_validate(params)
        if isinstance(parsed.value, dict):
            return ValueResult(value=_project_one(parsed.value, parsed.fields))
        if isinstance(parsed.value, list):
            projected: list[JsonValue] = []
            for index, item in enumerate(parsed.value):
                if not isinstance(item, dict):
                    raise ValueError(f"project item {index} is not an object")
                projected.append(_project_one(item, parsed.fields))
            return ValueResult(value=projected)
        raise ValueError("project value must be an object or list of objects")


class FilterParams(_Params):
    items: list[JsonValue]
    path: str
    equals: JsonValue


class FilterOperator:
    name: str = "filter"
    Params: type[BaseModel] = FilterParams
    Result: type[BaseModel] = ValueResult

    def run(self, params: BaseModel) -> BaseModel:
        parsed = FilterParams.model_validate(params)
        kept = [item for item in parsed.items if _path(item, parsed.path) == parsed.equals]
        return ValueResult(value=kept)


class PartitionParams(_Params):
    items: list[JsonValue]
    path: str
    categories: dict[str, JsonValue] = Field(min_length=1)


class PartitionOperator:
    name: str = "partition"
    Params: type[BaseModel] = PartitionParams
    Result: type[BaseModel] = PartitionResult

    def run(self, params: BaseModel) -> BaseModel:
        parsed = PartitionParams.model_validate(params)
        buckets: dict[str, list[JsonValue]] = {name: [] for name in parsed.categories}
        unmatched: list[JsonValue] = []
        for item in parsed.items:
            value = _path(item, parsed.path)
            match = next(
                (name for name, expected in parsed.categories.items() if value == expected),
                None,
            )
            (buckets[match] if match is not None else unmatched).append(item)
        return PartitionResult(buckets=buckets, unmatched=unmatched)


class SortParams(_Params):
    items: list[JsonValue]
    path: str
    descending: bool = False


class SortOperator:
    name: str = "sort"
    Params: type[BaseModel] = SortParams
    Result: type[BaseModel] = ValueResult

    def run(self, params: BaseModel) -> BaseModel:
        parsed = SortParams.model_validate(params)

        def key(item: JsonValue) -> tuple[str, str]:
            value = _path(item, parsed.path)
            return type(value).__name__, json.dumps(value, sort_keys=True)

        return ValueResult(value=sorted(parsed.items, key=key, reverse=parsed.descending))


class LimitParams(_Params):
    items: list[JsonValue]
    count: int = Field(ge=0)


class LimitOperator:
    name: str = "limit"
    Params: type[BaseModel] = LimitParams
    Result: type[BaseModel] = ValueResult

    def run(self, params: BaseModel) -> BaseModel:
        parsed = LimitParams.model_validate(params)
        return ValueResult(value=parsed.items[: parsed.count])


class MergeParams(_Params):
    values: list[JsonValue]
    mode: Literal["objects", "lists"]


class MergeOperator:
    name: str = "merge"
    Params: type[BaseModel] = MergeParams
    Result: type[BaseModel] = ValueResult

    def run(self, params: BaseModel) -> BaseModel:
        parsed = MergeParams.model_validate(params)
        if parsed.mode == "objects":
            merged: dict[str, JsonValue] = {}
            for index, value in enumerate(parsed.values):
                if not isinstance(value, dict):
                    raise ValueError(f"merge value {index} is not an object")
                merged.update(value)
            return ValueResult(value=merged)
        items: list[JsonValue] = []
        for index, value in enumerate(parsed.values):
            if not isinstance(value, list):
                raise ValueError(f"merge value {index} is not a list")
            items.extend(value)
        return ValueResult(value=items)


class DataOperatorRegistry:
    """An explicit registry of pure operators."""

    def __init__(self, operators: list[DataOperator] | tuple[DataOperator, ...]) -> None:
        self._operators = {operator.name: operator for operator in operators}
        if len(self._operators) != len(operators):
            raise ValueError("data operator names must be unique")

    def get(self, name: str) -> DataOperator | None:
        return self._operators.get(name)

    def names(self) -> set[str]:
        return set(self._operators)

    def run(self, name: str, args: Mapping[str, Any]) -> BaseModel:
        operator = self.get(name)
        if operator is None:
            raise ValueError(f"unknown data operator: {name}")
        params = operator.Params.model_validate(dict(args))
        result = operator.run(params)
        return operator.Result.model_validate(result)


def default_operator_registry() -> DataOperatorRegistry:
    """Build the fixed first-release operator set."""

    return DataOperatorRegistry(
        [
            ProjectOperator(),
            FilterOperator(),
            PartitionOperator(),
            SortOperator(),
            LimitOperator(),
            MergeOperator(),
        ]
    )


def _project_one(value: Mapping[str, JsonValue], fields: list[str]) -> dict[str, JsonValue]:
    missing = [name for name in fields if name not in value]
    if missing:
        raise ValueError("project field(s) are missing: " + ", ".join(missing))
    return {name: value[name] for name in fields}


def _path(value: JsonValue, path: str) -> JsonValue:
    if not path:
        return value
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise ValueError(f"data path {path!r} is missing at {part!r}")
        current = current[part]
    return current
