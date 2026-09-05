"""Execution requests, review drafts, contracts, and delegated authority commands."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from typing import Any

import typer

from ricky.authority.store import AuthorityStore, AuthorityStoreError
from ricky.authority.types import DelegationGrant
from ricky.config import RickySettings, load_settings
from ricky.durable_tasks.store import TaskStoreError
from ricky.executions.dispatcher import ExecutionDispatcher, ExecutionDispatchError
from ricky.executions.drafts import DraftStatus, ExecutionDraft
from ricky.executions.store import ExecutionStore, ExecutionStoreError
from ricky.executions.types import ExecutionRequest, ExecutionStatus
from ricky.interfaces.cli.render import CliRenderer
from ricky.jobs.runner import JobConfigurationError
from ricky.jobs.store import JobRunStore, JobStoreError
from ricky.profiles import ProfileScope

_EXECUTION_STATUS_OPTION = typer.Option(None, "--status")

_EXECUTION_LIMIT_OPTION = typer.Option(50, min=1, max=1_000)

_EXECUTION_DRAFT_STATUS_OPTION = typer.Option(None, "--status")

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

_AUTHORITY_TASK_OPTION = typer.Option(None, "--task", help="Filter by durable task id.")

_AUTHORITY_LIMIT_OPTION = typer.Option(50, min=1, max=1_000)

_AUTHORITY_REASON_OPTION = typer.Option(
    "revoked from the command line", "--reason", help="Recorded revocation reason."
)


def execution_list(
    status: ExecutionStatus | None = _EXECUTION_STATUS_OPTION,
    limit: int = _EXECUTION_LIMIT_OPTION,
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """List durable execution requests without constructing a provider."""

    _run_execution_command(
        lambda renderer: _execution_list(status, limit, profile, access_profiles or [], renderer)
    )


def execution_show(
    request_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Show one execution request and its activity."""

    _run_execution_command(
        lambda renderer: _execution_show(request_id, profile, access_profiles or [], renderer)
    )


