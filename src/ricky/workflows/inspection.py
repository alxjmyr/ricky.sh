"""Static workflow inspection shared by chat and document renderers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ricky.builtins import bundled_workflows_dir
from ricky.config import RickySettings, profile_data_path
from ricky.profiles import ProfileScope
from ricky.skills.registry import discover_skills
from ricky.tools.registry import ToolRegistry
from ricky.workflows.compile import compile_workflow
from ricky.workflows.registry import (
    find_workflow_bundle,
    load_workflow_bundle,
    resolve_bundle_resource,
)
from ricky.workflows.spec import (
    AgentStep,
    Condition,
    DataStep,
    ForeachStep,
    ModelTaskBase,
    ToolStep,
    iter_steps,
)
from ricky.workflows.values import (
    FormatExpr,
    ListExpr,
    ObjectExpr,
    ReferenceExpr,
    ValueExpr,
)


class InspectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FlowEdge(InspectionModel):
    source: str
    target: str
    kind: Literal["dependency", "data", "condition"]
    reference: str = ""
    destination: str = ""


class StepInspection(InspectionModel):
    id: str
    kind: str
    parent: str | None = None
    needs: list[str]
    declaration: dict[str, JsonValue]
    instruction: str | None = None
    instruction_source: str | None = None
    skill: str | None = None
    skill_body: str | None = None
    output_schema: JsonValue = None
    tools: list[str] = Field(default_factory=list)
    risk: str | None = None
    effect: str | None = None


class WorkflowInspection(InspectionModel):
    identity: str
    description: str
    fingerprint: str
    profiles: list[str]
    args: dict[str, JsonValue]
    steps: list[StepInspection]
    edges: list[FlowEdge]
    max_parallel_steps: int
    context_note: str = (
        "Static design: references are unresolved, not execution values. Model and agent "
        "tasks receive only their instruction, bound inputs, result schema and optional skill "
        "body. They do not inherit chat history, memory or other workflow records. "
        "The runtime requires one schema-valid JSON object with no Markdown or extra prose."
    )


def inspect_workflow(
    name: str, *, settings: RickySettings, scope: ProfileScope, tools: ToolRegistry
) -> WorkflowInspection:
    """Read current, scope-confined sources and compile without executing any step."""
    if not settings.workflow.enabled:
        raise ValueError("Workflows are disabled.")
    bundle = find_workflow_bundle(name, settings=settings, profile_scope=scope)
    if bundle is None:
        raise ValueError(f"Unknown or unavailable workflow: {name}")
    skills = discover_skills(settings=settings, profile_scope=scope)
    spec, errors = load_workflow_bundle(bundle, workflow_settings=settings.workflow)
    compiled = compile_workflow(
        spec, tool_registry=tools, skill_names=skills.identifiers(), settings=settings.workflow
    )
    errors.extend(compiled.errors)
    if errors or compiled.graph is None:
        raise ValueError("Invalid workflow:\n" + "\n".join(errors))
    graph = compiled.graph
    owner = "bundled"
    if not bundle.is_relative_to(bundled_workflows_dir().resolve()):
        owner = next(
            profile
            for profile in scope.profiles
            if bundle.is_relative_to(profile_data_path(settings, profile))
        )
    parents = {
        child.id: step.id
        for step in spec.steps
        if isinstance(step, ForeachStep)
        for child in step.body
    }
    steps: list[StepInspection] = []
    edges: list[FlowEdge] = []
    for step in iter_steps(spec.steps):
        declaration = step.model_dump(mode="json", exclude={"body"})
        detail = StepInspection(
            id=step.id,
            kind=step.kind,
            parent=parents.get(step.id),
            needs=step.needs,
            declaration=declaration,
        )
        if isinstance(step, ModelTaskBase):
            detail.instruction_source = step.instruction_file or "inline"
            detail.instruction = step.instruction
            if step.instruction_file:
                detail.instruction = resolve_bundle_resource(
                    bundle, step.instruction_file
                ).read_text(encoding="utf-8")
            if len(detail.instruction or "") > settings.workflow.instruction_char_limit:
                raise ValueError(f"Instruction for {step.id} exceeds the configured limit")
            if step.skill:
                skill = skills.get(step.skill)
                if skill is None:
                    raise ValueError(f"Unavailable skill: {step.skill}")
                detail.skill = skill.qualified_name
                detail.skill_body = skill.body
            detail.output_schema = spec.schemas[step.result_schema].model_dump(mode="json")
            if isinstance(step, AgentStep):
                detail.tools = step.tools
        elif isinstance(step, ToolStep):
            detail.tools = [step.tool]
            detail.risk = graph.tool_risks[step.tool]
            detail.effect = graph.tool_effect_kinds[step.tool]
            if step.expose_output:
                detail.output_schema = graph.tool_result_schemas.get(step.tool)
        elif isinstance(step, DataStep):
            detail.output_schema = graph.operator_result_schemas.get(step.operator)
        steps.append(detail)
        edges.extend(FlowEdge(source=dep, target=step.id, kind="dependency") for dep in step.needs)
        # Walk only expression/condition-bearing fields, never arbitrary instructions or schemas.
        for field in (
            "inputs",
            "args",
            "when",
            "check",
            "prompt",
            "proposal",
            "collection",
            "item_key",
            "outputs",
            "message",
        ):
            value = getattr(step, field, None)
            edges.extend(_references(value, field, step.id, parents.get(step.id)))
    result = WorkflowInspection(
        identity=f"{owner}/{spec.name}",
        description=spec.description,
        fingerprint="",
        profiles=list(scope.profiles),
        args={k: v.model_dump(mode="json") for k, v in spec.args.items()},
        steps=steps,
        edges=edges,
        max_parallel_steps=spec.max_parallel_steps or settings.workflow.max_parallel_steps,
    )
    # Include resolved instruction files and skill bodies, which can change independently of TOML.
    payload = result.model_dump_json() + graph.fingerprint
    result.fingerprint = hashlib.sha256(payload.encode()).hexdigest()
    return result


def _references(
    value: object, destination: str, target: str, parent: str | None
) -> Iterator[FlowEdge]:
    if isinstance(value, ValueExpr):
        node = value.root
        if isinstance(node, ReferenceExpr):
            yield _reference_edge(node.ref, destination, target, parent)
        elif isinstance(node, ObjectExpr | FormatExpr):
            prefix = destination + ".values" if isinstance(node, FormatExpr) else destination
            for key, child in node.values.items():
                yield from _references(child, f"{prefix}.{key}", target, parent)
        elif isinstance(node, ListExpr):
            for index, child in enumerate(node.values):
                yield from _references(child, f"{destination}[{index}]", target, parent)
        # Literal objects can contain a key called ref; they are never data edges.
    elif isinstance(value, Condition):
        yield _reference_edge(value.ref, destination, target, parent, condition=True)
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _references(child, f"{destination}.{key}", target, parent)


def _reference_edge(
    ref: str, destination: str, target: str, parent: str | None, *, condition: bool = False
) -> FlowEdge:
    parts = ref.split(".")
    source = "$trigger"
    if parts[0] == "steps":
        source = parts[1]
    elif parts[:2] == ["item", "steps"]:
        source = parts[2]
    elif parts[0] == "item":
        source = parent or target
    return FlowEdge(
        source=source,
        target=target,
        kind="condition" if condition else "data",
        reference=ref,
        destination=destination,
    )


def render_ascii(view: WorkflowInspection, *, step: str | None = None, section: str = "all") -> str:
    """Compact ASCII dependency map or exact focused step details."""
    lines = [f"{view.identity} | design {view.fingerprint[:10]}"]
    if step is None:
        lines.append(view.description)
        lines.append("\nExecution dependencies (siblings may run in parallel):")
        for item in view.steps:
            if item.parent:
                continue
            prefix = ", ".join(item.needs) or "START"
            lines.append(f"  {prefix} -> [{item.id}] {item.kind}")
            if item.kind == "foreach":
                count = sum(child.parent == item.id for child in view.steps)
                lines.append(f"    +-- {count} body steps (ask to expand {item.id})")
            if item.declaration.get("when"):
                lines.append("    ? conditional")
        lines.append("\nAsk for a step's instructions, inputs, outputs, policy or body.")
        return "\n".join(lines)
    item = next((item for item in view.steps if item.id == step), None)
    if item is None:
        raise ValueError(f"Unknown step: {step}. Available: " + ", ".join(s.id for s in view.steps))
    lines.extend([f"\n[{item.id}] {item.kind}", f"Needs: {', '.join(item.needs) or '(root)'}"])
    if item.parent:
        lines.append(f"Inside foreach: {item.parent}")
    if item.risk:
        lines.append(f"Tool risk: {item.risk}; effect: {item.effect}")
    if section in {"all", "instructions"}:
        if item.instruction is not None:
            lines.extend(
                [
                    f"\nInstructions ({item.instruction_source}):",
                    item.instruction,
                    f"\nSkill: {item.skill or '(none)'}",
                ]
            )
            if item.skill_body is not None:
                lines.append(item.skill_body)
            lines.extend([f"\nTools: {', '.join(item.tools) or '(none)'}", view.context_note])
        elif section == "instructions":
            lines.append("This step issues no model prompt. See its declaration below.")
    if section in {"all", "inputs", "outputs"}:
        for edge in view.edges:
            if edge.kind != "dependency" and (
                (section != "outputs" and edge.target == step)
                or (section != "inputs" and edge.source == step)
            ):
                lines.append(
                    f"  {edge.reference} -> {edge.target}.{edge.destination} [{edge.kind}]"
                )
        if section in {"all", "outputs"}:
            lines.append(
                "\nOutput schema:\n"
                + (
                    json.dumps(item.output_schema, indent=2)
                    if item.output_schema is not None
                    else "No declared JSON schema; see the declaration and output projection."
                )
            )
    if section in {"all", "body"} and item.kind == "foreach":
        lines.append("\nPer-item execution dependencies:")
        for child in view.steps:
            if child.parent == step:
                lines.append(f"  {', '.join(child.needs) or 'ITEM'} -> [{child.id}] {child.kind}")
    if section != "body":
        fields = item.declaration.copy()
        fields.pop("instruction", None)
        if section == "inputs":
            fields = {
                k: v
                for k, v in fields.items()
                if k
                in {"inputs", "args", "collection", "item_key", "proposal", "prompt", "message"}
            }
        elif section == "outputs":
            fields = {k: v for k, v in fields.items() if k in {"outputs", "expose_output"}}
        elif section == "policy":
            fields = {
                k: v
                for k, v in fields.items()
                if k
                in {
                    "when",
                    "retry",
                    "on_error",
                    "dependency_policy",
                    "max_iterations",
                    "max_items",
                    "max_parallel_items",
                    "on_item_error",
                }
            }
        elif section == "instructions" and item.instruction is not None:
            fields = {}
        if fields:
            lines.append("\nDeclaration:\n" + json.dumps(fields, ensure_ascii=False, indent=2))
    return "\n".join(lines)
