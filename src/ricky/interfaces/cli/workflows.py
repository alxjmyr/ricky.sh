"""Workflow discovery, validation, execution, and recovery commands."""

from __future__ import annotations

import hashlib
import json
import tempfile
import tomllib
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Literal

import typer

from ricky.agent import AgentSession
from ricky.agent.events import WorkflowEvent
from ricky.agent.workflow import WorkflowRunner
from ricky.builtins import bundled_workflows_dir
from ricky.config import RickySettings, load_settings
from ricky.durable_tasks.tools import durable_task_policy
from ricky.interfaces.cli.errors import run_with_provider_errors
from ricky.interfaces.cli.render import CliRenderer
from ricky.llm import create_provider
from ricky.permissions import PermissionEngine
from ricky.profiles import ProfileResourceRef, ProfileScope
from ricky.runtime import CapabilityRuntime, build_capability_runtime
from ricky.skills.registry import SkillRegistry, discover_skills
from ricky.tools import ToolRegistry
from ricky.tools.integrations.gcal import gcal_toolset
from ricky.tools.integrations.gmail import gmail_toolset
from ricky.tools.integrations.google import GoogleAuth
from ricky.tools.integrations.slack import slack_toolset
from ricky.tools.integrations.web_search import web_search_toolset
from ricky.workflows import (
    AgentStep,
    ModelStep,
    WorkflowRegistry,
    WorkflowSpec,
    compile_workflow,
    describe_workflow,
    find_workflow_bundle,
    iter_steps,
    load_workflow_bundle,
    resolve_trigger_args,
)
from ricky.workflows.run import WorkflowRun, WorkflowSourceIdentity
from ricky.workflows.run_store import WorkflowRunStore

_PROFILE_OPTION = typer.Option(
    None,
    "--profile",
    help="Primary profile; defaults to the configured default profile.",
)

_ACCESS_PROFILE_OPTION = typer.Option(
    None,
    "--access-profile",
    help="Additional accessible profile; repeatable.",
)

_RUN_ARGS_OPTION = typer.Option(
    [],
    "--args",
    "-a",
    help="Typed trigger arguments as key=JSON; repeatable.",
)

_DRYRUN_ARGS_OPTION = typer.Option(
    [],
    "--args",
    "-a",
    help="Trigger arguments as key=value; repeatable, each value may hold several pairs.",
)

_DRYRUN_FIXTURES_OPTION = typer.Option(
    None,
    "--fixtures",
    help=(
        "TOML file of canned step completions, keyed by step id or "
        "step-id[index] for one foreach item."
    ),
)


