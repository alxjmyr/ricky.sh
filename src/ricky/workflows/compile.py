"""Workflow graph compiler and aggregate validation."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ricky.config import WorkflowSettings
from ricky.tool_contracts import ToolContractError, inspect_tool_contract
from ricky.workflows.operators import DataOperatorRegistry, default_operator_registry
from ricky.workflows.schema import ResultSchema, schema_type_at_path, validate_schema_depth
from ricky.workflows.spec import (
    AgentStep,
    ApprovalStep,
    CheckStep,
    Condition,
    DataStep,
    ForeachStep,
    MessageStep,
    ModelStep,
    ModelTaskBase,
    ShellCheck,
    Step,
    ToolStep,
    WorkflowSpec,
)
from ricky.workflows.values import ValueExpr, iter_references, parse_reference


class CompiledGraph(BaseModel):
    """JSON-safe immutable output of the workflow compiler."""

    model_config = ConfigDict(frozen=True)

    spec: WorkflowSpec
    roots: list[str]
    order: list[str]
    body_roots: dict[str, list[str]] = Field(default_factory=dict)
    fingerprint: str
    tool_result_schemas: dict[str, dict[str, Any]] = Field(default_factory=dict)
    tool_risks: dict[str, str] = Field(default_factory=dict)
    tool_effect_kinds: dict[str, str] = Field(default_factory=dict)
    operator_result_schemas: dict[str, dict[str, Any]] = Field(default_factory=dict)


class CompileResult(BaseModel):
    """All detected errors and the graph when compilation succeeds."""

    graph: CompiledGraph | None = None
    errors: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.graph is not None and not self.errors


class ToolLookup(Protocol):
    """Narrow compiler view of the runtime tool registry."""

    def get(self, name: str) -> Any:
        """Return one registered tool or None."""
        ...


def compile_workflow(
    spec: WorkflowSpec,
    *,
    tool_registry: ToolLookup,
    operator_registry: DataOperatorRegistry | None = None,
    skill_names: set[str] | None = None,
    settings: WorkflowSettings | None = None,
) -> CompileResult:
    """Compile one graph and report every independent problem found."""

    operators = operator_registry or default_operator_registry()
    limits = settings or WorkflowSettings()
    errors: list[str] = []

    all_ids = [step.id for step in spec.steps]
    for step in spec.steps:
        if isinstance(step, ForeachStep):
            all_ids.extend(child.id for child in step.body)
    for step_id in sorted({value for value in all_ids if all_ids.count(value) > 1}):
        errors.append(f"workflow: step id '{step_id}' is reused across execution graph scopes")

    for name, schema in spec.schemas.items():
        try:
            validate_schema_depth(schema, max_depth=limits.max_schema_depth)
        except ValueError as exc:
            errors.append(f"schema '{name}': {exc}")

    top = _validate_graph(spec.steps, label="workflow", errors=errors)
    body_roots: dict[str, list[str]] = {}
    for step in spec.steps:
        if isinstance(step, ForeachStep):
            if any(isinstance(child, ForeachStep) for child in step.body):
                errors.append(f"step '{step.id}': nested foreach is not supported")
            body = _validate_graph(
                step.body,
                label=f"foreach '{step.id}' body",
                errors=errors,
            )
            body_roots[step.id] = body.roots
            maximum = step.max_items or limits.max_foreach_items
            if maximum > limits.max_foreach_items:
                errors.append(
                    f"step '{step.id}': max_items {maximum} exceeds configured maximum "
                    f"{limits.max_foreach_items}"
                )
            item_parallel = step.max_parallel_items or limits.max_parallel_items
            if item_parallel > limits.max_parallel_items:
                errors.append(
                    f"step '{step.id}': max_parallel_items {item_parallel} exceeds "
                    f"configured maximum {limits.max_parallel_items}"
                )

    tool_schemas: dict[str, dict[str, Any]] = {}
    tool_risks: dict[str, str] = {}
    tool_effect_kinds: dict[str, str] = {}
    operator_schemas: dict[str, dict[str, Any]] = {}
    output_schemas: dict[str, ResultSchema] = {}
    _validate_step_contracts(
        spec,
        spec.steps,
        tool_registry=tool_registry,
        operators=operators,
        skill_names=skill_names or set(),
        errors=errors,
        tool_schemas=tool_schemas,
        tool_risks=tool_risks,
        tool_effect_kinds=tool_effect_kinds,
        operator_schemas=operator_schemas,
        output_schemas=output_schemas,
        label_prefix="step",
    )
    for parent in spec.steps:
        if not isinstance(parent, ForeachStep):
            continue
        body_outputs: dict[str, ResultSchema] = {}
        _validate_step_contracts(
            spec,
            parent.body,
            tool_registry=tool_registry,
            operators=operators,
            skill_names=skill_names or set(),
            errors=errors,
            tool_schemas=tool_schemas,
            tool_risks=tool_risks,
            tool_effect_kinds=tool_effect_kinds,
            operator_schemas=operator_schemas,
            output_schemas=body_outputs,
            label_prefix=f"foreach '{parent.id}' body step",
        )
        _validate_references(
            spec,
            parent.body,
            graph=top,
            output_schemas=output_schemas,
            body_graph=_graph_info(parent.body),
            body_outputs=body_outputs,
            parent=parent,
            errors=errors,
        )
        projection_graph = _graph_info(parent.body)
        projection_graph.ancestors["__foreach_output__"] = set(projection_graph.steps)
        for expression in parent.outputs.values():
            for reference in iter_references(expression):
                _validate_reference(
                    reference,
                    label=f"foreach '{parent.id}' output",
                    current_step="__foreach_output__",
                    spec=spec,
                    graph=top,
                    active_graph=projection_graph,
                    output_schemas=output_schemas,
                    body_outputs=body_outputs,
                    allow_item=True,
                    parent=parent,
                    errors=errors,
                )

    _validate_references(
        spec,
        spec.steps,
        graph=top,
        output_schemas=output_schemas,
        body_graph=None,
        body_outputs={},
        parent=None,
        errors=errors,
    )

    if spec.max_parallel_steps is not None and spec.max_parallel_steps > limits.max_parallel_steps:
        errors.append(
            f"workflow max_parallel_steps {spec.max_parallel_steps} exceeds configured "
            f"maximum {limits.max_parallel_steps}"
        )

    for step in [
        current
        for parent in spec.steps
        for current in ([parent, *parent.body] if isinstance(parent, ForeachStep) else [parent])
    ]:
        if (
            isinstance(step, ModelStep | AgentStep)
            and step.retry.max_attempts > limits.model_attempts
        ):
            errors.append(
                f"step '{step.id}': retry max_attempts {step.retry.max_attempts} "
                f"exceeds configured maximum {limits.model_attempts}"
            )
        if (
            isinstance(step, AgentStep)
            and step.max_iterations is not None
            and step.max_iterations > limits.agent_iterations
        ):
            errors.append(
                f"step '{step.id}': max_iterations {step.max_iterations} exceeds "
                f"configured maximum {limits.agent_iterations}"
            )

    errors = list(dict.fromkeys(errors))
    if errors:
        return CompileResult(errors=errors)

    fingerprint_payload = {
        "spec": spec.model_dump(mode="json", by_alias=True),
        "tool_results": tool_schemas,
        "tool_risks": tool_risks,
        "tool_effect_kinds": tool_effect_kinds,
        "operator_results": operator_schemas,
    }
    encoded = json.dumps(
        fingerprint_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    graph = CompiledGraph(
        spec=spec,
        roots=top.roots,
        order=[step.id for step in spec.steps],
        body_roots=body_roots,
        fingerprint=hashlib.sha256(encoded).hexdigest(),
        tool_result_schemas=tool_schemas,
        tool_risks=tool_risks,
        tool_effect_kinds=tool_effect_kinds,
        operator_result_schemas=operator_schemas,
    )
    return CompileResult(graph=graph)


class _GraphInfo:
    def __init__(self, steps: list[Step]) -> None:
        self.steps = {step.id: step for step in steps}
        self.order = [step.id for step in steps]
        self.roots = [step.id for step in steps if not step.needs]
        self.ancestors = {step.id: _ancestors(step.id, self.steps) for step in steps}


def _graph_info(steps: list[Step]) -> _GraphInfo:
    return _GraphInfo(steps)


def _validate_graph(steps: list[Step], *, label: str, errors: list[str]) -> _GraphInfo:
    ids = [step.id for step in steps]
    duplicates = sorted({step_id for step_id in ids if ids.count(step_id) > 1})
    for step_id in duplicates:
        errors.append(f"{label}: duplicate step id '{step_id}'")
    known = set(ids)
    for step in steps:
        duplicate_needs = sorted({name for name in step.needs if step.needs.count(name) > 1})
        if duplicate_needs:
            errors.append(
                f"{label} step '{step.id}': duplicate dependencies: " + ", ".join(duplicate_needs)
            )
        if step.id in step.needs:
            errors.append(f"{label} step '{step.id}': a step cannot depend on itself")
        for dependency in step.needs:
            if dependency not in known:
                errors.append(f"{label} step '{step.id}': unknown dependency '{dependency}'")
    roots = [step.id for step in steps if not step.needs]
    if not roots:
        errors.append(f"{label}: graph has no root step")
    if not duplicates and not _has_unknown_dependencies(steps, known):
        cycle = _cycle_nodes(steps)
        if cycle:
            errors.append(f"{label}: dependency cycle: {' -> '.join(cycle)}")
        else:
            reachable = _reachable_from_roots(steps, roots)
            for step_id in ids:
                if step_id not in reachable:
                    errors.append(f"{label}: step '{step_id}' is unreachable from a root")
    return _graph_info(steps)


def _validate_step_contracts(
    spec: WorkflowSpec,
    steps: list[Step],
    *,
    tool_registry: ToolLookup,
    operators: DataOperatorRegistry,
    skill_names: set[str],
    errors: list[str],
    tool_schemas: dict[str, dict[str, Any]],
    tool_risks: dict[str, str],
    tool_effect_kinds: dict[str, str],
    operator_schemas: dict[str, dict[str, Any]],
    output_schemas: dict[str, ResultSchema],
    label_prefix: str,
) -> None:
    for step in steps:
        label = f"{label_prefix} '{step.id}'"
        if isinstance(step, ModelTaskBase):
            schema = spec.schemas.get(step.result_schema)
            if schema is None:
                errors.append(f"{label}: unknown result schema '{step.result_schema}'")
            else:
                output_schemas[step.id] = schema
            if step.skill is not None and step.skill not in skill_names:
                errors.append(f"{label}: unknown prompt skill '{step.skill}'")

        if isinstance(step, AgentStep):
            for name in step.tools:
                tool = tool_registry.get(name)
                if tool is None:
                    errors.append(f"{label}: unknown tool '{name}'")
                elif tool.risk != "read_only":
                    errors.append(
                        f"{label}: agent tool '{name}' is {tool.risk}; only read_only is allowed"
                    )
                else:
                    try:
                        inspect_tool_contract(tool)
                    except ToolContractError as exc:
                        errors.append(f"{label}: {exc}")

        if isinstance(step, ToolStep):
            tool = tool_registry.get(step.tool)
            if tool is None:
                errors.append(f"{label}: unknown tool '{step.tool}'")
            else:
                tool_risks[step.tool] = tool.risk
                try:
                    metadata = inspect_tool_contract(tool)
                except ToolContractError as exc:
                    errors.append(f"{label}: {exc}")
                    metadata = None
                if metadata is not None:
                    tool_effect_kinds[step.tool] = metadata.effect_kind
                result_model = getattr(tool, "Result", None)
                if step.expose_output and result_model is None:
                    errors.append(f"{label}: tool '{step.tool}' has no declared Result model")
                if result_model is not None:
                    schema = result_model.model_json_schema()
                    tool_schemas[step.tool] = schema
                _validate_param_names(label, step.args, tool.Params, errors)
                if step.retry.max_attempts > 1:
                    if tool.risk != "read_only":
                        idempotent = bool(getattr(tool, "idempotent_replay", False))
                        key_factory = getattr(tool, "idempotency_key", None)
                        if not idempotent or not callable(key_factory):
                            errors.append(
                                f"{label}: effect tool retry requires declared idempotent "
                                "replay and an idempotency key"
                            )
                    elif not bool(getattr(tool, "safe_replay", False)):
                        errors.append(
                            f"{label}: read-only tool retry requires declared safe_replay"
                        )

        if isinstance(step, DataStep):
            operator = operators.get(step.operator)
            if operator is None:
                errors.append(f"{label}: unknown data operator '{step.operator}'")
            else:
                _validate_param_names(label, step.args, operator.Params, errors)
                operator_schemas[step.operator] = operator.Result.model_json_schema()

        if not isinstance(step, ModelStep | AgentStep | ToolStep) and step.retry.max_attempts > 1:
            errors.append(f"{label}: this step kind does not support retries")


def _validate_param_names(
    label: str,
    args: Mapping[str, ValueExpr],
    params_model: type[BaseModel],
    errors: list[str],
) -> None:
    fields = params_model.model_fields
    unknown = sorted(set(args) - set(fields))
    missing = sorted(
        name for name, field in fields.items() if field.is_required() and name not in args
    )
    if unknown:
        errors.append(f"{label}: unknown argument(s): {', '.join(unknown)}")
    if missing:
        errors.append(f"{label}: missing argument(s): {', '.join(missing)}")


def _validate_references(
    spec: WorkflowSpec,
    steps: list[Step],
    *,
    graph: _GraphInfo,
    output_schemas: dict[str, ResultSchema],
    body_graph: _GraphInfo | None,
    body_outputs: dict[str, ResultSchema],
    parent: ForeachStep | None,
    errors: list[str],
) -> None:
    active_graph = body_graph or graph
    for step in steps:
        label = f"step '{step.id}'"
        if parent is not None:
            label = f"foreach '{parent.id}' body step '{step.id}'"
        expressions = list(_step_expressions(step))
        for expression, allow_item in expressions:
            for reference in iter_references(expression):
                _validate_reference(
                    reference,
                    label=label,
                    current_step=step.id,
                    spec=spec,
                    graph=graph,
                    active_graph=active_graph,
                    output_schemas=output_schemas,
                    body_outputs=body_outputs,
                    allow_item=allow_item or body_graph is not None,
                    parent=parent,
                    errors=errors,
                )
        for condition in _step_conditions(step):
            _validate_reference(
                condition.ref,
                label=label,
                current_step=step.id,
                spec=spec,
                graph=graph,
                active_graph=active_graph,
                output_schemas=output_schemas,
                body_outputs=body_outputs,
                allow_item=body_graph is not None,
                parent=parent,
                errors=errors,
                condition=condition,
            )


def _validate_reference(
    reference: str,
    *,
    label: str,
    current_step: str,
    spec: WorkflowSpec,
    graph: _GraphInfo,
    active_graph: _GraphInfo,
    output_schemas: dict[str, ResultSchema],
    body_outputs: dict[str, ResultSchema],
    allow_item: bool,
    parent: ForeachStep | None,
    errors: list[str],
    condition: Condition | None = None,
) -> None:
    try:
        parts = parse_reference(reference)
    except ValueError as exc:
        errors.append(f"{label}: {exc}")
        return
    root = parts[0]
    schema: ResultSchema | None = None
    if root == "trigger":
        if len(parts) != 2 or parts[1] not in spec.args:
            errors.append(f"{label}: unknown trigger reference '{reference}'")
    elif root == "steps":
        if len(parts) < 3 or parts[1] not in graph.steps:
            errors.append(f"{label}: unknown step reference '{reference}'")
            return
        source = parts[1]
        allowed = (
            source in graph.ancestors.get(parent.id, set())
            if parent is not None
            else source in active_graph.ancestors.get(current_step, set())
        )
        if not allowed:
            errors.append(f"{label}: reference '{reference}' is not from a dependency")
        if parts[2] not in {"status", "output", "error"}:
            errors.append(f"{label}: invalid step record field in '{reference}'")
        elif parts[2] == "output":
            schema = output_schemas.get(source)
            if schema is not None and len(parts) > 3:
                schema = schema_type_at_path(schema, parts[3:])
                if schema is None:
                    errors.append(f"{label}: output path does not exist in '{reference}'")
    elif root == "item":
        if not allow_item:
            errors.append(f"{label}: item reference is valid only inside foreach")
            return
        if len(parts) < 2 or parts[1] not in {"source", "key", "index", "steps"}:
            errors.append(f"{label}: invalid item reference '{reference}'")
            return
        if parts[1] == "steps":
            if len(parts) < 4 or parts[2] not in active_graph.steps:
                errors.append(f"{label}: unknown item step reference '{reference}'")
                return
            source = parts[2]
            if source not in active_graph.ancestors.get(current_step, set()):
                errors.append(f"{label}: reference '{reference}' is not from a dependency")
            if parts[3] not in {"status", "output", "error"}:
                errors.append(f"{label}: invalid item step record field in '{reference}'")
            elif parts[3] == "output":
                schema = body_outputs.get(source)
                if schema is not None and len(parts) > 4:
                    schema = schema_type_at_path(schema, parts[4:])
                    if schema is None:
                        errors.append(f"{label}: output path does not exist in '{reference}'")
    if (
        condition is not None
        and schema is not None
        and condition.operator in {"is_true", "is_false"}
        and schema.type != "boolean"
    ):
        errors.append(
            f"{label}: condition {condition.operator!r} requires boolean reference "
            f"'{reference}', found {schema.type}"
        )


def _step_expressions(step: Step) -> Iterable[tuple[ValueExpr, bool]]:
    if isinstance(step, ToolStep | DataStep):
        yield from ((value, False) for value in step.args.values())
    if isinstance(step, ModelTaskBase):
        yield from ((value, False) for value in step.inputs.values())
    if isinstance(step, ApprovalStep):
        yield step.prompt, False
        for value in (step.proposal, step.collection):
            if value is not None:
                yield value, False
        if step.item_key is not None:
            yield step.item_key, True
    if isinstance(step, MessageStep):
        yield step.message, False
    if isinstance(step, ForeachStep):
        yield step.collection, False
        yield step.item_key, True


def _step_conditions(step: Step) -> Iterable[Condition]:
    if step.when is not None:
        yield step.when
    if isinstance(step, CheckStep) and not isinstance(step.check, ShellCheck):
        yield step.check


def _ancestors(step_id: str, steps: Mapping[str, Step]) -> set[str]:
    found: set[str] = set()
    step = steps.get(step_id)
    pending = list(step.needs if step is not None else [])
    while pending:
        dependency = pending.pop()
        if dependency in found:
            continue
        found.add(dependency)
        if dependency in steps:
            pending.extend(steps[dependency].needs)
    return found


def _has_unknown_dependencies(steps: list[Step], known: set[str]) -> bool:
    return any(dependency not in known for step in steps for dependency in step.needs)


def _cycle_nodes(steps: list[Step]) -> list[str]:
    known = {step.id: step for step in steps}
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(step_id: str) -> list[str] | None:
        state[step_id] = 1
        stack.append(step_id)
        for dependency in known[step_id].needs:
            if state.get(dependency) == 1:
                start = stack.index(dependency)
                return [*stack[start:], dependency]
            if state.get(dependency, 0) == 0:
                cycle = visit(dependency)
                if cycle:
                    return cycle
        stack.pop()
        state[step_id] = 2
        return None

    for step in steps:
        if state.get(step.id, 0) == 0:
            cycle = visit(step.id)
            if cycle:
                return cycle
    return []


def _reachable_from_roots(steps: list[Step], roots: list[str]) -> set[str]:
    dependents: dict[str, list[str]] = {step.id: [] for step in steps}
    for step in steps:
        for dependency in step.needs:
            dependents[dependency].append(step.id)
    reached = set(roots)
    pending = deque(roots)
    while pending:
        current = pending.popleft()
        for child in dependents[current]:
            if child not in reached:
                reached.add(child)
                pending.append(child)
    return reached
