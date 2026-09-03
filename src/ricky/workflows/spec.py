"""Workflow authoring models and TOML parser."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterator, Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

from ricky.workflows.schema import ResultSchema
from ricky.workflows.values import ValueExpr

NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"

ArgType = Literal["string", "integer", "number", "boolean", "string_list"]
DependencyPolicy = Literal["success", "terminal"]
OnErrorPolicy = Literal["fail_workflow", "continue"]
RetryCategory = Literal["invalid_output", "provider_error", "timeout", "tool_error"]


class _AuthoringModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class WorkflowArg(_AuthoringModel):
    """One typed invocation argument."""

    type: ArgType
    description: str = Field(min_length=1)
    required: bool = True
    default: JsonValue = None
    minimum: float | None = Field(default=None, alias="min")
    maximum: float | None = Field(default=None, alias="max")
    min_length: int | None = Field(default=None, ge=0)
    max_length: int | None = Field(default=None, ge=0)
    values: list[str] | None = None

    @model_validator(mode="before")
    @classmethod
    def _derive_required(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        if "required" not in result:
            result["required"] = "default" not in result
        return result

    @model_validator(mode="after")
    def _validate_arg(self) -> WorkflowArg:
        if self.required and self.default is not None:
            raise ValueError("a required arg cannot declare a default")
        if not self.required and self.default is None:
            raise ValueError("an optional arg requires a default")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("min cannot exceed max")
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("min_length cannot exceed max_length")
        if (self.minimum is not None or self.maximum is not None) and self.type not in {
            "integer",
            "number",
        }:
            raise ValueError("min and max are valid only for numeric args")
        if (self.min_length is not None or self.max_length is not None) and self.type not in {
            "string",
            "string_list",
        }:
            raise ValueError("length constraints are valid only for string and string_list args")
        if self.values is not None and self.type not in {"string", "string_list"}:
            raise ValueError("values is valid only for string and string_list args")
        if self.values is not None and len(set(self.values)) != len(self.values):
            raise ValueError("arg values must be unique")
        if self.default is not None:
            validate_arg_value("default", self, self.default)
        return self


class RetryPolicy(_AuthoringModel):
    """Explicit retry categories for one step."""

    max_attempts: int = Field(default=1, ge=1)
    on: list[RetryCategory] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_retry(self) -> RetryPolicy:
        if len(set(self.on)) != len(self.on):
            raise ValueError("retry categories must be unique")
        if self.max_attempts > 1 and not self.on:
            raise ValueError("a retry with more than one attempt requires at least one category")
        return self


class Condition(_AuthoringModel):
    """One pure condition in canonical or author-friendly form."""

    ref: str
    operator: Literal["equals", "not_equals", "in", "exists", "is_true", "is_false"]
    value: JsonValue = None

    @model_validator(mode="before")
    @classmethod
    def _from_flat_form(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "operator" in value:
            return value
        operators = [
            name
            for name in ("equals", "not_equals", "in", "exists", "is_true", "is_false")
            if name in value
        ]
        if len(operators) != 1:
            raise ValueError("a condition requires exactly one operator")
        operator = operators[0]
        operand = value[operator]
        if operator in {"exists", "is_true", "is_false"}:
            if operand is not True:
                raise ValueError(f"condition operator {operator!r} must be true")
            operand = None
        return {"ref": value.get("ref"), "operator": operator, "value": operand}

    @model_validator(mode="after")
    def _validate_condition(self) -> Condition:
        if self.operator == "in":
            if not isinstance(self.value, list):
                raise ValueError("an 'in' condition requires a literal list")
        elif self.operator in {"exists", "is_true", "is_false"} and self.value is not None:
            raise ValueError(f"condition {self.operator!r} does not accept a value")
        return self


class StepBase(_AuthoringModel):
    """Fields shared by every workflow step."""

    id: str = Field(pattern=NAME_PATTERN)
    needs: list[str] = Field(default_factory=list)
    dependency_policy: DependencyPolicy = "success"
    on_error: OnErrorPolicy = "fail_workflow"
    when: Condition | None = None
    retry: RetryPolicy = Field(default_factory=RetryPolicy)


class ToolStep(StepBase):
    kind: Literal["tool"] = "tool"
    tool: str
    args: dict[str, ValueExpr] = Field(default_factory=dict)
    expose_output: bool = True


class ModelTaskBase(StepBase):
    instruction: str | None = None
    instruction_file: str | None = None
    inputs: dict[str, ValueExpr] = Field(default_factory=dict)
    result_schema: str
    skill: str | None = None

    @model_validator(mode="after")
    def _one_instruction_source(self) -> ModelTaskBase:
        if (self.instruction is None) == (self.instruction_file is None):
            raise ValueError("declare exactly one of instruction or instruction_file")
        return self


class ModelStep(ModelTaskBase):
    kind: Literal["model"] = "model"


class AgentStep(ModelTaskBase):
    kind: Literal["agent"] = "agent"
    tools: list[str] = Field(default_factory=list)
    max_iterations: int | None = Field(default=None, ge=1)


class DataStep(StepBase):
    kind: Literal["data"] = "data"
    operator: str
    args: dict[str, ValueExpr] = Field(default_factory=dict)


class ShellCheck(_AuthoringModel):
    kind: Literal["shell"] = "shell"
    command: str
    expect_exit: int = 0
    timeout_seconds: float | None = Field(default=None, gt=0)


class CheckStep(StepBase):
    kind: Literal["check"] = "check"
    check: Condition | ShellCheck


class ApprovalStep(StepBase):
    kind: Literal["approval"] = "approval"
    mode: Literal["confirm", "select"]
    prompt: ValueExpr
    proposal: ValueExpr | None = None
    collection: ValueExpr | None = None
    item_key: ValueExpr | None = None

    @model_validator(mode="after")
    def _validate_mode_fields(self) -> ApprovalStep:
        if self.mode == "confirm":
            if self.proposal is None or self.collection is not None or self.item_key is not None:
                raise ValueError("confirm approval requires proposal only")
        elif self.collection is None or self.item_key is None or self.proposal is not None:
            raise ValueError("select approval requires collection and item_key only")
        return self


class MessageStep(StepBase):
    kind: Literal["message"] = "message"
    message: ValueExpr


class ForeachStep(StepBase):
    kind: Literal["foreach"] = "foreach"
    collection: ValueExpr
    item_key: ValueExpr
    outputs: dict[Annotated[str, Field(pattern=NAME_PATTERN)], ValueExpr] = Field(
        default_factory=dict
    )
    body: list[Step] = Field(min_length=1)
    max_items: int | None = Field(default=None, ge=1)
    max_parallel_items: int | None = Field(default=None, ge=1)
    on_item_error: Literal["abort", "collect"] = "abort"


Step = Annotated[
    ToolStep
    | ModelStep
    | AgentStep
    | DataStep
    | CheckStep
    | ApprovalStep
    | MessageStep
    | ForeachStep,
    Field(discriminator="kind"),
]

ForeachStep.model_rebuild()


class WorkflowSpec(_AuthoringModel):
    """One declarative execution graph."""

    version: Literal[2]
    name: str = Field(pattern=NAME_PATTERN)
    description: str = Field(min_length=1)
    args: dict[str, WorkflowArg] = Field(default_factory=dict)
    schemas: dict[str, ResultSchema] = Field(default_factory=dict)
    steps: list[Step] = Field(min_length=1)
    max_parallel_steps: int | None = Field(default=None, ge=1)


def iter_steps(steps: list[Step]) -> Iterator[Step]:
    """Yield top-level and foreach body steps in document order."""

    for step in steps:
        yield step
        if isinstance(step, ForeachStep):
            yield from step.body


def parse_workflow_toml(text: str, *, source: str = "workflow.toml") -> WorkflowSpec:
    """Parse one version 2 workflow TOML body."""

    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in {source}: {exc}") from exc
    version = raw.get("version")
    if version != 2:
        raise ValueError(f"workflow in {source} is not version 2")
    try:
        return WorkflowSpec.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"invalid workflow spec in {source}: {exc}") from exc


def resolve_trigger_args(spec: WorkflowSpec, provided: Mapping[str, Any]) -> dict[str, JsonValue]:
    """Validate typed invocation values before a run exists."""

    unknown = sorted(set(provided) - set(spec.args))
    missing = sorted(
        name
        for name, declaration in spec.args.items()
        if declaration.required and name not in provided
    )
    if unknown or missing:
        problems: list[str] = []
        if missing:
            problems.append("missing required arg(s): " + ", ".join(missing))
        if unknown:
            problems.append("unknown arg(s): " + ", ".join(unknown))
        raise ValueError(f"workflow '{spec.name}' cannot start: {'; '.join(problems)}")

    result: dict[str, JsonValue] = {}
    for name, declaration in spec.args.items():
        value = provided.get(name, declaration.default)
        result[name] = validate_arg_value(name, declaration, value)
    return json.loads(json.dumps(result, allow_nan=False))


def validate_arg_value(name: str, declaration: WorkflowArg, value: Any) -> JsonValue:
    """Validate one invocation value without coercion."""

    if declaration.type == "string":
        if not isinstance(value, str):
            raise ValueError(f"arg '{name}' must be a string")
        length = len(value)
        if declaration.values is not None and value not in declaration.values:
            raise ValueError(f"arg '{name}' must be one of {declaration.values}")
    elif declaration.type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"arg '{name}' must be an integer")
        length = None
    elif declaration.type == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"arg '{name}' must be a number")
        length = None
    elif declaration.type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"arg '{name}' must be a boolean")
        length = None
    else:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"arg '{name}' must be a list of strings")
        if declaration.values is not None:
            unknown = sorted(set(value) - set(declaration.values))
            if unknown:
                raise ValueError(f"arg '{name}' has disallowed value(s): {', '.join(unknown)}")
        length = len(value)

    if isinstance(value, int | float) and not isinstance(value, bool):
        if declaration.minimum is not None and value < declaration.minimum:
            raise ValueError(f"arg '{name}' is less than {declaration.minimum}")
        if declaration.maximum is not None and value > declaration.maximum:
            raise ValueError(f"arg '{name}' exceeds {declaration.maximum}")
    if length is not None:
        if declaration.min_length is not None and length < declaration.min_length:
            raise ValueError(f"arg '{name}' length is less than {declaration.min_length}")
        if declaration.max_length is not None and length > declaration.max_length:
            raise ValueError(f"arg '{name}' length exceeds {declaration.max_length}")
    return json.loads(json.dumps(value, allow_nan=False))
