"""Provider-free capability inventory and policy commands."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, Literal

import typer

from ricky.agent.session import AgentSession
from ricky.browser import BrowserService
from ricky.capabilities import (
    CapabilityDiagnostic,
    CapabilityRegistry,
    build_capability_registry,
    registered_skill_owners,
    resolve_capability_policy,
    validate_capability_inventory,
    validate_capability_policy,
    validate_foreground_live_policy,
)
from ricky.config import RickySettings, find_project_root, load_settings
from ricky.durable_tasks.state_guard import DurableTaskStateGuard
from ricky.gateway.tools import (
    gateway_capability_inventory_tools,
    gateway_control_descriptors,
)
from ricky.interfaces.cli.render import CliRenderer
from ricky.runtime.composition import (
    CAPABILITY_SPECS,
    CapabilityRuntime,
    build_capability_runtime,
)
from ricky.tools import StateGuardRegistry

CapabilityAgent = Literal["gateway_foreground", "ad_hoc_background"]
_AGENT_OPTION = typer.Option("ad_hoc_background", "--agent")
_PROJECT_OPTION = typer.Option(None, "--project")


def register_capability_commands(app: typer.Typer) -> None:
    @app.command("list")
    def capability_list(
        agent: CapabilityAgent = _AGENT_OPTION,
        project: Path | None = _PROJECT_OPTION,
    ) -> None:
        """List actual installed capabilities and resolved standing eligibility."""

        _run(lambda renderer: _list(agent, project, renderer))

    @app.command("show")
    def capability_show(
        capability_id: str = typer.Argument(...),
        project: Path | None = _PROJECT_OPTION,
    ) -> None:
        """Show exact resources, provenance, risk, guardrail, and policy."""

        _run(lambda renderer: _show(capability_id, project, renderer))

    @app.command("validate")
    def capability_validate(
        project: Path | None = _PROJECT_OPTION,
    ) -> None:
        """Validate mappings and policy; see .designs/tool-authoring.md."""

        _run(lambda renderer: _validate(project, renderer))


def _run(factory: Callable[[CliRenderer], Coroutine[Any, Any, None]]) -> None:
    renderer = CliRenderer()
    try:
        asyncio.run(factory(renderer))
    except (OSError, ValueError, RuntimeError) as exc:
        renderer.render_error(f"Capability error: {exc}")
        raise typer.Exit(1) from exc


def _with_inventory(project: Path | None):
    settings = load_settings()
    root = find_project_root(project)
    profile_scope = settings.resolve_profile_scope()
    selection = settings.resolve_profile_selection(profile_scope)
    session = AgentSession.create(
        settings,
        profile_scope=profile_scope,
        provider=selection.provider,
        model=selection.model,
    )
    return settings.resolve_profile_runtime_settings(profile_scope), root, session


async def _list(
    agent: CapabilityAgent,
    project: Path | None,
    renderer: CliRenderer,
) -> None:
    settings, root, session = _with_inventory(project)
    async with build_capability_runtime(settings, session=session, project_root=root) as runtime:
        registry = _complete_registry(runtime)
        base = _agent_policy(settings, agent)
        decisions = {
            item.capability_id: item
            for item in resolve_capability_policy(
                registry,
                base,
                require_unattended=agent == "ad_hoc_background",
            )
        }
        lines = []
        for definition in registry.definitions():
            decision = decisions[definition.id]
            requirements = []
            if decision.confirmation_required:
                requirements.append("confirmation")
            if decision.guardrail_required:
                requirements.append("guardrail")
            lines.append(
                f"{definition.id} v{definition.version} "
                f"{'eligible' if decision.eligible else 'excluded'} "
                f"risk={definition.risk_class} resources={len(definition.resources)}"
                + (f" requires={','.join(requirements)}" if requirements else "")
            )
        renderer.render_status("\n".join(lines) or "No capabilities installed.", style="")


async def _show(
    capability_id: str,
    project: Path | None,
    renderer: CliRenderer,
) -> None:
    settings, root, session = _with_inventory(project)
    async with build_capability_runtime(settings, session=session, project_root=root) as runtime:
        registry = _complete_registry(runtime)
        definition = registry.require(capability_id)
        evaluator = runtime.guardrail_registry.get(capability_id)
        lines = [
            f"capability: {definition.id}",
            f"version: {definition.version}",
            f"owner: {definition.owner}",
            f"kind: {definition.kind}",
            f"risk: {definition.risk_class}",
            f"unattended eligible: {definition.unattended_eligible}",
            f"description: {definition.description}",
            "resources:",
        ]
        lines.extend(
            f"  - {item.kind}/{item.id} v{item.contract_version} "
            f"risk={item.risk_class or '-'} effect={item.effect_kind or '-'} "
            f"unattended={item.unattended or '-'} guard={item.state_guard_id or '-'} "
            f"digest={item.digest} provenance={item.provenance}"
            for item in definition.resources
        )
        if evaluator is not None:
            required = tuple(field.name for field in evaluator.intake_spec.fields if field.required)
            lines.append(
                f"guardrail: {evaluator.schema_id} v{evaluator.schema_version}; "
                f"required fields={', '.join(required) if required else 'evaluator-defined'}"
            )
            lines.extend(
                f"  - {field.name}: {field.value_type}"
                + (f" ({field.format})" if field.format is not None else "")
                + f" — {field.description}"
                for field in evaluator.intake_spec.fields
            )
        elif definition.guardrail_schema_id is not None:
            lines.append(f"guardrail: {definition.guardrail_schema_id} (evaluator missing)")
        for agent in ("gateway_foreground", "ad_hoc_background"):
            base = _agent_policy(settings, agent)
            decision = next(
                item
                for item in resolve_capability_policy(
                    registry,
                    base,
                    require_unattended=agent == "ad_hoc_background",
                )
                if item.capability_id == capability_id
            )
            lines.append(
                f"{agent}: {'eligible' if decision.eligible else 'excluded'}; "
                f"confirmation={decision.confirmation_required}; "
                f"guardrail={decision.guardrail_required}; "
                f"reason={'; '.join(decision.reasons)}"
            )
        for route_name, route in sorted(settings.gateway.routes.items()):
            decision = next(
                item
                for item in resolve_capability_policy(
                    registry,
                    settings.agents.ad_hoc_background,
                    route=route,
                    require_unattended=True,
                )
                if item.capability_id == capability_id
            )
            lines.append(
                f"route/{route_name}/ad_hoc_background: "
                f"{'eligible' if decision.eligible else 'excluded'}; "
                f"confirmation={decision.confirmation_required}; "
                f"guardrail={decision.guardrail_required}"
            )
        renderer.render_status("\n".join(lines), style="")


async def _validate(project: Path | None, renderer: CliRenderer) -> None:
    settings, root, session = _with_inventory(project)
    browser_factory = BrowserService.create if settings.browser.enabled else None
    async with build_capability_runtime(
        settings,
        session=session,
        project_root=root,
        browser_factory=browser_factory,
    ) as runtime:
        registry = _complete_registry(runtime)
        diagnostics: list[CapabilityDiagnostic] = []
        diagnostics.extend(validate_capability_inventory(registry, runtime.guardrail_registry))
        for agent in ("gateway_foreground", "ad_hoc_background"):
            base = _agent_policy(settings, agent)
            diagnostics.extend(
                validate_capability_policy(
                    registry,
                    runtime.guardrail_registry,
                    base,
                )
            )
            if agent == "gateway_foreground":
                diagnostics.extend(validate_foreground_live_policy(registry, base))
            for route in settings.gateway.routes.values():
                diagnostics.extend(
                    validate_capability_policy(
                        registry,
                        runtime.guardrail_registry,
                        base,
                        route=route,
                    )
                )
                if agent == "gateway_foreground":
                    diagnostics.extend(validate_foreground_live_policy(registry, base, route=route))
        unique = {(item.capability_id, item.severity, item.message): item for item in diagnostics}
        if not unique:
            renderer.render_status(
                f"{len(registry.ids())} capabilities: valid",
                style="green",
            )
            return
        lines = [
            f"{item.severity}: {item.capability_id}: {item.message}" for item in unique.values()
        ]
        renderer.render_status("\n".join(lines), style="yellow")
        if any(item.severity == "error" for item in unique.values()):
            raise RuntimeError("capability validation failed")


def _agent_policy(settings: RickySettings, agent: CapabilityAgent):
    return getattr(settings.agents, agent)


def _complete_registry(runtime: CapabilityRuntime) -> CapabilityRegistry:
    """Add code-constructed gateway controls without opening stores or a provider."""

    return build_capability_registry(
        gateway_capability_inventory_tools(
            runtime.tools,
            gateway_control_descriptors(),
        ),
        runtime.skill_registry,
        capability_specs=CAPABILITY_SPECS,
        skill_owners=registered_skill_owners(runtime.capability_registry),
        state_guards=StateGuardRegistry([DurableTaskStateGuard(runtime.durable_tasks)]),
    )