def workflow_list(
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """List discovered workflows and any load errors."""
    run_with_provider_errors(
        lambda renderer: _workflow_list(profile, access_profiles or [], renderer)
    )


def workflow_validate(
    name: str = typer.Argument(..., help="Workflow bundle name."),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Run the workflow linter and print every error."""
    run_with_provider_errors(
        lambda renderer: _workflow_validate(name, profile, access_profiles or [], renderer)
    )


def workflow_show(
    name: str = typer.Argument(..., help="Workflow name."),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Render the workflow's FSM: steps and the transition table."""
    run_with_provider_errors(
        lambda renderer: _workflow_show(name, profile, access_profiles or [], renderer)
    )


def workflow_run(
    name: str = typer.Argument(..., help="workflow name."),
    args: list[str] = _RUN_ARGS_OPTION,
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Run one workflow and persist recoverable checkpoints."""

    run_with_provider_errors(
        lambda renderer: _workflow_run(name, args, profile, access_profiles or [], renderer)
    )


def workflow_status(
    run_id: str = typer.Argument(..., help="Workflow run id."),
    scope: str = typer.Option(
        "user",
        help="Stored run scope. Use 'project' only for runs recorded before bundled discovery.",
    ),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Show one persisted workflow run."""

    run_with_provider_errors(
        lambda renderer: _workflow_status_v2(
            run_id, scope, profile, access_profiles or [], renderer
        )
    )


def workflow_resume(
    run_id: str = typer.Argument(..., help="Workflow run id."),
    scope: str = typer.Option(
        "user",
        help="Stored run scope. Use 'project' only for runs recorded before bundled discovery.",
    ),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Resume safe incomplete work from a workflow checkpoint."""

    run_with_provider_errors(
        lambda renderer: _workflow_resume_v2(
            run_id, scope, profile, access_profiles or [], renderer
        )
    )


def workflow_abandon(
    run_id: str = typer.Argument(..., help="Workflow run id."),
    scope: str = typer.Option(
        "user",
        help="Stored run scope. Use 'project' only for runs recorded before bundled discovery.",
    ),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Mark one persisted workflow run abandoned."""

    run_with_provider_errors(
        lambda renderer: _workflow_abandon_v2(
            run_id, scope, profile, access_profiles or [], renderer
        )
    )


def workflow_reconcile(
    run_id: str = typer.Argument(..., help="Workflow run id."),
    execution_address: str = typer.Argument(..., help="In-doubt effect address."),
    completed: bool = typer.Option(
        ...,
        "--completed/--not-completed",
        help="State whether the external effect completed.",
    ),
    scope: str = typer.Option(
        "user",
        help="Stored run scope. Use 'project' only for runs recorded before bundled discovery.",
    ),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Resolve one in-doubt effect from an explicit user fact."""

    run_with_provider_errors(
        lambda renderer: _workflow_reconcile_v2(
            run_id,
            execution_address,
            completed,
            scope,
            profile,
            access_profiles or [],
            renderer,
        )
    )


def workflow_dryrun(
    name: str = typer.Argument(..., help="Workflow name."),
    args: list[str] = _DRYRUN_ARGS_OPTION,
    fixtures: Path | None = _DRYRUN_FIXTURES_OPTION,
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Run the workflow with no side effect: mutating tools and checks are logged."""
    run_with_provider_errors(
        lambda renderer: _workflow_dryrun(
            name, args, fixtures, profile, access_profiles or [], renderer
        )
    )


async def _workflow_list(
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    async with AsyncExitStack() as resources:
        runtime = await _workflow_context(settings, resources, profile_scope=profile_scope)
        renderer.render_workflow_list(runtime.workflow_registry or WorkflowRegistry())


async def _workflow_validate(
    name: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    runtime_settings = settings.resolve_profile_runtime_settings(profile_scope)
    async with AsyncExitStack() as resources:
        runtime = await _workflow_context(settings, resources, profile_scope=profile_scope)
        bundle_path = find_workflow_bundle(
            name,
            settings=settings,
            profile_scope=profile_scope,
        )
        if bundle_path is None:
            renderer.render_error(
                f"No workflow bundle named '{name}' under the accessible "
                "profile workflow roots or the bundled workflows."
            )
            raise typer.Exit(1)
        try:
            spec, file_errors = load_workflow_bundle(
                bundle_path, workflow_settings=runtime_settings.workflow
            )
        except ValueError as exc:
            renderer.render_error(str(exc))
            raise typer.Exit(1) from exc
        compiled = compile_workflow(
            spec,
            tool_registry=runtime.full_registry,
            skill_names=runtime.skill_registry.identifiers(),
            settings=runtime_settings.workflow,
        )
        errors = [*compiled.errors, *file_errors]
        if errors:
            for error in errors:
                renderer.render_error(error)
            raise typer.Exit(1)
        renderer.render_status(f"Workflow '{name}' is valid.", style="green")


async def _workflow_show(
    name: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    runtime_settings = settings.resolve_profile_runtime_settings(profile_scope)
    async with AsyncExitStack() as resources:
        runtime = await _workflow_context(settings, resources, profile_scope=profile_scope)
        spec = (
            runtime.workflow_registry.get(name) if runtime.workflow_registry is not None else None
        )
        if spec is None:
            renderer.render_error(
                f"Workflow '{name}' is not loaded. Run 'ricky workflow list' for "
                "loaded workflows and load errors, or 'ricky workflow validate "
                f"{name}' for its lint report."
            )
            raise typer.Exit(1)
        compiled = compile_workflow(
            spec,
            tool_registry=runtime.full_registry,
            skill_names=runtime.skill_registry.identifiers(),
            settings=runtime_settings.workflow,
        )
        if compiled.graph is None:
            for error in compiled.errors:
                renderer.render_error(error)
            raise typer.Exit(1)
        renderer.render_workflow_show(describe_workflow(compiled.graph))


async def _workflow_run(
    name: str,
    arg_tokens: list[str],
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    runtime_settings = settings.resolve_profile_runtime_settings(profile_scope)
    async with AsyncExitStack() as resources:
        runtime = await _workflow_context(settings, resources, profile_scope=profile_scope)
        loaded = (
            runtime.workflow_registry.loaded(name)
            if runtime.workflow_registry is not None
            else None
        )
        if loaded is None:
            renderer.render_error(f"Workflow '{name}' is not a loaded workflow.")
            raise typer.Exit(1)
        args = _parse_workflow_args(arg_tokens)
        resolve_trigger_args(loaded.spec, args)
        compiled = compile_workflow(
            loaded.spec,
            tool_registry=runtime.full_registry,
            skill_names=runtime.skill_registry.identifiers(),
            settings=runtime_settings.workflow,
        )
        if compiled.graph is None:
            for error in compiled.errors:
                renderer.render_error(error)
            raise typer.Exit(1)
        selection = settings.resolve_profile_selection(profile_scope)
        provider = None
        if _workflow_needs_provider(loaded.spec):
            provider = create_provider(
                selection.provider,
                runtime_settings,
            )
            resources.push_async_callback(provider.aclose)
        session = AgentSession.create(
            settings,
            profile_scope=profile_scope,
            provider=selection.provider,
            model=selection.model,
        )
        resources.push_async_callback(runtime.durable_tasks.release_session_leases, session.id)
        store = WorkflowRunStore(settings)
        runner = WorkflowRunner(
            graph=compiled.graph,
            provider=provider,
            tool_registry=runtime.full_registry,
            settings=runtime_settings,
            session=session,
            source=_workflow_source(loaded.bundle_path, loaded.resource),
            permission_engine=PermissionEngine(durable_task_policy()),
            permission_responder=renderer.request_permission,
            approval_responder=renderer.request_workflow_approval,
            emit_event=_event_renderer(renderer),
            checkpoint=store.save,
            skill_bodies=_workflow_skill_bodies(loaded.spec, runtime.skill_registry),
        )
        run = await runner.start(args)
        renderer.render_status(
            f"Workflow run {run.id}: {run.status}",
            style="green" if run.status == "completed" else "yellow",
        )
        if run.status not in {"completed", "completed_with_errors"}:
            raise typer.Exit(1)


async def _workflow_status_v2(
    run_id: str,
    scope: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    run = await WorkflowRunStore(settings).load(
        run_id,
        profile_scope=profile_scope,
        scope=_run_scope(scope),
    )
    renderer.render_workflow_show(_describe_run_v2(run))


async def _workflow_resume_v2(
    run_id: str,
    scope: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    run_scope = _run_scope(scope)
    store = WorkflowRunStore(settings)
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    run = await store.load(run_id, scope=run_scope, profile_scope=profile_scope)
    runtime_settings = settings.resolve_profile_runtime_settings(run.profile_scope)
    async with AsyncExitStack() as resources:
        runtime = await _workflow_context(
            settings,
            resources,
            profile_scope=run.profile_scope,
        )
        loaded = (
            runtime.workflow_registry.loaded(run.source.resource.qualified)
            if runtime.workflow_registry is not None
            else None
        )
        if loaded is None:
            raise ValueError(f"cannot resume: workflow '{run.workflow_name}' is not loaded")
        compiled = compile_workflow(
            loaded.spec,
            tool_registry=runtime.full_registry,
            skill_names=runtime.skill_registry.identifiers(),
            settings=runtime_settings.workflow,
        )
        if compiled.graph is None:
            raise ValueError("cannot resume invalid workflow: " + "; ".join(compiled.errors))
        provider = None
        if _workflow_needs_provider(loaded.spec):
            provider = create_provider(
                run.provider,
                runtime_settings,
            )
            resources.push_async_callback(provider.aclose)
        session = AgentSession.create(
            settings,
            profile_scope=run.profile_scope,
            provider=run.provider,
            model=run.model,
        )
        resources.push_async_callback(runtime.durable_tasks.release_session_leases, session.id)
        runner = WorkflowRunner(
            graph=compiled.graph,
            provider=provider,
            tool_registry=runtime.full_registry,
            settings=runtime_settings,
            session=session,
            source=_workflow_source(loaded.bundle_path, loaded.resource),
            permission_engine=PermissionEngine(durable_task_policy()),
            permission_responder=renderer.request_permission,
            approval_responder=renderer.request_workflow_approval,
            emit_event=_event_renderer(renderer),
            checkpoint=store.save,
            skill_bodies=_workflow_skill_bodies(loaded.spec, runtime.skill_registry),
        )
        resumed = await runner.resume(run)
        renderer.render_status(f"Workflow run {resumed.id}: {resumed.status}")
        if resumed.status not in {"completed", "completed_with_errors"}:
            raise typer.Exit(1)


async def _workflow_abandon_v2(
    run_id: str,
    scope: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    run = await WorkflowRunStore(settings).abandon(
        run_id,
        profile_scope=profile_scope,
        scope=_run_scope(scope),
    )
    renderer.render_status(f"Workflow run {run.id}: abandoned", style="yellow")


async def _workflow_reconcile_v2(
    run_id: str,
    execution_address: str,
    completed: bool,
    scope: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    run = await WorkflowRunStore(settings).reconcile(
        run_id,
        execution_address,
        completed=completed,
        profile_scope=profile_scope,
        scope=_run_scope(scope),
    )
    entry = next(
        value for value in run.effect_journal if value.execution_address == execution_address
    )
    renderer.render_event(
        WorkflowEvent(
            action="effect_reconciled",
            run_id=run.id,
            workflow_name=run.workflow_name,
            step_id=entry.step_id,
            execution_address=entry.execution_address,
            details={"completed": completed, "journal_status": entry.status},
        )
    )
    fact = "completed" if completed else "not completed"
    renderer.render_status(
        f"Reconciled {execution_address} as {fact}. Run {run.id} is ready to resume.",
        style="yellow",
    )


async def _workflow_dryrun(
    name: str,
    arg_tokens: list[str],
    fixtures_path: Path | None,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    runtime_settings = settings.resolve_profile_runtime_settings(profile_scope)
    async with AsyncExitStack() as resources:
        runtime = await _workflow_context(settings, resources, profile_scope=profile_scope)
        loaded = (
            runtime.workflow_registry.loaded(name)
            if runtime.workflow_registry is not None
            else None
        )
        if loaded is None:
            renderer.render_error(f"Workflow '{name}' is not loaded.")
            raise typer.Exit(1)
        trigger_args = _parse_workflow_args(arg_tokens)
        resolve_trigger_args(loaded.spec, trigger_args)
        fixtures = _load_fixtures(fixtures_path) if fixtures_path is not None else {}
        compiled = compile_workflow(
            loaded.spec,
            tool_registry=runtime.full_registry,
            skill_names=runtime.skill_registry.identifiers(),
            settings=runtime_settings.workflow,
        )
        if compiled.graph is None:
            raise ValueError("cannot dry-run invalid workflow: " + "; ".join(compiled.errors))
        provider = None
        model_addresses = {
            step.id
            for step in iter_steps(loaded.spec.steps)
            if isinstance(step, ModelStep | AgentStep)
        }
        fixture_steps = {address.rsplit("/", 1)[-1] for address in fixtures}
        if model_addresses - fixture_steps:
            selection = settings.resolve_profile_selection(profile_scope)
            provider = create_provider(
                selection.provider,
                runtime_settings,
            )
            resources.push_async_callback(provider.aclose)
        with tempfile.TemporaryDirectory(prefix="ricky-workflow-dryrun-") as run_root:
            dry_settings = settings.model_copy(update={"project_data_dir": run_root})
            session = AgentSession.create(dry_settings, profile_scope=profile_scope)
            resources.push_async_callback(runtime.durable_tasks.release_session_leases, session.id)
            store = WorkflowRunStore(dry_settings)
            runner = WorkflowRunner(
                graph=compiled.graph,
                provider=provider,
                tool_registry=runtime.full_registry,
                settings=runtime_settings,
                session=session,
                source=_workflow_source(loaded.bundle_path, loaded.resource),
                permission_engine=PermissionEngine(durable_task_policy()),
                permission_responder=renderer.request_permission,
                approval_responder=renderer.preview_and_deny_workflow_approval,
                emit_event=_event_renderer(renderer),
                checkpoint=store.save,
                skill_bodies=_workflow_skill_bodies(loaded.spec, runtime.skill_registry),
                fixtures=fixtures,
                dry_run=True,
            )
            run = await runner.start(trigger_args)
        renderer.render_workflow_dryrun(name, run.status, None)
        if run.status not in {"completed", "completed_with_errors"}:
            raise typer.Exit(1)


async def _workflow_context(
    settings: RickySettings,
    resources: AsyncExitStack,
    *,
    profile_scope: ProfileScope | None = None,
) -> CapabilityRuntime:
    """Build the tool pool, skill registry, and workflow registry for CLI commands."""
    profile_scope = profile_scope or settings.resolve_profile_scope()
    selection = settings.resolve_profile_selection(profile_scope)
    session = AgentSession.create(
        settings,
        profile_scope=profile_scope,
        provider=selection.provider,
        model=selection.model,
    )
    return await resources.enter_async_context(
        build_capability_runtime(
            settings,
            session=session,
            slack_factory=slack_toolset,
            gmail_factory=gmail_toolset,
            gcal_factory=gcal_toolset,
            web_search_factory=web_search_toolset,
            google_auth_factory=GoogleAuth,
            skill_factory=discover_skills,
            registry_factory=ToolRegistry,
        )
    )


def _parse_workflow_args(tokens: list[str]) -> dict[str, Any]:
    """Parse repeated key=JSON workflow arguments without type coercion."""

    result: dict[str, Any] = {}
    for token in " ".join(tokens).split():
        key, separator, raw = token.partition("=")
        if not separator or not key:
            raise ValueError(f"Workflow args must be key=value pairs; got: {token}")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        result[key] = value
    return result


def _load_fixtures(path: Path) -> dict[str, Any]:
    """Load typed workflow outputs keyed by execution address."""

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read workflow fixtures file {path}: {exc}") from exc
    entries = raw.get("fixtures", raw)
    if not isinstance(entries, dict):
        raise ValueError("workflow fixtures must be a table keyed by execution address")
    fixtures: dict[str, Any] = {}
    for address, declaration in entries.items():
        if not isinstance(address, str) or not address:
            raise ValueError("workflow fixture addresses must be non-empty strings")
        if not isinstance(declaration, dict) or set(declaration) != {"output"}:
            raise ValueError(
                f"workflow fixture {address!r} must be a table with only an output field"
            )
        fixtures[address] = declaration["output"]
    return fixtures


def _workflow_source(
    bundle_path: Path,
    resource: ProfileResourceRef,
) -> WorkflowSourceIdentity:
    """Build the stable source identity for one loaded workflow bundle."""

    source = (bundle_path / "workflow.toml").resolve()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    scope: Literal["project", "user", "bundled", "fixture"] = (
        "bundled" if source.is_relative_to(bundled_workflows_dir()) else "user"
    )
    return WorkflowSourceIdentity(
        path=str(source),
        scope=scope,
        content_digest=digest,
        resource=resource,
    )


def _workflow_skill_bodies(spec: WorkflowSpec, skill_registry: SkillRegistry) -> dict[str, str]:
    """Load only skills explicitly named by this workflow."""

    names = {
        step.skill
        for step in iter_steps(spec.steps)
        if isinstance(step, ModelStep | AgentStep) and step.skill is not None
    }
    bodies: dict[str, str] = {}
    for name in names:
        skill = skill_registry.get(name)
        if skill is None:
            raise ValueError(f"declared skill is unavailable: {name}")
        bodies[name] = skill.body
    return bodies


def _workflow_needs_provider(spec: WorkflowSpec) -> bool:
    return any(isinstance(step, ModelStep | AgentStep) for step in iter_steps(spec.steps))


def _run_scope(value: str) -> Literal["project", "user"]:
    if value == "project":
        return "project"
    if value == "user":
        return "user"
    raise ValueError("run scope must be 'project' or 'user'")


def _event_renderer(renderer: CliRenderer):
    async def emit(event: Any) -> None:
        renderer.render_event(event)

    return emit


def _describe_run_v2(run: WorkflowRun) -> str:
    """Render stable run and step state without private model context."""

    lines = [
        f"run: {run.id}",
        f"workflow: {run.workflow_name} (v2)",
        f"status: {run.status}",
        f"provider: {run.provider}",
        f"model: {run.model}",
        f"fingerprint: {run.graph_fingerprint}",
        "steps:",
    ]
    for record in run.steps.values():
        detail = f" error={record.error.category}" if record.error is not None else ""
        lines.append(f"  - {record.execution_address}: {record.kind} {record.status}{detail}")
    if run.item_runs:
        lines.append("items:")
        for step_id, items in run.item_runs.items():
            for item in items:
                lines.append(f"  - {step_id}/{item.key}: {item.status}")
    if run.effect_journal:
        lines.append("effects:")
        for entry in run.effect_journal:
            lines.append(f"  - {entry.execution_address}: {entry.tool_name} {entry.status}")
    return "\n".join(lines)


def register_workflow_commands(workflow_app: typer.Typer) -> None:
    """Register the commands on the application composition root."""

    workflow_app.command("list")(workflow_list)
    workflow_app.command("validate")(workflow_validate)
    workflow_app.command("show")(workflow_show)
    workflow_app.command("run")(workflow_run)
    workflow_app.command("status")(workflow_status)
    workflow_app.command("resume")(workflow_resume)
    workflow_app.command("abandon")(workflow_abandon)
    workflow_app.command("reconcile")(workflow_reconcile)
    workflow_app.command("dryrun")(workflow_dryrun)
