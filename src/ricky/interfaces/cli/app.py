"""ricky CLI entry point."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import signal
import tempfile
import tomllib
from collections.abc import Callable, Coroutine
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

import typer

from ricky import __version__
from ricky.agent import AgentSession
from ricky.agent.events import TextDeltaEvent, WorkflowEvent
from ricky.agent.workflow import WorkflowRunner
from ricky.authority.store import AuthorityStore, AuthorityStoreError
from ricky.authority.types import DelegationGrant
from ricky.browser import BrowserService
from ricky.builtins import bundled_workflows_dir
from ricky.config import (
    RickySettings,
    config_file,
    find_project_root,
    load_settings,
    profile_config_file,
    profile_data_path,
    profile_secrets_file,
    user_data_path,
)
from ricky.durable_tasks.artifacts import TaskArtifactStore
from ricky.durable_tasks.render import (
    render_activity,
    render_artifacts,
    render_task,
    render_task_list,
)
from ricky.durable_tasks.scoped import ScopedDurableTaskStore
from ricky.durable_tasks.store import DurableTaskStore, TaskStoreError
from ricky.durable_tasks.tools import durable_task_policy
from ricky.durable_tasks.types import TaskExecutionMode, TaskSearchQuery, canonicalize_task_tags
from ricky.executions.dispatcher import ExecutionDispatcher, ExecutionDispatchError
from ricky.executions.drafts import DraftStatus, ExecutionDraft
from ricky.executions.store import ExecutionStore, ExecutionStoreError
from ricky.executions.types import ExecutionRequest, ExecutionStatus
from ricky.installation import (
    InstallationError,
    bootstrap_file,
    installation_operation_lock,
    require_compatible_installation,
)
from ricky.interfaces.cli.browser import register_browser_commands
from ricky.interfaces.cli.capabilities import register_capability_commands
from ricky.interfaces.cli.chat import ChatController
from ricky.interfaces.cli.gateway import register_gateway_commands
from ricky.interfaces.cli.installation import register_installation_commands
from ricky.interfaces.cli.protected_values import register_protected_value_commands
from ricky.interfaces.cli.render import CliRenderer
from ricky.interfaces.cli.select import run_model_picker
from ricky.interfaces.cli.sessions import register_session_commands
from ricky.jobs.registry import JobRegistry
from ricky.jobs.render import render_job, render_run
from ricky.jobs.runner import (
    JobConfigurationError,
    JobRunner,
)
from ricky.jobs.store import JobRunStore, JobStoreError
from ricky.llm import (
    CompletionRequest,
    Message,
    MessageDone,
    ProviderError,
    TextDelta,
    create_provider,
)
from ricky.memory import MemoryStore, memory_note_counts
from ricky.notifications import NotificationStore, NotificationStoreError
from ricky.notifications.types import NotificationRecord, OutboxStatus, ResolutionDisposition
from ricky.permissions import PermissionEngine
from ricky.profiles import ProfileName, ProfileResourceRef, ProfileScope
from ricky.runtime import build_capability_runtime, build_session_runtime
from ricky.schedules.cron import CronError
from ricky.schedules.render import (
    render_approval,
    render_doctor,
    render_schedule,
    render_schedule_list,
    render_sync,
)
from ricky.schedules.service import ScheduleService, ScheduleServiceError
from ricky.schedules.store import ScheduleStoreError
from ricky.skills.registry import SkillRegistry, discover_skills
from ricky.tools import Tool, ToolRegistry
from ricky.tools.integrations.gcal import GcalError, gcal_toolset
from ricky.tools.integrations.gmail import GmailError, gmail_toolset
from ricky.tools.integrations.google import (
    ALL_SERVICE_SCOPES,
    SERVICE_SCOPES,
    GoogleAuth,
    GoogleAuthError,
)
from ricky.tools.integrations.slack import SlackError, slack_toolset
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

app = typer.Typer(
    name="ricky",
    help="A personal agentic assistant and agent harness.",
    no_args_is_help=False,
    invoke_without_command=True,
    add_completion=False,
)
config_app = typer.Typer(
    help="Inspect or update ricky configuration.",
    invoke_without_command=True,
    add_completion=False,
)
app.add_typer(config_app, name="config")
google_config_app = typer.Typer(
    help="Inspect or authorize named Google accounts.",
    invoke_without_command=True,
    add_completion=False,
)
config_app.add_typer(google_config_app, name="google")
workflow_app = typer.Typer(
    help="Inspect, validate, and dry-run workflows.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(workflow_app, name="workflow")
task_app = typer.Typer(
    help="Inspect and update cross-session durable tasks.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(task_app, name="task")
job_app = typer.Typer(
    help="Run and inspect bounded read-only agent jobs.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(job_app, name="job")
job_action_app = typer.Typer(
    help="Inspect and explicitly reconcile guarded external job actions.",
    no_args_is_help=True,
    add_completion=False,
)
job_app.add_typer(job_action_app, name="action")
schedule_app = typer.Typer(
    help="Manage verified cron schedules for named jobs.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(schedule_app, name="schedule")
session_app = typer.Typer(
    help="Inspect, archive, and resume persistent conversations.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(session_app, name="session")
register_session_commands(session_app)
gateway_app = typer.Typer(
    help="Operate persistent messaging transports and the durable inbox.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(gateway_app, name="gateway")
register_gateway_commands(gateway_app)
capability_app = typer.Typer(
    help="Inspect installed capability expansion and standing agent policy.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(capability_app, name="capability")
register_capability_commands(capability_app)
browser_app = typer.Typer(
    help="Install Chromium and manage browser resources.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(browser_app, name="browser")
register_browser_commands(browser_app)
protected_values_app = typer.Typer(
    help="Manage profile-scoped encrypted protected values.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(protected_values_app, name="protected-values")
register_protected_value_commands(protected_values_app)
register_installation_commands(app)
notification_app = typer.Typer(
    help="Inspect and reconcile durable user notifications.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(notification_app, name="notification")
execution_app = typer.Typer(
    help="Inspect and run durable fire-and-report execution requests.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(execution_app, name="execution")
execution_draft_app = typer.Typer(
    help="Inspect and cancel durable live execution-review drafts.",
    no_args_is_help=True,
    add_completion=False,
)
execution_app.add_typer(execution_draft_app, name="draft")
execution_contract_app = typer.Typer(
    help="Inspect immutable capability-compiled execution contracts.",
    no_args_is_help=True,
    add_completion=False,
)
execution_app.add_typer(execution_contract_app, name="contract")
authority_app = typer.Typer(
    help="Inspect and revoke task-scoped delegated authority.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(authority_app, name="authority")
_SCHEDULE_PROJECT_OPTION = typer.Option(None, "--project", help="Explicit project root.")
_SCHEDULE_INVOKE_PROJECT_OPTION = typer.Option(
    ..., "--project", help="Approved absolute project root."
)
_JOB_TOOLS_OPTION = typer.Option([], "--tool", help="Read-only tool; repeatable.")
_JOB_DRY_RUN_OPTION = typer.Option(False, "--dry-run")
_NOTIFICATION_STATUS_OPTION = typer.Option(None, "--status")
_NOTIFICATION_LIMIT_OPTION = typer.Option(50, min=1, max=1_000)
_NOTIFICATION_RESOLUTION_OPTION = typer.Option(..., "--as")
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


@execution_app.command("list")
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


@execution_app.command("show")
def execution_show(
    request_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Show one execution request and its activity."""

    _run_execution_command(
        lambda renderer: _execution_show(request_id, profile, access_profiles or [], renderer)
    )


