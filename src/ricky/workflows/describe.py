"""Stable plain-text inspection for compiled Workflow graphs."""

from __future__ import annotations

import json

from ricky.workflows.compile import CompiledGraph
from ricky.workflows.spec import (
    AgentStep,
    DataStep,
    ForeachStep,
    ModelTaskBase,
    Step,
    ToolStep,
)


def describe_workflow(graph: CompiledGraph) -> str:
    """Show roots, dependencies, typed outputs, retry, and nested graphs."""

    spec = graph.spec
    lines = [
        f"workflow: {spec.name}",
        "version: 2",
        f"description: {spec.description}",
        f"roots: {', '.join(graph.roots)}",
    ]
    if spec.args:
        lines.append("args:")
        for name, arg in spec.args.items():
            state = "required" if arg.required else f"default: {arg.default!r}"
            lines.append(f"  {name} ({arg.type}, {state}): {arg.description}")
    if spec.schemas:
        lines.append("schemas: " + ", ".join(spec.schemas))
    lines.append("")
    for step in spec.steps:
        lines.extend(_describe_step(step, graph, indent=""))
    return "\n".join(lines).rstrip()


def _describe_step(step: Step, graph: CompiledGraph, *, indent: str) -> list[str]:
    lines = [f"{indent}[{step.id}] {step.kind}"]
    lines.append(f"{indent}  needs: {', '.join(step.needs) if step.needs else '(root)'}")
    lines.append(f"{indent}  dependency_policy: {step.dependency_policy}")
    lines.append(f"{indent}  on_error: {step.on_error}")
    if step.when is not None:
        operand = "" if step.when.value is None else f" {step.when.value!r}"
        lines.append(f"{indent}  when: {step.when.ref} {step.when.operator}{operand}")
    if isinstance(step, ToolStep):
        lines.append(f"{indent}  tool: {step.tool}")
        lines.append(f"{indent}  risk: {graph.tool_risks.get(step.tool, 'unknown')}")
        lines.append(f"{indent}  effect: {graph.tool_effect_kinds.get(step.tool, 'unknown')}")
        lines.append(
            f"{indent}  output: "
            + (graph.tool_result_schemas.get(step.tool, {}).get("title") or "ignored")
        )
    if isinstance(step, ModelTaskBase):
        lines.append(f"{indent}  result_schema: {step.result_schema}")
        if isinstance(step, AgentStep):
            lines.append(f"{indent}  tools: {', '.join(step.tools) or '(none)'}")
    if isinstance(step, DataStep):
        lines.append(f"{indent}  operator: {step.operator}")
        result = graph.operator_result_schemas.get(step.operator, {})
        lines.append(f"{indent}  output: {result.get('title', 'unknown')}")
    if step.retry.max_attempts > 1:
        lines.append(f"{indent}  retry: {step.retry.max_attempts} on " + ", ".join(step.retry.on))
    if isinstance(step, ForeachStep):
        lines.append(f"{indent}  body roots: {', '.join(graph.body_roots[step.id])}")
        lines.append(f"{indent}  on_item_error: {step.on_item_error}")
        if step.outputs:
            outputs = {name: value.model_dump(mode="json") for name, value in step.outputs.items()}
            lines.append(f"{indent}  outputs: {json.dumps(outputs, sort_keys=True)}")
        else:
            lines.append(f"{indent}  outputs: full item records")
        lines.append(f"{indent}  body:")
        for child in step.body:
            lines.extend(_describe_step(child, graph, indent=f"{indent}    "))
    if isinstance(step, ToolStep | DataStep) and step.args:
        args = {name: value.model_dump(mode="json") for name, value in step.args.items()}
        lines.append(f"{indent}  args: {json.dumps(args, sort_keys=True)}")
    return lines
