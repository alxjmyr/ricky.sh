"""Conversational design inspection and HTML export tools."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from ricky.tools.base import Risk, ToolContext, ToolResult
from ricky.tools.registry import ToolRegistry
from ricky.workflows.inspection import inspect_workflow, render_ascii
from ricky.workflows.visualization import export_visualization


class InspectWorkflowParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(description="Workflow name, preferably profile-qualified.")
    step: str | None = Field(
        default=None, description="Exact step id to expand; omit for overview."
    )
    section: Literal["all", "instructions", "inputs", "outputs", "policy", "body"] = "all"


class RenderWorkflowParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(description="Workflow name to export as an interactive HTML design document.")


class InspectWorkflowTool:
    name: ClassVar[str] = "inspect_workflow"
    description: ClassVar[str] = (
        "Inspect a workflow design fresh from disk without executing it. Returns a compact "
        "ASCII dependency graph, or exact step instructions, skill guidance, input bindings, "
        "output schemas, consumers, policies or foreach body when a step is specified."
    )
    Params: ClassVar[type[BaseModel]] = InspectWorkflowParams
    risk: ClassVar[Risk] = "read_only"
    capability_id = "builtin.automation.read"
    effect_kind = "none"
    unattended = "allowed"
    review_mode = "policy"
    state_guard_id = None

    def __init__(self, tool_registry: ToolRegistry) -> None:
        self._tools = tool_registry

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        parsed = InspectWorkflowParams.model_validate(params)
        try:
            view = inspect_workflow(
                parsed.name,
                settings=ctx.settings,
                scope=ctx.session.profile_scope,
                tools=self._tools,
            )
            content = render_ascii(view, step=parsed.step, section=parsed.section)
        except (ValueError, OSError) as exc:
            return ToolResult(content=str(exc), is_error=True)
        return ToolResult(content=content)


class RenderWorkflowTool:
    name: ClassVar[str] = "render_workflow"
    description: ClassVar[str] = (
        "Export a workflow design as a self-contained interactive HTML blueprint. "
        "Includes resolved instructions, skills, schemas and data bindings. Returns a local "
        "file path; does not execute the workflow, open a browser or send the file."
    )
    Params: ClassVar[type[BaseModel]] = RenderWorkflowParams
    risk: ClassVar[Risk] = "mutating"
    capability_id = "builtin.automation.mutate"
    effect_kind = "ricky_state"
    unattended = "allowed"
    review_mode = "policy"
    state_guard_id = None

    def __init__(self, tool_registry: ToolRegistry) -> None:
        self._tools = tool_registry

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        parsed = RenderWorkflowParams.model_validate(params)
        try:
            view = inspect_workflow(
                parsed.name,
                settings=ctx.settings,
                scope=ctx.session.profile_scope,
                tools=self._tools,
            )
            path = export_visualization(
                view, settings=ctx.settings, scope=ctx.session.profile_scope
            )
        except (ValueError, OSError) as exc:
            return ToolResult(content=str(exc), is_error=True)
        return ToolResult(
            content=(
                f"Workflow blueprint: {view.identity}\nDesign: {view.fingerprint[:10]}\n"
                f"HTML file: {path}\n"
                "Open on this host with: ricky workflow open-view <HTML-file> <scope-flags>. "
                "Use the exact path and this session's --profile / --access-profile flags. "
                "The file can also be sent as an attachment through an authorized messaging tool."
            )
        )