@execution_app.command("cancel")
def execution_cancel(
    request_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Cancel queued or active execution work."""

    _run_execution_command(
        lambda renderer: _execution_cancel(request_id, profile, access_profiles or [], renderer)
    )


@execution_app.command("retry")
def execution_retry(
    request_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Create a new child attempt from a terminal request."""

    _run_execution_command(
        lambda renderer: _execution_retry(request_id, profile, access_profiles or [], renderer)
    )


@execution_app.command("worker")
def execution_worker(once: bool = typer.Option(False, "--once")) -> None:
    """Run the execution dispatcher once or continuously."""

    _run_execution_command(lambda renderer: _execution_worker(once, renderer))


_AUTHORITY_TASK_OPTION = typer.Option(None, "--task", help="Filter by durable task id.")
_AUTHORITY_LIMIT_OPTION = typer.Option(50, min=1, max=1_000)
_AUTHORITY_REASON_OPTION = typer.Option(
    "revoked from the command line", "--reason", help="Recorded revocation reason."
)


@authority_app.command("list")
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


@authority_app.command("show")
def authority_show(
    grant_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Show one delegation grant, its exact scope, and its source message."""

    _run_execution_command(
        lambda renderer: _authority_show(grant_id, profile, access_profiles or [], renderer)
    )


@authority_app.command("revoke")
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


@authority_app.command("activity")
def authority_activity(
    grant_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Show every append-only use, denial, and lifecycle record for one grant."""

    _run_execution_command(
        lambda renderer: _authority_activity(grant_id, profile, access_profiles or [], renderer)
    )


@execution_draft_app.command("list")
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


@execution_draft_app.command("show")
def execution_draft_show(
    draft_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Show one draft and its append-only lifecycle without source text."""

    _run_execution_command(
        lambda renderer: _execution_draft_show(draft_id, profile, access_profiles or [], renderer)
    )


@execution_draft_app.command("cancel")
def execution_draft_cancel(
    draft_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Cancel one incomplete live-review draft using compare-and-swap."""

    _run_execution_command(
        lambda renderer: _execution_draft_cancel(draft_id, profile, access_profiles or [], renderer)
    )


@execution_contract_app.command("show")
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


@execution_contract_app.command("explain")
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


@notification_app.command("list")
def notification_list(
    status: OutboxStatus | None = _NOTIFICATION_STATUS_OPTION,
    limit: int = _NOTIFICATION_LIMIT_OPTION,
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """List durable notification requests without constructing a provider."""

    _run_notification_command(
        lambda renderer: _notification_list(status, limit, profile, access_profiles or [], renderer)
    )


@notification_app.command("show")
def notification_show(
    notification_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Show one immutable request and its current outbox state."""

    _run_notification_command(
        lambda renderer: _notification_show(
            notification_id, profile, access_profiles or [], renderer
        )
    )


@notification_app.command("retry")
def notification_retry(
    outbox_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Requeue one delivery confirmed not to have been performed."""

    _run_notification_command(
        lambda renderer: _notification_retry(outbox_id, profile, access_profiles or [], renderer)
    )


@notification_app.command("resolve")
def notification_resolve(
    outbox_id: str = typer.Argument(...),
    resolution: ResolutionDisposition = _NOTIFICATION_RESOLUTION_OPTION,
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Record the user resolution of an ambiguous delivery."""

    _run_notification_command(
        lambda renderer: _notification_resolve(
            outbox_id, resolution, profile, access_profiles or [], renderer
        )
    )


@notification_app.command("cancel")
def notification_cancel(
    outbox_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Cancel a notification that has not been delivered."""

    _run_notification_command(
        lambda renderer: _notification_cancel(outbox_id, profile, access_profiles or [], renderer)
    )


@job_app.command("list")
def job_list(
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """List discovered job bundles without constructing a provider."""

    _run_job_command(lambda renderer: _job_list(profile, access_profiles or [], renderer))


@job_app.command("validate")
def job_validate(
    name: str | None = typer.Argument(None, help="Job name; omit to validate all."),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Validate job shapes and current-machine read-only tool availability."""

    _run_job_command(lambda renderer: _job_validate(name, profile, access_profiles or [], renderer))


@job_app.command("show")
def job_show(
    name: str = typer.Argument(..., help="Job name."),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Show one resolved job and its current tool availability."""

    _run_job_command(lambda renderer: _job_show(name, profile, access_profiles or [], renderer))


@job_app.command("run")
def job_run(
    name: str = typer.Argument(..., help="Job name."),
    dry_run: bool = _JOB_DRY_RUN_OPTION,
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Run one named bounded recurring job without prompting."""

    _run_job_command(
        lambda renderer: _job_run(name, dry_run, profile, access_profiles or [], renderer)
    )


@job_app.command("once")
def job_once(
    goal: str = typer.Argument(..., help="One bounded read-only goal."),
    tools: list[str] = _JOB_TOOLS_OPTION,
    provider: str | None = typer.Option(None, "--provider", "-p"),
    model: str | None = typer.Option(None, "--model", "-m"),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Run one ad-hoc goal with no persistent job identity or lock."""

    _run_job_command(
        lambda renderer: _job_once(
            goal,
            tools,
            provider,
            model,
            profile,
            access_profiles or [],
            renderer,
        )
    )


@job_app.command("history")
def job_history(
    job_name: str | None = typer.Option(None, "--job", help="Filter by job name."),
    limit: int = typer.Option(50, min=1, max=500),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """List persisted launch attempts without constructing a provider."""

    _run_job_command(
        lambda renderer: _job_history(job_name, limit, profile, access_profiles or [], renderer)
    )


@job_app.command("report")
def job_report(
    run_id: str = typer.Argument(..., help="Job run id."),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Show one persisted job run report."""

    _run_job_command(lambda renderer: _job_report(run_id, profile, access_profiles or [], renderer))


@job_action_app.command("show")
def job_action_show(
    action_id: str = typer.Argument(..., help="Job action id."),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Inspect one guarded external action without constructing a provider."""

    _run_job_command(
        lambda renderer: _job_action_show(action_id, profile, access_profiles or [], renderer)
    )


@job_action_app.command("resolve")
def job_action_resolve(
    action_id: str = typer.Argument(..., help="In-doubt job action id."),
    performed: bool = typer.Option(False, "--performed"),
    not_performed: bool = typer.Option(False, "--not-performed"),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Append a user reconciliation after checking the external system."""

    if performed == not_performed:
        raise typer.BadParameter("choose exactly one of --performed or --not-performed")
    disposition = "performed" if performed else "not_performed"
    _run_job_command(
        lambda renderer: _job_action_resolve(
            action_id,
            disposition,
            profile,
            access_profiles or [],
            renderer,
        )
    )


@schedule_app.command("list")
def schedule_list(
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """List desired schedules and current revision/approval state."""

    _run_schedule_command(lambda renderer: _schedule_list(profile, access_profiles or [], renderer))


@schedule_app.command("show")
def schedule_show(
    schedule_id: str = typer.Argument(..., help="Opaque schedule id."),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Inspect one desired schedule and its current revision/approval state."""

    _run_schedule_command(
        lambda renderer: _schedule_show(schedule_id, profile, access_profiles or [], renderer)
    )


@schedule_app.command("add")
def schedule_add(
    job_name: str = typer.Argument(..., help="Named job to launch."),
    cron: str = typer.Option(..., "--cron", help="Numeric five-field cron expression."),
    project: Path | None = _SCHEDULE_PROJECT_OPTION,
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Create and approve desired state; does not install it."""

    _run_schedule_command(
        lambda renderer: _schedule_add(
            job_name, cron, project, profile, access_profiles or [], renderer
        )
    )


@schedule_app.command("set")
def schedule_set(
    schedule_id: str = typer.Argument(..., help="Opaque schedule id."),
    cron: str = typer.Option(..., "--cron", help="Numeric five-field cron expression."),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Update desired timing; the new cron must then be approved."""

    _run_schedule_command(
        lambda renderer: _schedule_set(schedule_id, cron, profile, access_profiles or [], renderer)
    )


@schedule_app.command("enable")
def schedule_enable(
    schedule_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Enable desired state; run sync separately to install it."""

    _run_schedule_command(
        lambda renderer: _schedule_enabled(
            schedule_id, True, profile, access_profiles or [], renderer
        )
    )


@schedule_app.command("disable")
def schedule_disable(
    schedule_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Disable desired state; run sync separately to remove its cron line."""

    _run_schedule_command(
        lambda renderer: _schedule_enabled(
            schedule_id, False, profile, access_profiles or [], renderer
        )
    )


@schedule_app.command("remove")
def schedule_remove(
    schedule_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Remove desired state only; run sync separately to reconcile cron."""

    _run_schedule_command(
        lambda renderer: _schedule_remove(schedule_id, profile, access_profiles or [], renderer)
    )


@schedule_app.command("approve")
def schedule_approve(
    schedule_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Approve the current authority envelope, cron, and exact revision."""

    _run_schedule_command(
        lambda renderer: _schedule_approve(schedule_id, profile, access_profiles or [], renderer)
    )


@schedule_app.command("refresh")
def schedule_refresh(
    schedule_id: str = typer.Argument(...),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Validate and pin a changed job revision that does not expand authority."""

    _run_schedule_command(
        lambda renderer: _schedule_refresh(schedule_id, profile, access_profiles or [], renderer)
    )


@schedule_app.command("sync")
def schedule_sync(
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Reconcile desired schedules into Ricky's verified user-crontab block."""

    _run_schedule_command(lambda renderer: _schedule_sync(profile, access_profiles or [], renderer))


@schedule_app.command("doctor")
def schedule_doctor(
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Check desired/installed drift and launch prerequisites without a model."""

    _run_schedule_command(
        lambda renderer: _schedule_doctor(profile, access_profiles or [], renderer)
    )


@schedule_app.command("uninstall")
def schedule_uninstall(
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Remove only Ricky's managed block, retaining desired schedules."""

    _run_schedule_command(
        lambda renderer: _schedule_uninstall(profile, access_profiles or [], renderer)
    )


@schedule_app.command("invoke")
def schedule_invoke(
    schedule_id: str = typer.Argument(...),
    project: Path = _SCHEDULE_INVOKE_PROJECT_OPTION,
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Backend trigger: validate one approved schedule and launch its job."""

    _run_schedule_command(
        lambda renderer: _schedule_invoke(
            schedule_id, project, profile, access_profiles or [], renderer
        )
    )


_TASK_PROFILE_OPTION = typer.Option(
    None,
    "--profile",
    help="Owning task profile; defaults to the configured default profile.",
)
_TASK_INCLUDE_CLOSED_OPTION = typer.Option(
    False, "--include-closed", help="Include completed and cancelled tasks."
)
_TASK_LIMIT_OPTION = typer.Option(50, min=1, max=500)
_TASK_MODE_OPTION = typer.Option("user", "--mode")
_TASK_PRIORITY_OPTION = typer.Option(0, min=-100, max=100)
_TASK_DUE_OPTION = typer.Option(None, "--due-at")
_TASK_TAG_OPTION = typer.Option(None, "--tag")
_TASK_TAG_ADD_OPTION = typer.Option(None, "--add")
_TASK_TAG_REMOVE_OPTION = typer.Option(None, "--remove")


@task_app.command("list")
def task_list(
    profile: ProfileName | None = _TASK_PROFILE_OPTION,
    include_closed: bool = _TASK_INCLUDE_CLOSED_OPTION,
    limit: int = _TASK_LIMIT_OPTION,
) -> None:
    """List durable tasks without invoking a model provider."""

    _run_task_command(lambda renderer: _task_list(profile, include_closed, limit, renderer))


@task_app.command("show")
def task_show(
    task_id: str = typer.Argument(..., help="Durable task id."),
    profile: ProfileName | None = _TASK_PROFILE_OPTION,
) -> None:
    """Show one durable task."""

    _run_task_command(lambda renderer: _task_show(task_id, profile, renderer))


@task_app.command("activity")
def task_activity(
    task_id: str = typer.Argument(..., help="Durable task id."),
    profile: ProfileName | None = _TASK_PROFILE_OPTION,
    limit: int = _TASK_LIMIT_OPTION,
) -> None:
    """Show append-only activity for one durable task."""

    _run_task_command(lambda renderer: _task_activity(task_id, profile, limit, renderer))


@task_app.command("artifacts")
def task_artifacts(
    task_id: str = typer.Argument(..., help="Durable task id."),
    profile: ProfileName | None = _TASK_PROFILE_OPTION,
) -> None:
    """List one durable task's human-readable artifact files."""

    _run_task_command(lambda renderer: _task_artifacts(task_id, profile, renderer))


@task_app.command("create")
def task_create(
    title: str = typer.Option(..., "--title"),
    objective: str = typer.Option(..., "--objective"),
    closure_criteria: str = typer.Option(..., "--closure-criteria"),
    execution_mode: TaskExecutionMode = _TASK_MODE_OPTION,
    profile: ProfileName | None = _TASK_PROFILE_OPTION,
    priority: int = _TASK_PRIORITY_OPTION,
    due_at: datetime | None = _TASK_DUE_OPTION,
    tags: list[str] | None = _TASK_TAG_OPTION,
) -> None:
    """Create a durable task as a deterministic user command."""

    _run_task_command(
        lambda renderer: _task_create(
            title,
            objective,
            closure_criteria,
            execution_mode,
            profile,
            priority,
            due_at,
            tags or [],
            renderer,
        )
    )


@task_app.command("tag")
def task_tag(
    task_id: str = typer.Argument(..., help="Durable task id."),
    add: list[str] | None = _TASK_TAG_ADD_OPTION,
    remove: list[str] | None = _TASK_TAG_REMOVE_OPTION,
    profile: ProfileName | None = _TASK_PROFILE_OPTION,
) -> None:
    """Add or remove exact open-vocabulary tags through the normal lease path."""

    _run_task_command(
        lambda renderer: _task_tag(task_id, profile, add or [], remove or [], renderer)
    )


@task_app.command("complete")
def task_complete(
    task_id: str = typer.Argument(..., help="Durable task id."),
    summary: str = typer.Option(..., "--summary", help="How closure criteria were met."),
    profile: ProfileName | None = _TASK_PROFILE_OPTION,
) -> None:
    """Claim and complete one task unless another live lease exists."""

    _run_task_command(
        lambda renderer: _task_close(task_id, profile, "completed", summary, renderer)
    )


@task_app.command("cancel")
def task_cancel(
    task_id: str = typer.Argument(..., help="Durable task id."),
    reason: str = typer.Option(..., "--reason"),
    profile: ProfileName | None = _TASK_PROFILE_OPTION,
) -> None:
    """Claim and cancel one task without deleting its history."""

    _run_task_command(lambda renderer: _task_close(task_id, profile, "cancelled", reason, renderer))


@task_app.command("reopen")
def task_reopen(
    task_id: str = typer.Argument(..., help="Durable task id."),
    reason: str = typer.Option(..., "--reason"),
    profile: ProfileName | None = _TASK_PROFILE_OPTION,
) -> None:
    """Reopen a completed or cancelled durable task."""

    _run_task_command(lambda renderer: _task_reopen(task_id, profile, reason, renderer))


def _version_callback(value: bool) -> None:
    if value:
        CliRenderer().render_status(f"ricky {__version__}", style="")
        raise typer.Exit()


@app.callback()
def main_callback(
    ctx: typer.Context,
    _version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the ricky version and exit.",
    ),
) -> None:
    """ricky - a personal agentic assistant and agent harness."""
    # Lifecycle commands own their exclusive/recovery locking. Every ordinary
    # command against an initialized release holds shared authority until its
    # Click context closes, before configuration or durable state is opened.
    if ctx.invoked_subcommand not in {
        "init",
        "upgrade",
        "_upgrade-handoff",
        "decommission",
        "data",
    }:
        try:
            pointer_path = bootstrap_file()
            if pointer_path.exists() or pointer_path.is_symlink():
                ctx.with_resource(
                    installation_operation_lock(
                        mode="shared",
                        timeout_seconds=5.0,
                        operation="runtime",
                    )
                )
                require_compatible_installation()
        except (InstallationError, OSError) as exc:
            CliRenderer().render_error(f"Installation error: {exc}")
            raise typer.Exit(1) from exc
    if ctx.invoked_subcommand is None:
        _run_with_provider_errors(lambda renderer: _chat(None, None, renderer))


@config_app.callback()
def config_callback(ctx: typer.Context) -> None:
    """Show the resolved configuration (secrets redacted)."""
    if ctx.invoked_subcommand is None:
        root_settings = load_settings()
        scope = root_settings.resolve_profile_scope()
        settings = root_settings.resolve_profile_runtime_settings(scope)
        root = user_data_path(root_settings)
        CliRenderer().render_config(
            settings,
            installation_config_path=config_file(root),
            profile_config_path=profile_config_file(scope.primary, root),
            profile_secrets_path=profile_secrets_file(scope.primary, root),
        )


@config_app.command("model")
def config_model(
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Profile whose provider and model defaults will be updated.",
    ),
) -> None:
    """Interactively choose and persist one profile's provider and model defaults."""
    _run_with_provider_errors(lambda renderer: _config_model(profile, renderer))


async def _config_model(profile: str | None, renderer: CliRenderer) -> None:
    root_settings = load_settings()
    scope = root_settings.resolve_profile_scope(profile)
    await run_model_picker(root_settings, renderer, profile_scope=scope, profile=scope.primary)


@google_config_app.callback()
def config_google_callback(ctx: typer.Context) -> None:
    """Show redacted OAuth status for every configured Google account."""
    if ctx.invoked_subcommand is None:
        _run_with_provider_errors(_check_google)


@google_config_app.command("auth")
def config_google_auth(
    account: str = typer.Argument(..., help="Configured Google account name."),
    no_browser: bool = typer.Option(
        False,
        "--no-browser",
        help="Print the consent URL without attempting to open a browser.",
    ),
    callback_port: int | None = typer.Option(
        None,
        "--callback-port",
        min=1,
        max=65535,
        help="Fixed loopback callback port; use with SSH port forwarding.",
    ),
) -> None:
    """Authorize one Google account via PKCE consent."""
    _run_with_provider_errors(
        lambda renderer: _authorize_google(
            account,
            renderer,
            open_browser=not no_browser,
            callback_port=callback_port or 0,
        )
    )


@config_app.command("gmail")
def config_gmail() -> None:
    """Check Gmail identity and mailbox totals for every configured account."""
    _run_with_provider_errors(_check_gmail)


@config_app.command("gcal")
def config_gcal() -> None:
    """Check Calendar identity, primary calendar, and timezone for each account."""
    _run_with_provider_errors(_check_gcal)


@config_app.command("slack")
def config_slack() -> None:
    """Check Slack authentication for every configured profile."""
    _run_with_provider_errors(_check_slack)


@config_app.command("memory")
def config_memory() -> None:
    """Show profile-owned memory roots and read-only note counts."""
    settings = load_settings()
    CliRenderer().render_memory_config(
        roots={
            profile: profile_data_path(settings, profile) / "memory"
            for profile in settings.profiles.enabled
        },
        counts=memory_note_counts(settings),
    )


async def _check_google(renderer: CliRenderer) -> None:
    root_settings = load_settings()
    settings = root_settings.resolve_profile_runtime_settings(
        root_settings.resolve_profile_scope(
            access_profiles=root_settings.profiles.enabled,
        )
    )
    auth = GoogleAuth(settings, scopes=ALL_SERVICE_SCOPES)
    failed = False
    try:
        if not auth.account_names:
            renderer.render_status(
                "No Google accounts are configured in an enabled profile.",
                style="yellow",
            )
            raise typer.Exit(1)
        for status in auth.statuses():
            prefix = f"Google [{status.account}] {status.expected_email}:"
            if not status.client_configured:
                failed = True
                profile_name, account_name = status.account.split("/", 1)
                resource = ProfileResourceRef(profile=profile_name, name=account_name)
                renderer.render_status(
                    f"{prefix} OAuth client missing; add "
                    f"[google_oauth_clients.{resource.name}] to "
                    f"<user_data_dir>/profiles/{resource.profile}/.secrets.toml.",
                    style="yellow",
                )
            elif not status.token_present:
                failed = True
                renderer.render_status(
                    f"{prefix} not authorized; run ricky config google auth {status.account}.",
                    style="yellow",
                )
            elif status.client_matches is False:
                failed = True
                renderer.render_status(
                    f"{prefix} OAuth client changed; "
                    f"run ricky config google auth {status.account}.",
                    style="yellow",
                )
            elif status.missing_scopes:
                # Granular consent may grant a subset of services; only a
                # token with no usable service scope is a failure.
                enabled, disabled = _service_readiness(status.granted_scopes)
                if not enabled:
                    failed = True
                    renderer.render_status(
                        f"{prefix} missing scopes {', '.join(status.missing_scopes)}; "
                        f"run ricky config google auth {status.account}.",
                        style="yellow",
                    )
                else:
                    renderer.render_status(
                        f"{prefix} authorized as {status.stored_email}; services: "
                        f"{', '.join(enabled)} (not granted: {', '.join(disabled)}; "
                        f"re-run ricky config google auth {status.account} to enable).",
                        style="yellow",
                    )
            else:
                scopes = ", ".join(status.granted_scopes)
                renderer.render_status(
                    f"{prefix} authorized as {status.stored_email}; scopes: {scopes}.",
                    style="green",
                )
    finally:
        await auth.aclose()
    if failed:
        raise typer.Exit(1)


async def _authorize_google(
    account: str,
    renderer: CliRenderer,
    *,
    open_browser: bool,
    callback_port: int,
) -> None:
    root_settings = load_settings()
    settings = root_settings.resolve_profile_runtime_settings(
        root_settings.resolve_profile_scope(
            access_profiles=root_settings.profiles.enabled,
        )
    )
    auth = GoogleAuth(settings, scopes=ALL_SERVICE_SCOPES)

    def show_authorization_url(url: str) -> None:
        behavior = (
            "also opening a browser"
            if open_browser
            else (
                "browser launch disabled; open the URL on your local machine and "
                "forward its callback port over SSH when Ricky is remote"
            )
        )
        renderer.render_status(
            f"Open this URL to authorize [{account}] ({behavior}):\n{url}",
            style="cyan",
        )

    try:
        status = await auth.authorize(
            account,
            on_authorization_url=show_authorization_url,
            open_browser=open_browser,
            callback_port=callback_port,
        )
    finally:
        await auth.aclose()
    enabled, disabled = _service_readiness(status.granted_scopes)
    detail = f"; services enabled: {', '.join(enabled) or '[none]'}"
    if disabled:
        detail += f" (not granted: {', '.join(disabled)})"
    renderer.render_status(
        f"Google [{status.account}] authorized as {status.stored_email}{detail}.",
        style="green",
    )


def _service_readiness(granted_scopes: list[str]) -> tuple[list[str], list[str]]:
    """Split known Google services into (enabled, not granted) for one grant."""
    granted = set(granted_scopes)
    enabled = [name for name, scopes in SERVICE_SCOPES.items() if scopes <= granted]
    disabled = [name for name in SERVICE_SCOPES if name not in enabled]
    return enabled, disabled


async def _check_gmail(renderer: CliRenderer) -> None:
    root_settings = load_settings()
    settings = root_settings.resolve_profile_runtime_settings(
        root_settings.resolve_profile_scope(
            access_profiles=root_settings.profiles.enabled,
        )
    )
    toolset = gmail_toolset(settings)
    if toolset is None:
        _render_missing_google_credentials(renderer)
        raise typer.Exit(1)

    async def probe(account: str) -> str:
        email, total = await toolset.check_account(account)
        return f"Gmail [{account}] OK: authenticated as {email}; {total} total messages."

    await _check_google_accounts(
        renderer,
        settings=settings,
        label="Gmail",
        probe=probe,
        error_types=(GoogleAuthError, GmailError),
        aclose=toolset.aclose,
    )


async def _check_gcal(renderer: CliRenderer) -> None:
    root_settings = load_settings()
    settings = root_settings.resolve_profile_runtime_settings(
        root_settings.resolve_profile_scope(
            access_profiles=root_settings.profiles.enabled,
        )
    )
    toolset = gcal_toolset(settings)
    if toolset is None:
        _render_missing_google_credentials(renderer)
        raise typer.Exit(1)

    async def probe(account: str) -> str:
        primary, timezone = await toolset.check_account(account)
        return f"Calendar [{account}] OK: primary {primary}; timezone {timezone}."

    await _check_google_accounts(
        renderer,
        settings=settings,
        label="Calendar",
        probe=probe,
        error_types=(GoogleAuthError, GcalError),
        aclose=toolset.aclose,
    )


def _render_missing_google_credentials(renderer: CliRenderer) -> None:
    renderer.render_status(
        "No configured Google account has matching OAuth client credentials. "
        "Add [google_oauth_clients.<account>] to the owning profile's .secrets.toml.",
        style="yellow",
    )


async def _check_google_accounts(
    renderer: CliRenderer,
    *,
    settings: RickySettings,
    label: str,
    probe: Callable[[str], Coroutine[Any, Any, str]],
    error_types: tuple[type[Exception], ...],
    aclose: Callable[[], Coroutine[Any, Any, None]],
) -> None:
    """Probe every credentialed account; accounts without OAuth credentials
    are reported as skipped, matching the toolset availability rule."""
    failed = False
    try:
        for account in sorted(settings.google.accounts):
            if account not in settings.google_oauth_clients:
                renderer.render_status(
                    f"{label} [{account}] skipped: no OAuth client configured.",
                    style="yellow",
                )
                continue
            try:
                line = await probe(account)
            except error_types as exc:
                failed = True
                renderer.render_error(f"{label} [{account}] check failed: {exc}")
                continue
            renderer.render_status(line, style="green")
    finally:
        await aclose()
    if failed:
        raise typer.Exit(1)


async def _check_slack(renderer: CliRenderer) -> None:
    root_settings = load_settings()
    configured_profiles = [
        profile
        for profile in root_settings.profiles.enabled
        if (configured := root_settings.profile_configs.get(profile)) is not None
        and (configured.slack is not None or configured.slack_user_token is not None)
    ]
    # A conventional environment credential is installation-level rather than
    # present in one profile document. Check it through the default scope once.
    profiles = configured_profiles or [root_settings.profiles.default]
    failed = False
    found = False
    for profile in profiles:
        settings = root_settings.resolve_profile_runtime_settings(
            root_settings.resolve_profile_scope(profile)
        )
        toolset = slack_toolset(settings)
        if toolset is None:
            failed = True
            renderer.render_status(
                f"Slack [{profile}] slack_user_token is not set. Add it to the owning "
                "profile's .secrets.toml; required user-token scopes are listed in "
                ".designs/assets/slack-app-manifest.yaml.",
                style="yellow",
            )
            continue
        found = True
        try:
            user, team = await toolset.check_auth()
        except SlackError as exc:
            failed = True
            renderer.render_error(f"Slack [{profile}] auth check failed: {exc}")
        else:
            renderer.render_status(
                f"Slack [{profile}] OK: authenticated as {user} in {team}.",
                style="green",
            )
        finally:
            await toolset.aclose()
    if failed or not found:
        raise typer.Exit(1)


@app.command()
def chat(
    provider: str | None = typer.Option(
        None,
        "--provider",
        "-p",
        help="Override the default provider.",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        "-m",
        help="Override the provider's default model.",
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Primary profile for this session.",
    ),
    access_profiles: Annotated[
        list[str] | None,
        typer.Option("--access-profile", help="Additional accessible profile; repeatable."),
    ] = None,
) -> None:
    """Start an interactive agent chat session."""
    _run_with_provider_errors(
        lambda renderer: _chat(
            provider,
            model,
            renderer,
            profile_name=profile,
            access_profiles=access_profiles or (),
        )
    )


@app.command()
def ask(
    prompt: str = typer.Argument(..., help="Prompt to send to the configured model."),
    provider: str | None = typer.Option(
        None,
        "--provider",
        "-p",
        help="Override the default provider.",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        "-m",
        help="Override the provider's default model.",
    ),
    temperature: float | None = typer.Option(None, help="Sampling temperature."),
    max_tokens: int | None = typer.Option(None, help="Maximum completion tokens."),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Primary profile for this request.",
    ),
    access_profiles: Annotated[
        list[str] | None,
        typer.Option("--access-profile", help="Additional accessible profile; repeatable."),
    ] = None,
) -> None:
    """Ask the configured model once and stream the reply."""
    _run_with_provider_errors(
        lambda renderer: _ask(
            prompt,
            provider,
            model,
            temperature,
            max_tokens,
            renderer,
            profile_name=profile,
            access_profiles=access_profiles or (),
        )
    )


async def _task_list(
    profile: ProfileName | None,
    include_closed: bool,
    limit: int,
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    selected = settings.resolve_profile_scope(profile).primary
    store = await DurableTaskStore.create(settings, profile=selected)
    tasks = await store.search(TaskSearchQuery(include_closed=include_closed, limit=limit))
    renderer.render_status(render_task_list(tasks), style="")


async def _task_show(task_id: str, profile: ProfileName | None, renderer: CliRenderer) -> None:
    settings = load_settings()
    selected = settings.resolve_profile_scope(profile).primary
    store = await DurableTaskStore.create(settings, profile=selected)
    task = await store.get_task(task_id)
    renderer.render_status(render_task(task), style="")


async def _task_activity(
    task_id: str,
    profile: ProfileName | None,
    limit: int,
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    selected = settings.resolve_profile_scope(profile).primary
    store = await DurableTaskStore.create(settings, profile=selected)
    activity = await store.activities(task_id, limit=limit)
    renderer.render_status(render_activity(activity), style="")


async def _task_artifacts(task_id: str, profile: ProfileName | None, renderer: CliRenderer) -> None:
    settings = load_settings()
    selected = settings.resolve_profile_scope(profile).primary
    store = await DurableTaskStore.create(settings, profile=selected)
    entries = await TaskArtifactStore(store).list(task_id)
    renderer.render_status(render_artifacts(entries), style="")


async def _task_create(
    title: str,
    objective: str,
    closure_criteria: str,
    execution_mode: TaskExecutionMode,
    profile: ProfileName | None,
    priority: int,
    due_at: datetime | None,
    tags: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    selected = settings.resolve_profile_scope(profile).primary
    store = await DurableTaskStore.create(settings, profile=selected)
    task = await store.create_task(
        title=title,
        objective=objective,
        closure_criteria=closure_criteria,
        execution_mode=execution_mode,
        priority=priority,
        due_at=due_at,
        tags=tags,
        authority="deterministic_user_command",
        executor_id="ricky_task_cli",
    )
    renderer.render_status(render_task(task), style="green")


async def _task_tag(
    task_id: str,
    profile: ProfileName | None,
    add: list[str],
    remove: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    selected = settings.resolve_profile_scope(profile).primary
    store = await DurableTaskStore.create(settings, profile=selected)
    session_id = f"cli_{uuid4().hex}"
    claimed = await store.claim(
        task_id,
        holder_session_id=session_id,
        authority="deterministic_user_command",
        executor_id="ricky_task_cli",
    )
    assert claimed.lease is not None
    try:
        additions = set(canonicalize_task_tags(add))
        removals = set(canonicalize_task_tags(remove))
        tags = sorted((set(claimed.tags) | additions) - removals)
        updated = await store.update_tags(
            task_id,
            tags=tags,
            lease=claimed.lease,
            expected_revision=claimed.revision,
            authority="deterministic_user_command",
            executor_id="ricky_task_cli",
        )
        if updated.lease is not None:
            updated = await store.release(
                task_id,
                lease=updated.lease,
                expected_revision=updated.revision,
                authority="deterministic_user_command",
                executor_id="ricky_task_cli",
                summary="Tag edit complete",
            )
    except BaseException:
        await store.release_session_leases(session_id)
        raise
    renderer.render_status(render_task(updated), style="green")


async def _task_close(
    task_id: str,
    profile: ProfileName | None,
    status: Literal["completed", "cancelled"],
    summary: str,
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    selected = settings.resolve_profile_scope(profile).primary
    store = await DurableTaskStore.create(settings, profile=selected)
    session_id = f"cli_{uuid4().hex}"
    claimed = await store.claim(
        task_id,
        holder_session_id=session_id,
        authority="deterministic_user_command",
        executor_id="ricky_task_cli",
    )
    assert claimed.lease is not None
    try:
        if status == "completed":
            task = await store.complete(
                task_id,
                lease=claimed.lease,
                expected_revision=claimed.revision,
                completion_summary=summary,
                authority="deterministic_user_command",
                executor_id="ricky_task_cli",
            )
        else:
            task = await store.cancel(
                task_id,
                lease=claimed.lease,
                expected_revision=claimed.revision,
                reason=summary,
                authority="deterministic_user_command",
                executor_id="ricky_task_cli",
            )
    except BaseException:
        await store.release_session_leases(session_id)
        raise
    renderer.render_status(render_task(task), style="green")


async def _task_reopen(
    task_id: str,
    profile: ProfileName | None,
    reason: str,
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    selected = settings.resolve_profile_scope(profile).primary
    store = await DurableTaskStore.create(settings, profile=selected)
    task = await store.reopen(
        task_id,
        reason=reason,
        authority="deterministic_user_command",
        executor_id="ricky_task_cli",
    )
    renderer.render_status(render_task(task), style="green")


def _run_task_command(
    factory: Callable[[CliRenderer], Coroutine[Any, Any, None]],
) -> None:
    renderer = CliRenderer()
    try:
        asyncio.run(factory(renderer))
    except (TaskStoreError, ValueError, OSError) as exc:
        renderer.render_error(f"Task error: {exc}")
        raise typer.Exit(1) from exc


async def _notification_list(
    status: OutboxStatus | None,
    limit: int,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = NotificationStore(settings)
    await store.initialize()
    records = await store.list(scope=scope, status=status, limit=limit)
    if not records:
        renderer.render_status("No notifications found.", style="yellow")
        return
    renderer.render_status("\n\n".join(_render_notification(item) for item in records), style="")


async def _notification_show(
    notification_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = NotificationStore(settings)
    await store.initialize()
    renderer.render_status(
        _render_notification(await store.get(notification_id, scope=scope)), style=""
    )


async def _notification_retry(
    outbox_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = NotificationStore(settings)
    await store.initialize()
    entry = await store.retry(outbox_id, scope=scope)
    renderer.render_status(f"{entry.id}: {entry.status}", style="green")


async def _notification_resolve(
    outbox_id: str,
    resolution: ResolutionDisposition,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = NotificationStore(settings)
    await store.initialize()
    entry = await store.resolve(
        outbox_id,
        scope=scope,
        disposition=resolution,
        actor="ricky_notification_cli",
    )
    renderer.render_status(f"{entry.id}: {entry.status}", style="green")


async def _notification_cancel(
    outbox_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = NotificationStore(settings)
    await store.initialize()
    entry = await store.cancel(outbox_id, scope=scope)
    renderer.render_status(f"{entry.id}: {entry.status}", style="green")


def _render_notification(record: NotificationRecord) -> str:
    request = record.request
    outbox = record.outbox
    lines = [
        f"notification: {request.id}",
        f"outbox: {outbox.id}",
        f"status: {outbox.status}",
        f"route: {request.route}",
        f"urgency: {request.urgency}",
        f"source: {request.source_kind}/{request.source_id}",
        f"profiles: {', '.join(request.profile_label.required_profiles)}",
        f"created: {request.created_at.isoformat()}",
    ]
    if request.title is not None:
        lines.append(f"title: {request.title}")
    lines.append(f"body: {request.body}")
    if request.correlations:
        lines.append("correlations:")
        lines.extend(
            f"  - {item.kind}/{item.id} revision={item.revision} "
            "profiles=" + ",".join(item.profile_label.required_profiles)
            for item in request.correlations
        )
    if outbox.error is not None:
        lines.append(f"error: {outbox.error}")
    if outbox.platform_message_id is not None:
        lines.append(f"platform_message_id: {outbox.platform_message_id}")
    return "\n".join(lines)


def _operator_profile_scope(settings: RickySettings) -> ProfileScope:
    return settings.resolve_profile_scope(
        settings.profiles.default,
        access_profiles=settings.profiles.enabled,
    )


def _run_notification_command(
    factory: Callable[[CliRenderer], Coroutine[Any, Any, None]],
) -> None:
    renderer = CliRenderer()
    try:
        asyncio.run(factory(renderer))
    except (NotificationStoreError, ValueError, OSError) as exc:
        renderer.render_error(f"Notification error: {exc}")
        raise typer.Exit(1) from exc


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


async def _job_list(
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    registry = JobRegistry(settings, profile_scope=profile_scope)
    jobs, errors = registry.discover()
    if jobs:
        renderer.render_status(
            "\n".join(
                f"{job.resource.qualified}: {job.spec.description} "
                f"[{job.spec.provider}/{job.spec.model}]"
                for job in jobs
            ),
            style="",
        )
    else:
        renderer.render_status("No jobs found.", style="yellow")
    for error in errors:
        renderer.render_error(f"{error.source_path}: {error.message}")


async def _job_validate(
    name: str | None,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    registry = JobRegistry(settings, profile_scope=profile_scope)
    if name is not None:
        jobs = [registry.load(name)]
        errors = []
    else:
        jobs, errors = registry.discover()
    failed = bool(errors)
    for error in errors:
        renderer.render_error(f"{error.source_path}: {error.message}")
    for job in jobs:
        try:
            await _validate_job_tools(settings, job, profile_scope)
        except ValueError as exc:
            failed = True
            renderer.render_error(f"{job.spec.name}: {exc}")
        else:
            renderer.render_status(f"{job.spec.name}: valid and available", style="green")
    if failed:
        raise typer.Exit(2)


async def _job_show(
    name: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    job = JobRegistry(settings, profile_scope=profile_scope).load(name)
    renderer.render_status(render_job(job), style="")
    try:
        await _validate_job_tools(settings, job, profile_scope)
    except ValueError as exc:
        renderer.render_status(f"tool availability: unavailable ({exc})", style="yellow")
    else:
        renderer.render_status("tool availability: available", style="green")


async def _validate_job_tools(
    settings: RickySettings,
    job: Any,
    profile_scope: ProfileScope,
) -> None:
    await JobRunner(settings).validate(job.resource.qualified, profile_scope=profile_scope)


async def _job_run(
    name: str,
    dry_run: bool,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    sink, saw_text = _job_event_renderer(renderer)
    run = await JobRunner(settings, event_sink=sink).run(
        name,
        profile_scope=profile_scope,
        dry_run=dry_run,
    )
    renderer.finish_stream()
    if not saw_text[0] and run.final_message:
        renderer.render_status(run.final_message, style="")
    if run.error:
        renderer.render_error(run.error)
    renderer.render_status(
        f"Job run {run.id}: {run.outcome}",
        style="green" if run.outcome == "succeeded" else "yellow",
    )
    _raise_for_job_outcome(run.outcome)


async def _job_once(
    goal: str,
    tools: list[str],
    provider: str | None,
    model: str | None,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    sink, saw_text = _job_event_renderer(renderer)
    run = await JobRunner(settings, event_sink=sink).once(
        goal,
        profile_scope=profile_scope,
        tools=tools,
        provider_name=provider,
        model=model,
    )
    renderer.finish_stream()
    if not saw_text[0] and run.final_message:
        renderer.render_status(run.final_message, style="")
    if run.error:
        renderer.render_error(run.error)
    renderer.render_status(
        f"Job run {run.id}: {run.outcome}",
        style="green" if run.outcome == "succeeded" else "yellow",
    )
    _raise_for_job_outcome(run.outcome)


async def _job_history(
    job_name: str | None,
    limit: int,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    if job_name is not None:
        job_name = (
            JobRegistry(settings, profile_scope=profile_scope).load(job_name).resource.qualified
        )
    store = JobRunStore(settings)
    await store.initialize()
    runs = await store.list(scope=profile_scope, job_name=job_name, limit=limit)
    if not runs:
        renderer.render_status("No job runs found.", style="yellow")
        return
    renderer.render_status(
        "\n".join(
            f"{run.id}  {run.started_at.isoformat()}  "
            f"{run.job_name or '(ad-hoc)'}  {run.outcome or 'running'}"
            for run in runs
        ),
        style="",
    )


async def _job_report(
    run_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = JobRunStore(settings)
    await store.initialize()
    renderer.render_status(render_run(await store.get(run_id, scope=profile_scope)), style="")


async def _job_action_show(
    action_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = JobRunStore(settings)
    await store.initialize()
    action = await store.get_action(action_id, scope=profile_scope)
    renderer.render_status(
        "\n".join(
            [
                f"action: {action.id}",
                f"job/run: {action.job_name}/{action.run_id}",
                f"status: {action.status}",
                f"operation: {action.operation}",
                f"target: {action.target}",
                f"occurrence: {action.occurrence}",
                f"summary: {action.summary}",
                f"provider reference: {action.provider_reference or '-'}",
            ]
        ),
        style="",
    )


async def _job_action_resolve(
    action_id: str,
    disposition: Literal["performed", "not_performed"],
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    store = JobRunStore(settings)
    await store.initialize()
    action, resolution = await store.reconcile_action(
        action_id,
        disposition,
        scope=profile_scope,
    )
    renderer.render_status(
        f"Resolved {action.id} as {action.status}; audit record {resolution.id} appended.",
        style="green",
    )


def _schedule_service(
    project: Path | None = None,
    *,
    profile: str | None = None,
    access_profiles: list[str] | tuple[str, ...] = (),
    all_profiles_by_default: bool = False,
) -> ScheduleService:
    root = find_project_root(project) if project is not None else find_project_root()
    settings = load_settings()
    if all_profiles_by_default and profile is None and not access_profiles:
        access_profiles = settings.profiles.enabled
    profile_scope = settings.resolve_profile_scope(profile, access_profiles=access_profiles)
    return ScheduleService(
        settings,
        profile_scope=profile_scope,
        project_root=root,
    )


async def _schedule_list(
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    service = _schedule_service(
        profile=profile,
        access_profiles=access_profiles,
        all_profiles_by_default=True,
    )
    renderer.render_status(render_schedule_list(await service.list()), style="")


async def _schedule_show(
    schedule_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    inspection = await _schedule_service(profile=profile, access_profiles=access_profiles).show(
        schedule_id
    )
    renderer.render_status(render_schedule(inspection.schedule, inspection), style="")


async def _schedule_add(
    job_name: str,
    cron: str,
    project: Path | None,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    service = _schedule_service(project, profile=profile, access_profiles=access_profiles)
    schedule = await service.create(
        job_name,
        cron,
        profile_scope=service.profile_scope,
        project_root=project,
    )
    job = JobRegistry(
        service.settings,
        profile_scope=schedule.profile_scope,
    ).load(schedule.job_name)
    renderer.render_status(render_approval(schedule, job), style="yellow")
    renderer.render_status(
        f"Desired schedule created. It is not installed; run 'ricky schedule sync'.\n"
        f"{render_schedule(schedule)}",
        style="green",
    )


async def _schedule_set(
    schedule_id: str,
    cron: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    schedule = await _schedule_service(
        profile=profile, access_profiles=access_profiles
    ).update_cron(schedule_id, cron)
    renderer.render_status(
        f"Desired cron updated; approve the new timing, then sync to install it.\n"
        f"{render_schedule(schedule)}",
        style="green",
    )


async def _schedule_enabled(
    schedule_id: str,
    enabled: bool,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    schedule = await _schedule_service(
        profile=profile, access_profiles=access_profiles
    ).set_enabled(schedule_id, enabled)
    renderer.render_status(
        f"Desired schedule {'enabled' if enabled else 'disabled'}; installed cron is "
        f"unchanged until sync.\n{render_schedule(schedule)}",
        style="green",
    )


async def _schedule_remove(
    schedule_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    removed = await _schedule_service(profile=profile, access_profiles=access_profiles).remove(
        schedule_id
    )
    renderer.render_status(
        f"Removed desired schedule {removed.id}; installed cron is unchanged until sync.",
        style="yellow",
    )


async def _schedule_approve(
    schedule_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    service = _schedule_service(
        profile=profile,
        access_profiles=access_profiles,
        all_profiles_by_default=True,
    )
    before, schedule, changes = await service.approve(schedule_id)
    job = JobRegistry(
        service.settings,
        profile_scope=schedule.profile_scope,
    ).load(schedule.job_name)
    renderer.render_status(
        f"Previous state: {before.state}\n"
        f"spec digest: {before.schedule.approved_spec_digest} -> "
        f"{schedule.approved_spec_digest}\n"
        f"runtime revision: {before.schedule.approved_runtime_policy_digest} -> "
        f"{schedule.approved_runtime_policy_digest}\n"
        f"material changes:\n- " + "\n- ".join(changes) + f"\n\n{render_approval(schedule, job)}",
        style="yellow",
    )
    renderer.render_status(
        "Approval pins updated; installed cron is unchanged until sync.",
        style="green",
    )


async def _schedule_refresh(
    schedule_id: str,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    service = _schedule_service(
        profile=profile,
        access_profiles=access_profiles,
        all_profiles_by_default=True,
    )
    before, schedule, changes = await service.refresh(schedule_id)
    renderer.render_status(
        f"Previous state: {before.state}\n"
        f"spec digest: {before.schedule.approved_spec_digest} -> "
        f"{schedule.approved_spec_digest}\n"
        f"runtime revision: {before.schedule.approved_runtime_policy_digest} -> "
        f"{schedule.approved_runtime_policy_digest}\n"
        f"validated changes:\n- " + "\n- ".join(changes),
        style="yellow",
    )
    renderer.render_status(
        "Execution revision refreshed; installed cron is unchanged until sync.",
        style="green",
    )


async def _schedule_sync(
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    service = _schedule_service(
        profile=profile,
        access_profiles=access_profiles,
        all_profiles_by_default=True,
    )
    renderer.render_status(render_sync(await service.sync()), style="green")


async def _schedule_doctor(
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    report = await _schedule_service(
        profile=profile,
        access_profiles=access_profiles,
        all_profiles_by_default=True,
    ).doctor()
    renderer.render_status(render_doctor(report), style="green" if report.healthy else "yellow")
    if not report.healthy:
        raise typer.Exit(1)


async def _schedule_uninstall(
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    backup = await _schedule_service(
        profile=profile,
        access_profiles=access_profiles,
        all_profiles_by_default=True,
    ).uninstall()
    renderer.render_status(
        "Ricky's managed crontab block was removed and verified; schedules.toml was retained."
        + (f" Backup: {backup}" if backup else " No installed block was present."),
        style="green",
    )


async def _schedule_invoke(
    schedule_id: str,
    project: Path,
    profile: str | None,
    access_profiles: list[str],
    renderer: CliRenderer,
) -> None:
    root = project.expanduser().resolve()
    run = await _schedule_service(
        root,
        profile=profile,
        access_profiles=access_profiles,
    ).invoke(schedule_id)
    if run.final_message:
        renderer.render_status(run.final_message, style="")
    if run.error:
        renderer.render_error(run.error)
    renderer.render_status(
        f"Scheduled job run {run.id}: {run.outcome}",
        style="green" if run.outcome == "succeeded" else "yellow",
    )
    _raise_for_job_outcome(run.outcome)


def _raise_for_job_outcome(outcome: str | None) -> None:
    if outcome == "succeeded":
        return
    if outcome == "skipped_locked":
        raise typer.Exit(10)
    if outcome == "budget_exceeded":
        raise typer.Exit(30)
    if outcome == "approval_required":
        raise typer.Exit(20)
    raise typer.Exit(40)


def _job_event_renderer(renderer: CliRenderer):
    saw_text = [False]

    async def emit(event: Any) -> None:
        if isinstance(event, TextDeltaEvent):
            saw_text[0] = True
        renderer.render_event(event)

    return emit, saw_text


def _run_job_command(
    factory: Callable[[CliRenderer], Coroutine[Any, Any, None]],
) -> None:
    renderer = CliRenderer()
    try:
        asyncio.run(_run_job_with_signals(factory, renderer))
    except ProviderError as exc:
        renderer.render_error(f"Provider error: {exc}")
        raise typer.Exit(40) from exc
    except (JobStoreError, ValueError, OSError) as exc:
        renderer.render_error(f"Job error: {exc}")
        raise typer.Exit(2) from exc


def _run_schedule_command(
    factory: Callable[[CliRenderer], Coroutine[Any, Any, None]],
) -> None:
    renderer = CliRenderer()
    try:
        asyncio.run(_run_job_with_signals(factory, renderer))
    except ProviderError as exc:
        renderer.render_error(f"Provider error: {exc}")
        raise typer.Exit(40) from exc
    except (
        CronError,
        JobConfigurationError,
        JobStoreError,
        ScheduleServiceError,
        ScheduleStoreError,
        ValueError,
        OSError,
    ) as exc:
        renderer.render_error(f"Schedule error: {exc}")
        raise typer.Exit(2) from exc


async def _run_job_with_signals(
    factory: Callable[[CliRenderer], Coroutine[Any, Any, None]],
    renderer: CliRenderer,
) -> None:
    """Turn SIGTERM into normal task cancellation so runs can finalize."""

    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    installed = False
    if task is not None:
        try:
            loop.add_signal_handler(signal.SIGTERM, task.cancel)
            installed = True
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await factory(renderer)
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGTERM)


def _prompt_skill_names(skill_registry: SkillRegistry) -> set[str]:
    """Return names a workflow step may activate."""
    return skill_registry.identifiers()


async def _chat(
    provider_name: str | None,
    model: str | None,
    renderer: CliRenderer,
    *,
    profile_name: str | None = None,
    access_profiles: tuple[str, ...] | list[str] = (),
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(
        profile_name,
        access_profiles=access_profiles,
    )
    selection = settings.resolve_profile_selection(profile_scope, provider_name, model)
    provider = create_provider(
        selection.provider,
        settings.resolve_profile_runtime_settings(profile_scope),
    )
    session = AgentSession.create(
        settings,
        profile_scope=profile_scope,
        provider=selection.provider,
        model=selection.model,
    )
    async with build_session_runtime(
        settings,
        session=session,
        provider=provider,
        permission_responder=renderer.request_permission,
        approval_responder=renderer.request_workflow_approval,
        slack_factory=slack_toolset,
        gmail_factory=gmail_toolset,
        gcal_factory=gcal_toolset,
        web_search_factory=web_search_toolset,
        google_auth_factory=GoogleAuth,
        browser_factory=BrowserService.create,
        unlock_responder=renderer.request_protected_unlock,
        secure_value_responder=renderer.request_secure_value,
        destination_responder=renderer.request_protected_destination,
        skill_factory=discover_skills,
        registry_factory=ToolRegistry,
    ) as runtime:
        controller = ChatController(
            agent_loop=runtime.agent_loop,
            session=session,
            settings=settings,
            renderer=renderer,
            skill_registry=runtime.skill_registry,
            memory=runtime.memory,
            durable_tasks=runtime.durable_tasks,
            workflow_runner=runtime.workflow_runner,
            workflow_registry=runtime.workflow_registry,
            session_artifacts=runtime.capabilities.session_artifacts,
            session_media=runtime.capabilities.session_media,
        )
        await controller.run()


async def _ask(
    prompt: str,
    provider_name: str | None,
    model: str | None,
    temperature: float | None,
    max_tokens: int | None,
    renderer: CliRenderer,
    *,
    profile_name: str | None = None,
    access_profiles: tuple[str, ...] | list[str] = (),
) -> None:
    settings = load_settings()
    profile_scope = settings.resolve_profile_scope(
        profile_name,
        access_profiles=access_profiles,
    )
    selection = settings.resolve_profile_selection(profile_scope, provider_name, model)
    provider = create_provider(
        selection.provider,
        settings.resolve_profile_runtime_settings(profile_scope),
    )
    request = CompletionRequest(
        model=selection.model,
        messages=[Message.text("user", prompt)],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    try:
        async for event in provider.stream(request):
            if isinstance(event, TextDelta):
                renderer.console.file.write(event.delta)
                renderer.console.file.flush()
            elif isinstance(event, MessageDone):
                renderer.console.file.write("\n")
                renderer.console.file.flush()
    finally:
        await provider.aclose()


@workflow_app.command("list")
def workflow_list(
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """List discovered workflows and any load errors."""
    _run_with_provider_errors(
        lambda renderer: _workflow_list(profile, access_profiles or [], renderer)
    )


@workflow_app.command("validate")
def workflow_validate(
    name: str = typer.Argument(..., help="Workflow bundle name."),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Run the workflow linter and print every error."""
    _run_with_provider_errors(
        lambda renderer: _workflow_validate(name, profile, access_profiles or [], renderer)
    )


@workflow_app.command("show")
def workflow_show(
    name: str = typer.Argument(..., help="Workflow name."),
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Render the workflow's FSM: steps and the transition table."""
    _run_with_provider_errors(
        lambda renderer: _workflow_show(name, profile, access_profiles or [], renderer)
    )


_RUN_ARGS_OPTION = typer.Option(
    [],
    "--args",
    "-a",
    help="Typed trigger arguments as key=JSON; repeatable.",
)


@workflow_app.command("run")
def workflow_run(
    name: str = typer.Argument(..., help="workflow name."),
    args: list[str] = _RUN_ARGS_OPTION,
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Run one workflow and persist recoverable checkpoints."""

    _run_with_provider_errors(
        lambda renderer: _workflow_run(name, args, profile, access_profiles or [], renderer)
    )


@workflow_app.command("status")
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

    _run_with_provider_errors(
        lambda renderer: _workflow_status_v2(
            run_id, scope, profile, access_profiles or [], renderer
        )
    )


@workflow_app.command("resume")
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

    _run_with_provider_errors(
        lambda renderer: _workflow_resume_v2(
            run_id, scope, profile, access_profiles or [], renderer
        )
    )


@workflow_app.command("abandon")
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

    _run_with_provider_errors(
        lambda renderer: _workflow_abandon_v2(
            run_id, scope, profile, access_profiles or [], renderer
        )
    )


@workflow_app.command("reconcile")
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

    _run_with_provider_errors(
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


@workflow_app.command("dryrun")
def workflow_dryrun(
    name: str = typer.Argument(..., help="Workflow name."),
    args: list[str] = _DRYRUN_ARGS_OPTION,
    fixtures: Path | None = _DRYRUN_FIXTURES_OPTION,
    profile: str | None = _PROFILE_OPTION,
    access_profiles: list[str] | None = _ACCESS_PROFILE_OPTION,
) -> None:
    """Run the workflow with no side effect: mutating tools and checks are logged."""
    _run_with_provider_errors(
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
        registry, _, _, _, _ = await _workflow_context(
            settings, resources, profile_scope=profile_scope
        )
        renderer.render_workflow_list(registry)


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
        _, tools, skill_registry, _, _ = await _workflow_context(
            settings, resources, profile_scope=profile_scope
        )
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
            tool_registry=ToolRegistry(tools),
            skill_names=_prompt_skill_names(skill_registry),
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
        registry, tools, skill_registry, _, _ = await _workflow_context(
            settings, resources, profile_scope=profile_scope
        )
        spec = registry.get(name)
        if spec is None:
            renderer.render_error(
                f"Workflow '{name}' is not loaded. Run 'ricky workflow list' for "
                "loaded workflows and load errors, or 'ricky workflow validate "
                f"{name}' for its lint report."
            )
            raise typer.Exit(1)
        compiled = compile_workflow(
            spec,
            tool_registry=ToolRegistry(tools),
            skill_names=_prompt_skill_names(skill_registry),
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
        registry, tools, skill_registry, _, task_store = await _workflow_context(
            settings, resources, profile_scope=profile_scope
        )
        loaded = registry.loaded(name)
        if loaded is None:
            renderer.render_error(f"Workflow '{name}' is not a loaded workflow.")
            raise typer.Exit(1)
        args = _parse_workflow_args(arg_tokens)
        resolve_trigger_args(loaded.spec, args)
        compiled = compile_workflow(
            loaded.spec,
            tool_registry=ToolRegistry(tools),
            skill_names=_prompt_skill_names(skill_registry),
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
        resources.push_async_callback(task_store.release_session_leases, session.id)
        store = WorkflowRunStore(settings)
        runner = WorkflowRunner(
            graph=compiled.graph,
            provider=provider,
            tool_registry=ToolRegistry(tools),
            settings=runtime_settings,
            session=session,
            source=_workflow_source(loaded.bundle_path, loaded.resource),
            permission_engine=PermissionEngine(durable_task_policy()),
            permission_responder=renderer.request_permission,
            approval_responder=renderer.request_workflow_approval,
            emit_event=_event_renderer(renderer),
            checkpoint=store.save,
            skill_bodies=_workflow_skill_bodies(loaded.spec, skill_registry),
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
        registry, tools, skill_registry, _, task_store = await _workflow_context(
            settings,
            resources,
            profile_scope=run.profile_scope,
        )
        loaded = registry.loaded(run.source.resource.qualified)
        if loaded is None:
            raise ValueError(f"cannot resume: workflow '{run.workflow_name}' is not loaded")
        compiled = compile_workflow(
            loaded.spec,
            tool_registry=ToolRegistry(tools),
            skill_names=_prompt_skill_names(skill_registry),
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
        resources.push_async_callback(task_store.release_session_leases, session.id)
        runner = WorkflowRunner(
            graph=compiled.graph,
            provider=provider,
            tool_registry=ToolRegistry(tools),
            settings=runtime_settings,
            session=session,
            source=_workflow_source(loaded.bundle_path, loaded.resource),
            permission_engine=PermissionEngine(durable_task_policy()),
            permission_responder=renderer.request_permission,
            approval_responder=renderer.request_workflow_approval,
            emit_event=_event_renderer(renderer),
            checkpoint=store.save,
            skill_bodies=_workflow_skill_bodies(loaded.spec, skill_registry),
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
        registry, tools, skill_registry, _, task_store = await _workflow_context(
            settings, resources, profile_scope=profile_scope
        )
        loaded = registry.loaded(name)
        if loaded is None:
            renderer.render_error(f"Workflow '{name}' is not loaded.")
            raise typer.Exit(1)
        trigger_args = _parse_workflow_args(arg_tokens)
        resolve_trigger_args(loaded.spec, trigger_args)
        fixtures = _load_fixtures(fixtures_path) if fixtures_path is not None else {}
        compiled = compile_workflow(
            loaded.spec,
            tool_registry=ToolRegistry(tools),
            skill_names=_prompt_skill_names(skill_registry),
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
            resources.push_async_callback(task_store.release_session_leases, session.id)
            store = WorkflowRunStore(dry_settings)
            runner = WorkflowRunner(
                graph=compiled.graph,
                provider=provider,
                tool_registry=ToolRegistry(tools),
                settings=runtime_settings,
                session=session,
                source=_workflow_source(loaded.bundle_path, loaded.resource),
                permission_engine=PermissionEngine(durable_task_policy()),
                permission_responder=renderer.request_permission,
                approval_responder=renderer.preview_and_deny_workflow_approval,
                emit_event=_event_renderer(renderer),
                checkpoint=store.save,
                skill_bodies=_workflow_skill_bodies(loaded.spec, skill_registry),
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
) -> tuple[
    WorkflowRegistry,
    list[Tool],
    SkillRegistry,
    MemoryStore | None,
    ScopedDurableTaskStore,
]:
    """Build the tool pool, skill registry, and workflow registry for CLI commands."""
    profile_scope = profile_scope or settings.resolve_profile_scope()
    selection = settings.resolve_profile_selection(profile_scope)
    session = AgentSession.create(
        settings,
        profile_scope=profile_scope,
        provider=selection.provider,
        model=selection.model,
    )
    capabilities = await resources.enter_async_context(
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
    registry = capabilities.workflow_registry or WorkflowRegistry()
    return (
        registry,
        list(capabilities.tools),
        capabilities.skill_registry,
        capabilities.memory,
        capabilities.durable_tasks,
    )


_FIXTURE_KEY_RE = re.compile(
    r"^(?P<step>[a-z0-9][a-z0-9_-]{0,63})(?:\[(?P<index>0|[1-9][0-9]*)\])?$"
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


def _run_with_provider_errors[T](
    factory: Callable[[CliRenderer], Coroutine[Any, Any, T]],
) -> T:
    renderer = CliRenderer()
    try:
        return asyncio.run(factory(renderer))
    except ProviderError as exc:
        renderer.render_error(f"Provider error: {exc}")
        raise typer.Exit(1) from exc
    except (GoogleAuthError, GmailError, GcalError) as exc:
        renderer.render_error(str(exc))
        raise typer.Exit(1) from exc
    except ValueError as exc:
        renderer.render_error(str(exc))
        raise typer.Exit(2) from exc


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