def execution_cancel(
    request_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Cancel queued or active execution work."""

    _run_execution_command(
        lambda renderer: _execution_cancel(request_id, profile, access_profiles or [], renderer)
    )


def execution_retry(
    request_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Create a new child attempt from a terminal request."""

    _run_execution_command(
        lambda renderer: _execution_retry(request_id, profile, access_profiles or [], renderer)
    )


def execution_worker(once: bool = typer.Option(False, "--once")) -> None:
    """Run the execution dispatcher once or continuously."""

    _run_execution_command(lambda renderer: _execution_worker(once, renderer))


def authority_list(
    task: str | None = _AUTHORITY_TASK_OPTION,
    limit: int = _AUTHORITY_LIMIT_OPTION,
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """List delegation grants without constructing a provider."""

    _run_execution_command(
        lambda renderer: _authority_list(task, limit, profile, access_profiles or [], renderer)
    )


def authority_show(
    grant_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Show one delegation grant, its exact scope, and its source message."""

    _run_execution_command(
        lambda renderer: _authority_show(grant_id, profile, access_profiles or [], renderer)
    )


def authority_revoke(
    grant_id: str = typer.Argument(...),
    reason: str = _AUTHORITY_REASON_OPTION,
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Revoke a grant so no further delegated tool call is allowed."""

    _run_execution_command(
        lambda renderer: _authority_revoke(
            grant_id, reason, profile, access_profiles or [], renderer
        )
    )


def authority_activity(
    grant_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Show every append-only use, denial, and lifecycle record for one grant."""

    _run_execution_command(
        lambda renderer: _authority_activity(grant_id, profile, access_profiles or [], renderer)
    )


def execution_draft_list(
    status: DraftStatus | None = _EXECUTION_DRAFT_STATUS_OPTION,
    limit: int = _EXECUTION_LIMIT_OPTION,
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """List durable capability/guardrail/confirmation review drafts."""

    _run_execution_command(
        lambda renderer: _execution_draft_list(
            status, limit, profile, access_profiles or [], renderer
        )
    )


def execution_draft_show(
    draft_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Show one draft and its append-only lifecycle without source text."""

    _run_execution_command(
        lambda renderer: _execution_draft_show(draft_id, profile, access_profiles or [], renderer)
    )


def execution_draft_cancel(
    draft_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Cancel one incomplete live-review draft using compare-and-swap."""

    _run_execution_command(
        lambda renderer: _execution_draft_cancel(draft_id, profile, access_profiles or [], renderer)
    )


def execution_contract_show(
    contract_id_or_digest: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Show one exact immutable execution contract."""

    _run_execution_command(
        lambda renderer: _execution_contract_show(
            contract_id_or_digest, profile, access_profiles or [], renderer
        )
    )


def execution_contract_explain(
    contract_id_or_digest: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Explain capability-to-resource and live-evidence resolution."""

    _run_execution_command(
        lambda renderer: _execution_contract_explain(
            contract_id_or_digest, profile, access_profiles or [], renderer
        )
    )


def _operator_profile_scope(settings: RickySettings) -> ProfileScope:
    return settings.resolve_profile_scope(
        settings.profiles.default,
        access_profiles=settings.profiles.enabled,
    )


async def _execution_list(
    status: ExecutionStatus | None,
    limit: int,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    requests = await ExecutionDispatcher(settings).list_execution_requests(
        scope=scope, status=status, limit=limit
    )
    if not requests:
        renderer.render_status("No execution requests found.", style="yellow")
        return
    renderer.render_status("\n\n".join(_render_execution(item) for item in requests), style="")


async def _execution_show(
    request_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    dispatcher = ExecutionDispatcher(settings)
    request = await dispatcher.read_execution_request(request_id, scope=scope)
    activity = await dispatcher.store.activities(request_id, scope=scope)
    text = _render_execution(request)
    if activity:
        text += "\nactivity:\n" + "\n".join(
            f"  - {item.created_at.isoformat()} {item.kind}: {item.summary}"
            for item in reversed(activity)
        )
    renderer.render_status(text, style="")


async def _execution_cancel(
    request_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    request = await ExecutionDispatcher(settings).cancel_execution_request(request_id, scope=scope)
    renderer.render_status(f"{request.id}: {request.status}", style="green")


async def _execution_retry(
    request_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    request = await ExecutionDispatcher(settings).retry_execution_request(request_id, scope=scope)
    renderer.render_status(
        f"queued retry {request.id} of {request.parent_request_id}", style="green"
    )


async def _execution_worker(once: bool, renderer: CliRenderer) -> None:
    settings = load_settings()
    scope = _operator_profile_scope(settings)
    dispatcher = ExecutionDispatcher(settings)
    if not once:
        renderer.render_status("Execution worker started.", style="green")
        await dispatcher.worker(scope=scope)
        return
    completed = await dispatcher.worker_once(scope=scope)
    if not completed:
        renderer.render_status("No eligible execution requests.", style="yellow")
        return
    renderer.render_status("\n".join(f"{item.id}: {item.status}" for item in completed), style="")


async def _execution_draft_list(
    status: DraftStatus | None,
    limit: int,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = ExecutionStore(settings)
    await store.initialize()
    drafts = await store.list_drafts(scope=scope, status=status, limit=limit)
    if not drafts:
        renderer.render_status("No execution drafts found.", style="yellow")
        return
    renderer.render_status("\n\n".join(_render_draft(item) for item in drafts), style="")


async def _execution_draft_show(
    draft_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = ExecutionStore(settings)
    await store.initialize()
    draft = await store.get_draft(draft_id, scope=scope)
    activity = await store.draft_activities(draft.id, scope=scope)
    text = _render_draft(draft)
    if activity:
        text += "\nactivity:\n" + "\n".join(
            f"  - {item.created_at.isoformat()} {item.kind} "
            f"revision={item.revision}: {item.summary}"
            for item in reversed(activity)
        )
    renderer.render_status(text, style="")


async def _execution_draft_cancel(
    draft_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = ExecutionStore(settings)
    await store.initialize()
    draft = await store.cancel_draft(draft_id, scope=scope)
    renderer.render_status(f"{draft.id}: {draft.status}", style="green")


async def _execution_contract_show(
    contract_id_or_digest: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = ExecutionStore(settings)
    await store.initialize()
    contract = await store.get_contract(contract_id_or_digest, scope=scope)
    renderer.render_status(contract.model_dump_json(indent=2), style="")


async def _execution_contract_explain(
    contract_id_or_digest: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = ExecutionStore(settings)
    await store.initialize()
    contract = await store.get_contract(contract_id_or_digest, scope=scope)
    tools = {item.id: item for item in contract.tools}
    skills = {item.id: item for item in contract.skills}
    guardrails = {item.capability_id: item for item in contract.guardrails}
    lines = [
        f"contract: {contract.id}",
        f"digest: {contract.digest}",
        f"task: {contract.task_id} revision={contract.task_revision} "
        f"profiles={','.join(contract.profile_scope.profiles)}",
        f"route: {contract.route_name} -> {contract.notification_route}",
        f"runtime: {contract.provider}/{contract.model}",
        "resolution:",
    ]
    for capability in contract.capabilities:
        lines.append(
            f"  - {capability.id} v{capability.version}: "
            f"confirmation={capability.confirmation_required}; "
            f"guardrail={capability.guardrail_required}"
        )
        for resource in capability.resources:
            if resource.kind == "tool":
                resolved = tools[resource.id]
                lines.append(
                    f"      tool/{resolved.id} v{resolved.contract_version} "
                    f"risk={resolved.risk_class} digest={resolved.schema_digest}"
                )
            else:
                resolved_skill = skills[resource.id]
                lines.append(
                    f"      skill/{resolved_skill.id} digest={resolved_skill.bundle_digest}"
                )
        guardrail = guardrails.get(capability.id)
        if guardrail is not None:
            lines.append(
                f"      guardrail {guardrail.schema_id} v{guardrail.schema_version}: "
                f"{guardrail.summary}"
            )
    lines.extend(
        (
            f"confirmations: {len(contract.confirmations)}",
            f"agent policy: {contract.agent_policy_digest}",
            f"route policy: {contract.route_policy_digest}",
            f"inventory: {contract.inventory_digest}",
            f"authority policy: {contract.authority_policy_digest}",
        )
    )
    renderer.render_status("\n".join(lines), style="")


def _render_draft(draft: ExecutionDraft) -> str:
    lines = [
        f"draft: {draft.id}",
        f"target: {draft.target}",
        f"status: {draft.status} revision={draft.revision}",
        f"task: {draft.task_id} revision={draft.task_revision} "
        f"profiles={','.join(draft.profile_scope.profiles)}",
        f"conversation: {draft.conversation_id}",
        f"principal: {draft.principal_id}",
        f"capabilities: {', '.join(draft.requested_capabilities)}",
        f"sources: {', '.join(item.message_id for item in draft.sources)}",
        (
            "collected guardrail fields: "
            + ", ".join(
                f"{item.capability_id}.{item.field}={item.value!r} "
                f"(source {item.source_message_id})"
                for item in draft.collected_guardrail_fields
            )
            if draft.collected_guardrail_fields
            else "collected guardrail fields: -"
        ),
        f"guardrails: {', '.join(item.capability_id for item in draft.guardrails) or '-'}",
        f"confirmation: {draft.confirmation.id if draft.confirmation else '-'}",
        f"updated: {draft.updated_at.isoformat()}",
        f"expires: {draft.expires_at.isoformat()}",
    ]
    if draft.pending_questions:
        lines.append("pending questions: " + " | ".join(draft.pending_questions))
    if draft.contract_id is not None:
        lines.append(f"contract: {draft.contract_id}")
    if draft.request_id is not None:
        lines.append(f"execution: {draft.request_id}")
    if draft.reason is not None:
        lines.append(f"reason: {draft.reason}")
    return "\n".join(lines)


async def _authority_list(
    task: str | None,
    limit: int,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = AuthorityStore(settings)
    await store.initialize()
    grants = await store.list(scope=scope, task_id=task, limit=limit)
    if not grants:
        renderer.render_status("No delegation grants found.", style="yellow")
        return
    renderer.render_status(
        "\n".join(
            f"{item.id} {item.status} task={item.task_id} "
            f"expires={item.expires_at.isoformat()} :: {item.summary[:120]}"
            for item in grants
        ),
        style="",
    )


async def _authority_show(
    grant_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = AuthorityStore(settings)
    await store.initialize()
    grant = await store.get(grant_id, scope=scope)
    renderer.render_status(_render_grant(grant), style="")


async def _authority_revoke(
    grant_id: str,
    reason: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = AuthorityStore(settings)
    await store.initialize()
    grant = await store.revoke(
        grant_id,
        scope=scope,
        actor="ricky_authority_cli",
        reason=reason,
    )
    jobs = JobRunStore(settings)
    await jobs.initialize()
    await jobs.set_grant_budget_status(grant.id, "revoked", scope=scope)
    renderer.render_status(
        f"{grant.id}: {grant.status}. Future delegated calls are denied; any "
        "already confirmed effect is unchanged.",
        style="green",
    )


async def _authority_activity(
    grant_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = AuthorityStore(settings)
    await store.initialize()
    records = await store.activities(grant_id, scope=scope)
    if not records:
        renderer.render_status("No authority activity recorded.", style="yellow")
        return
    renderer.render_status(
        "\n".join(
            f"{item.created_at.isoformat()} {item.kind}"
            f"{' ' + item.tool_name if item.tool_name else ''}"
            f"{' ' + item.disposition if item.disposition else ''}: {item.summary}"
            for item in records
        ),
        style="",
    )


def _render_grant(grant: DelegationGrant) -> str:
    lines = [
        f"grant: {grant.id}",
        f"status: {grant.status}",
        f"task: {grant.task_id} revision={grant.task_revision} "
        f"profiles={','.join(grant.profile_scope.profiles)}",
        f"contract: {grant.contract_id} ({grant.contract_digest})",
        f"execution: {grant.execution_request_id or '-'}",
        f"issued: {grant.issued_at.isoformat()}",
        f"expires: {grant.expires_at.isoformat()}",
        f"effect calls: {grant.effect_call_limit}",
        f"money: {grant.financial_limit_minor if grant.financial_limit_minor is not None else '-'}"
        f" {grant.currency or ''}".rstrip(),
        f"policy digest: {grant.policy_digest}",
        f"summary: {grant.summary}",
        "source:",
        f"  principal: {grant.source.principal_id}",
        f"  conversation: {grant.source.conversation_id}",
        f"  message: {grant.source.inbound_message_id} "
        f"({grant.source.transport}/{grant.source.platform_message_id})",
        f"  text digest: {grant.source.text_digest}",
        "scopes:",
    ]
    lines.extend(
        f"  - {scope.capability} [{scope.schema_id} v{scope.schema_version}]: "
        f"{json.dumps(scope.constraints, sort_keys=True)}"
        for scope in grant.scopes
    )
    return "\n".join(lines)


def _render_execution(request: ExecutionRequest) -> str:
    lines = [
        f"execution: {request.id}",
        f"kind: {request.kind}",
        f"status: {request.status}",
        f"profiles: {','.join(request.profile_scope.profiles)}",
        f"created: {request.created_at.isoformat()}",
        f"route: {request.notification_route}",
    ]
    if request.named_job is not None:
        lines.append(f"job: {request.named_job} ({request.job_digest})")
    if request.contract_id is not None:
        lines.append(f"contract: {request.contract_id} ({request.contract_digest})")
    if request.task_id is not None:
        lines.append(f"task: {request.task_id} revision={request.task_revision}")
    if request.grant_id is not None:
        lines.append(f"grant: {request.grant_id}")
    if request.run_id is not None:
        lines.append(f"run: {request.run_id}")
    if request.parent_request_id is not None:
        lines.append(f"parent: {request.parent_request_id}")
    if request.error is not None:
        lines.append(f"error: {request.error}")
    return "\n".join(lines)


def _run_execution_command(
    factory: Callable[[CliRenderer], Coroutine[Any, Any, None]],
) -> None:
    renderer = CliRenderer()
    try:
        asyncio.run(factory(renderer))
    except (
        AuthorityStoreError,
        ExecutionDispatchError,
        ExecutionStoreError,
        JobConfigurationError,
        JobStoreError,
        TaskStoreError,
        ValueError,
        OSError,
    ) as exc:
        renderer.render_error(f"Execution error: {exc}")
        raise typer.Exit(1) from exc


def register_execution_commands(execution_app: typer.Typer) -> None:
    """Register the commands on the application composition root."""

    execution_app.command("list")(execution_list)
    execution_app.command("show")(execution_show)
    execution_app.command("cancel")(execution_cancel)
    execution_app.command("retry")(execution_retry)
    execution_app.command("worker")(execution_worker)


def register_execution_draft_commands(execution_draft_app: typer.Typer) -> None:
    """Register the commands on the application composition root."""

    execution_draft_app.command("list")(execution_draft_list)
    execution_draft_app.command("show")(execution_draft_show)
    execution_draft_app.command("cancel")(execution_draft_cancel)


def register_execution_contract_commands(execution_contract_app: typer.Typer) -> None:
    """Register the commands on the application composition root."""

    execution_contract_app.command("show")(execution_contract_show)
    execution_contract_app.command("explain")(execution_contract_explain)


def register_authority_commands(authority_app: typer.Typer) -> None:
    """Register the commands on the application composition root."""

    authority_app.command("list")(authority_list)
    authority_app.command("show")(authority_show)
    authority_app.command("revoke")(authority_revoke)
    authority_app.command("activity")(authority_activity)
