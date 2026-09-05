"""Validation and typed argument resolution for workflow-backed jobs."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ricky.agent.model_task import ModelTaskFailure, run_model_task
from ricky.agent.session import AgentSession
from ricky.builtins import bundled_workflows_dir
from ricky.config import RickySettings, profile_data_path
from ricky.jobs.spec import JobSpec
from ricky.jobs.types import JobRun
from ricky.llm import Provider, Usage
from ricky.profiles import BUNDLED_OWNER, ProfileResourceRef, ProfileScope
from ricky.tools import ToolRegistry
from ricky.workflows.compile import CompiledGraph, compile_workflow
from ricky.workflows.registry import (
    USER_WORKFLOWS_DIR,
    LoadedWorkflow,
    WorkflowRegistry,
    find_workflow_bundle,
    load_workflow_bundle,
    resolve_bundle_resource,
)
from ricky.workflows.schema import ResultSchema
from ricky.workflows.spec import (
    ApprovalStep,
    ModelTaskBase,
    WorkflowArg,
    WorkflowSpec,
    iter_steps,
    resolve_trigger_args,
    validate_arg_value,
)


class WorkflowJobPlan(BaseModel):
    """Validated exact workflow selection used by one job launch."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    qualified_name: str
    bundle_digest: str
    graph: CompiledGraph
    tool_names: tuple[str, ...]
    storage_scope: Literal["project", "user"]


class WorkflowArgumentResolution(BaseModel):
    """Validated invocation plus isolated resolver accounting."""

    model_config = ConfigDict(frozen=True)

    args: dict[str, JsonValue]
    usage: Usage = Field(default_factory=Usage)
    attempts: int = 0


def prepare_workflow_job(
    spec: JobSpec,
    *,
    workflow_registry: WorkflowRegistry | None,
    tool_registry: ToolRegistry,
    skill_names: set[str],
    settings: RickySettings,
) -> WorkflowJobPlan:
    """Compile and validate one workflow as an unattended job target."""

    target = spec.workflow
    if target is None:
        raise ValueError("job does not declare a workflow")
    if workflow_registry is None:
        raise ValueError("workflows are disabled")
    loaded = workflow_registry.loaded(target.name)
    if loaded is None:
        raise ValueError(f"job workflow is unknown or unavailable: {target.name}")
    compiled = compile_workflow(
        loaded.spec,
        tool_registry=tool_registry,
        settings=settings.workflow,
        skill_names=skill_names,
    )
    if compiled.graph is None:
        raise ValueError("invalid job workflow: " + "; ".join(compiled.errors))
    approval_steps = [
        step.id for step in iter_steps(loaded.spec.steps) if isinstance(step, ApprovalStep)
    ]
    if approval_steps:
        raise ValueError(
            "unattended job workflow contains interactive approval step(s): "
            + ", ".join(approval_steps)
        )
    unknown_args = sorted(set(target.args) - set(loaded.spec.args))
    if unknown_args:
        raise ValueError("job workflow declares unknown arg(s): " + ", ".join(unknown_args))
    for name, value in target.args.items():
        validate_arg_value(name, loaded.spec.args[name], value)
    tool_names = workflow_tool_names(loaded.spec)
    mutation_names = set(spec.permissions.allow_mutating)
    unknown_permissions = sorted(mutation_names - set(tool_names))
    if unknown_permissions:
        raise ValueError(
            "allow_mutating contains tools not used by the workflow: "
            + ", ".join(unknown_permissions)
        )
    return WorkflowJobPlan(
        qualified_name=loaded.resource.qualified,
        bundle_digest=workflow_bundle_digest(loaded),
        graph=compiled.graph,
        tool_names=tool_names,
        storage_scope="user",
    )


