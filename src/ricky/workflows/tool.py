"""Model-callable workflow tools: start_workflow and validate_workflow."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ricky.tools.base import Risk, ToolContext, ToolResult
from ricky.tools.registry import ToolRegistry
from ricky.workflows.compile import compile_workflow
from ricky.workflows.describe import describe_workflow
from ricky.workflows.registry import (
    WorkflowRegistry,
    discover_workflows,
    find_workflow_bundle,
    load_workflow_bundle,
)
from ricky.workflows.run import WorkflowInvocation
from ricky.workflows.spec import resolve_trigger_args


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StartWorkflowParams(_Params):
    """Arguments for start_workflow."""

    name: str = Field(description="Workflow name to start.")
    args: dict[str, Any] = Field(
        default_factory=dict,
        description="Typed invocation arguments declared by the workflow.",
    )


class StartWorkflowTool:
    """Queue a discovered workflow to run after the current turn ends."""

    name: ClassVar[str] = "start_workflow"
    description: ClassVar[str] = (
        "Queue one loaded workflow by name. The workflow runs after this turn "
        "ends; its graph owns tool scope and order."
    )
    Params: ClassVar[type[BaseModel]] = StartWorkflowParams
    risk: ClassVar[Risk] = "mutating"
    capability_id = "builtin.automation.mutate"
    effect_kind = "ricky_state"
    unattended = "allowed"
    state_guard_id = None

    def __init__(self, workflow_registry: WorkflowRegistry) -> None:
        self._workflow_registry = workflow_registry

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        """Validate the invocation and queue it on the session."""
        parsed = StartWorkflowParams.model_validate(params)
        if ctx.session.active_workflow is not None:
            return ToolResult(
                content=(
                    f"Workflow '{ctx.session.active_workflow.name}' is already active or "
                    "queued; one workflow runs at a time."
                ),
                is_error=True,
            )
        loaded = self._workflow_registry.loaded(parsed.name)
        if loaded is None:
            return ToolResult(
                content=f"Unknown or unavailable workflow: {parsed.name}",
                is_error=True,
            )
        try:
            resolved_args = resolve_trigger_args(loaded.spec, parsed.args)
        except ValueError as exc:
            return ToolResult(
                content=f"{exc} Call start_workflow again with exactly the declared arg names.",
                is_error=True,
            )
        ctx.session.active_workflow = WorkflowInvocation(
            name=loaded.resource.qualified,
            args=resolved_args,
        )
        return ToolResult(
            content=(
                f"Workflow '{loaded.resource.qualified}' is queued and will run when "
                "this turn ends. "
                "Finish the turn now."
            )
        )


class ValidateWorkflowParams(_Params):
    """Arguments for validate_workflow."""

    name: str = Field(description="Local or profile-qualified workflow bundle name.")


class ValidateWorkflowTool:
    """Compile one workflow bundle fresh from disk and describe its graph."""

    name: ClassVar[str] = "validate_workflow"
    description: ClassVar[str] = (
        "Validate one workflow bundle by name: read it fresh from the workflow "
        "directories, then return every compile error or the compiled graph."
    )
    Params: ClassVar[type[BaseModel]] = ValidateWorkflowParams
    risk: ClassVar[Risk] = "read_only"
    capability_id = "builtin.automation.read"
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None

    def __init__(
        self,
        *,
        skill_names: set[str],
        tool_registry: ToolRegistry,
        workflow_registry: WorkflowRegistry | None = None,
    ) -> None:
        self._skill_names = set(skill_names)
        self._tool_registry = tool_registry
        self._workflow_registry = workflow_registry

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        """Parse and compile the named bundle; report all errors."""
        parsed = ValidateWorkflowParams.model_validate(params)
        try:
            bundle_path = find_workflow_bundle(
                parsed.name,
                settings=ctx.settings,
                profile_scope=ctx.session.profile_scope,
            )
        except ValueError as exc:
            return ToolResult(
                content=f"Workflow '{parsed.name}' is invalid:\n{exc}",
                is_error=True,
            )
        if bundle_path is None:
            return ToolResult(
                content=(
                    f"No workflow bundle named '{parsed.name}' under the "
                    "accessible profile workflow roots or the bundled workflows."
                ),
                is_error=True,
            )
        try:
            spec, file_errors = load_workflow_bundle(
                bundle_path, workflow_settings=ctx.settings.workflow
            )
        except ValueError as exc:
            return ToolResult(content=f"Workflow '{parsed.name}' is invalid:\n{exc}")
        compiled = compile_workflow(
            spec,
            tool_registry=self._tool_registry,
            skill_names=self._skill_names,
            settings=ctx.settings.workflow,
        )
        errors = [*compiled.errors, *file_errors]
        if errors:
            return ToolResult(content=f"Workflow '{parsed.name}' is invalid:\n" + "\n".join(errors))
        assert compiled.graph is not None
        description = describe_workflow(compiled.graph)
        if self._workflow_registry is not None and ctx.session.active_workflow is None:
            refreshed = discover_workflows(
                settings=ctx.settings,
                profile_scope=ctx.session.profile_scope,
                skill_names=self._skill_names,
                workflow_settings=ctx.settings.workflow,
                tool_registry=self._tool_registry,
            )
            if refreshed.get(parsed.name) is None:
                return ToolResult(
                    content=(
                        f"Workflow '{parsed.name}' changed or became unavailable "
                        "while the registry was refreshing; validate it again."
                    ),
                    is_error=True,
                )
            self._workflow_registry.replace_with(refreshed)
        return ToolResult(content=f"{description}\n\nvalid")
