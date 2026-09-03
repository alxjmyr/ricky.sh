"""Plain text renderers for schedule CLI surfaces."""

from __future__ import annotations

import json

from ricky.jobs.registry import LoadedJob
from ricky.schedules.types import (
    ScheduleDoctorReport,
    ScheduleInspection,
    ScheduleSpec,
    ScheduleSyncReport,
)


def render_schedule(schedule: ScheduleSpec, inspection: ScheduleInspection | None = None) -> str:
    state = inspection.state if inspection else "desired state updated; run schedule sync"
    return (
        f"schedule: {schedule.id}\njob: {schedule.job_name}\ncron: {schedule.cron}\n"
        f"enabled: {schedule.enabled}\nstate: {state}\nproject: {schedule.project_root}\n"
        f"profiles: {', '.join(schedule.profile_scope.profiles)} "
        f"(primary: {schedule.profile_scope.primary})\n"
        f"approved spec: {schedule.approved_spec_digest}\n"
        f"validated runtime revision: {schedule.approved_runtime_policy_digest}\n"
        f"approved cron: {schedule.approved_cron or schedule.cron}\n"
        f"updated: {schedule.updated_at.isoformat()}"
    )


def render_approval(schedule: ScheduleSpec, job: LoadedJob) -> str:
    """Render every material unattended-authority input without secrets."""

    spec = job.spec
    authority = schedule.approved_authority
    goal = " ".join(job.goal.split())[:500]
    if authority is None:
        provider = spec.provider
        tools = ", ".join(spec.tools.allow) or "(none)"
        standing_mutations = ", ".join(spec.permissions.allow_mutating) or "(none)"
        source_scopes = "(legacy approval; approve once to record exact scopes)"
        google_accounts = "(legacy approval; approve once to record issued accounts)"
        workflow_args = "(none)"
        browser_scope = "(legacy approval; approve once to record exact browser scope)"
        effect_calls = spec.budget.effect_calls
    else:
        provider = authority.provider
        tools = (
            ", ".join(
                f"{name} ({tool.risk}/{tool.effect_kind}; {tool.contract_digest[:12]})"
                for name, tool in sorted(authority.tools.items())
            )
            or "(none)"
        )
        standing_mutations = ", ".join(authority.mutating_tools) or "(none)"
        source_scopes = (
            "; ".join(f"{name}={scope}" for name, scope in sorted(authority.source_scopes.items()))
            or "(none)"
        )
        google_accounts = (
            ", ".join(f"{name} ({email})" for name, email in authority.google_accounts.items())
            or "(none)"
        )
        workflow_args = (
            json.dumps(authority.workflow_args, sort_keys=True, separators=(",", ":"))
            if authority.workflow_args is not None
            else "(none)"
        )
        browser_scope = authority.browser_scope or "(none)"
        effect_calls = authority.effect_calls
    return (
        f"Approving repeated unattended launch capability:\n"
        f"schedule/job: {schedule.id}/{schedule.job_name}\n"
        f"profiles: {', '.join(schedule.profile_scope.profiles)} "
        f"(primary: {schedule.profile_scope.primary})\n"
        f"goal: {goal}\nprovider/model: {provider}/{spec.model}\n"
        f"resolved tools: {tools}\n"
        f"standing mutations: {standing_mutations}\n"
        f"source scopes: {source_scopes}\n"
        f"issued Google accounts: {google_accounts}\n"
        f"locked workflow arguments: {workflow_args}\n"
        f"browser scope: {browser_scope}\n"
        f"budgets: {spec.budget.wall_clock_seconds:g}s, {spec.budget.iterations} iterations, "
        f"{spec.budget.max_completion_tokens_per_request} tokens/request, "
        f"{effect_calls} approved effects\n"
        f"approved cron: {schedule.approved_cron or schedule.cron}\n"
        f"project: {schedule.project_root}\n"
        f"spec digest: {schedule.approved_spec_digest}\n"
        f"runtime revision digest: {schedule.approved_runtime_policy_digest}"
    )


def render_schedule_list(inspections: list[ScheduleInspection]) -> str:
    if not inspections:
        return "No schedules found."
    return "\n".join(
        f"{item.schedule.id}  {item.schedule.cron}  {item.schedule.job_name}  {item.state}"
        for item in inspections
    )


def render_sync(report: ScheduleSyncReport) -> str:
    return (
        f"crontab: {'changed and verified' if report.changed else 'already current'}\n"
        f"installed: {', '.join(report.installed) or '(none)'}\n"
        f"disabled: {', '.join(report.disabled) or '(none)'}\n"
        f"validation required: {', '.join(report.validation_required) or '(none)'}\n"
        f"lineage decision required: {', '.join(report.lineage_required) or '(none)'}\n"
        f"approval required: {', '.join(report.approval_required) or '(none)'}\n"
        f"unavailable: {', '.join(report.unavailable) or '(none)'}\n"
        f"fragment: {report.fragment_path}\nbackup: {report.backup_path or '(not needed)'}"
    )


def render_doctor(report: ScheduleDoctorReport) -> str:
    lines = [
        f"schedule doctor: {'healthy' if report.healthy else 'issues found'}",
        f"executable: {report.executable_path or '(unavailable)'}",
        f"desired/installed: {report.desired_count}/{report.installed_count}",
    ]
    lines.extend(
        f"{issue.severity}: {issue.code}{f' [{issue.schedule_id}]' if issue.schedule_id else ''}: "
        f"{issue.message}"
        for issue in report.issues
    )
    return "\n".join(lines)