async def resolve_workflow_job_args(
    spec: JobSpec,
    *,
    plan: WorkflowJobPlan,
    goal: str,
    provider: Provider,
    session: AgentSession,
    prior_success: JobRun | None,
    settings: RickySettings,
) -> WorkflowArgumentResolution:
    """Resolve only job-omitted args, then validate the complete invocation."""

    target = spec.workflow
    assert target is not None
    declarations = plan.graph.spec.args
    omitted = {
        name: declaration for name, declaration in declarations.items() if name not in target.args
    }
    if not omitted:
        return WorkflowArgumentResolution(
            args=resolve_trigger_args(plan.graph.spec, target.args),
        )
    schema = ResultSchema(
        type="object",
        required=[name for name, declaration in omitted.items() if declaration.required],
        properties={name: _arg_schema(declaration) for name, declaration in omitted.items()},
    )
    resources = session.settings_snapshot.get("google_accounts")
    profile_definitions = session.settings_snapshot.get("profile_definitions")
    inputs: dict[str, JsonValue] = {
        "workflow": {
            "name": plan.qualified_name,
            "description": plan.graph.spec.description,
        },
        "locked_args": cast(dict[str, JsonValue], target.args),
        "unresolved_args": {
            name: cast(JsonValue, declaration.model_dump(mode="json", by_alias=True))
            for name, declaration in omitted.items()
        },
        "profile_scope": cast(JsonValue, session.profile_scope.model_dump(mode="json")),
        "profile_definitions": cast(JsonValue, profile_definitions or {}),
        "accessible_resources": cast(JsonValue, resources or {}),
        "prior_success": _prior_success_context(prior_success),
    }
    instruction = (
        "Resolve the omitted invocation arguments for this unattended workflow job. "
        "Use the job goal and supplied context. Return only omitted argument names; locked "
        "arguments are authoritative and cannot be replaced. Optional fields may be omitted "
        "when the workflow default is appropriate.\n\nJob goal:\n" + goal
    )
    try:
        result = await run_model_task(
            provider=provider,
            model=session.model,
            instruction=instruction,
            inputs=inputs,
            result_schema_name="workflow_job_args",
            result_schema=schema,
            max_attempts=max(1, min(spec.budget.iterations, settings.workflow.model_attempts)),
            max_result_chars=settings.workflow.max_result_chars,
        )
    except ModelTaskFailure as exc:
        raise ValueError(f"workflow argument resolution failed ({exc.category}): {exc}") from exc
    if not isinstance(result.output, dict):
        raise ValueError("workflow argument resolver returned a non-object")
    resolved = cast(dict[str, JsonValue], result.output)
    overlap = sorted(set(resolved) & set(target.args))
    if overlap:
        raise ValueError(
            "workflow argument resolver attempted to replace locked arg(s): " + ", ".join(overlap)
        )
    merged = {**target.args, **resolved}
    return WorkflowArgumentResolution(
        args=resolve_trigger_args(plan.graph.spec, merged),
        usage=result.usage,
        attempts=result.attempts,
    )


def workflow_bundle_digest(loaded: LoadedWorkflow) -> str:
    """Digest every executable file referenced by one workflow bundle."""

    source = (loaded.bundle_path / "workflow.toml").resolve()
    files: list[tuple[str, bytes]] = [("workflow.toml", source.read_bytes())]
    referenced = {
        step.instruction_file
        for step in iter_steps(loaded.spec.steps)
        if isinstance(step, ModelTaskBase) and step.instruction_file is not None
    }
    for relative in sorted(cast(set[str], referenced)):
        path = resolve_bundle_resource(loaded.bundle_path, relative)
        files.append((relative, path.read_bytes()))
    digest = hashlib.sha256()
    for relative, content in files:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def configured_workflow_bundle(
    spec: JobSpec,
    *,
    settings: RickySettings,
    profile_scope: ProfileScope,
) -> LoadedWorkflow | None:
    """Resolve one configured workflow with its exact qualified identity."""

    target = spec.workflow
    if target is None:
        return None
    bundle = find_workflow_bundle(
        target.name,
        settings=settings,
        profile_scope=profile_scope,
    )
    if bundle is None:
        raise ValueError(f"job workflow is unknown or unavailable: {target.name}")
    workflow_spec, errors = load_workflow_bundle(
        bundle,
        workflow_settings=settings.workflow,
    )
    if errors:
        raise ValueError("invalid job workflow bundle: " + "; ".join(errors))
    resolved_bundle = bundle.resolve()
    profile: str | None = None
    for candidate in profile_scope.profiles:
        if resolved_bundle.is_relative_to(
            (profile_data_path(settings, candidate) / USER_WORKFLOWS_DIR).resolve()
        ):
            profile = candidate
            break
    if profile is None and resolved_bundle.is_relative_to(bundled_workflows_dir().resolve()):
        profile = BUNDLED_OWNER
    if profile is None:
        raise ValueError(f"job workflow bundle is outside the configured roots: {bundle}")
    return LoadedWorkflow(
        spec=workflow_spec,
        bundle_path=bundle,
        resource=ProfileResourceRef(profile=profile, name=workflow_spec.name),
    )


def workflow_tool_names(spec: WorkflowSpec) -> tuple[str, ...]:
    """Return the exact ordered tool surface referenced by a workflow graph."""

    names: list[str] = []
    for step in iter_steps(spec.steps):
        tool = getattr(step, "tool", None)
        if isinstance(tool, str):
            names.append(tool)
        tools = getattr(step, "tools", None)
        if isinstance(tools, list):
            names.extend(name for name in tools if isinstance(name, str))
    return tuple(dict.fromkeys(names))


def _arg_schema(arg: WorkflowArg) -> ResultSchema:
    if arg.type == "string_list":
        return ResultSchema(
            type="array",
            items=ResultSchema(type="string", values=arg.values),
            min_length=arg.min_length,
            max_length=arg.max_length,
        )
    return ResultSchema(
        type=arg.type,
        values=arg.values,
        minimum=arg.minimum,
        maximum=arg.maximum,
        min_length=arg.min_length,
        max_length=arg.max_length,
    )


def _prior_success_context(run: JobRun | None) -> JsonValue:
    if run is None:
        return None
    return json.loads(
        json.dumps(
            {
                "job_run_id": run.id,
                "started_at": run.started_at.isoformat(),
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "workflow_run_id": run.workflow_run_id,
                "workflow_args": run.workflow_args,
                "final_message": (
                    run.final_message[:2_000] if run.final_message is not None else None
                ),
            },
            allow_nan=False,
        )
    )
