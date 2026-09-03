"""Plain job renderers independent of any interface library."""

from __future__ import annotations

from ricky.jobs.registry import LoadedJob
from ricky.jobs.types import JobRun


def render_job(job: LoadedJob) -> str:
    """Render one resolved job definition without secrets."""

    spec = job.spec
    workflow = spec.workflow
    execution = (
        f"workflow: {workflow.name}\n"
        f"locked workflow args: {', '.join(sorted(workflow.args)) or '(none)'}\n"
        if workflow is not None
        else f"tools: {', '.join(spec.tools.allow) or '(none)'}\n"
    )
    browser_resource = (
        spec.browser.resource.qualified
        if spec.browser is not None and spec.browser.resource is not None
        else "ephemeral"
    )
    browser = (
        "browser: disabled\n"
        if spec.browser is None
        else (
            "browser: "
            f"{browser_resource}, "
            f"public HTTPS={spec.browser.allow_public_https_research}, "
            f"visual={spec.browser.allow_masked_visual_observations}, "
            f"origins={', '.join(spec.browser.allowed_origins) or '(none)'}\n"
        )
    )
    return (
        f"{spec.name}: {spec.description}\n"
        f"provider/model: {spec.provider}/{spec.model}\n"
        f"{execution}"
        f"{browser}"
        f"standing mutations: {', '.join(spec.permissions.allow_mutating) or '(none)'}\n"
        f"result notification: {spec.result_notification}\n"
        f"context: lineage={spec.context.lineage}, revision={spec.context.revision}\n"
        f"sources: streams={len(spec.stream_sources)}, tasks={len(spec.task_sources)}\n"
        f"budget: {spec.budget.wall_clock_seconds:g}s, {spec.budget.iterations} iterations, "
        f"{spec.budget.max_completion_tokens_per_request} completion tokens/request, "
        f"{spec.budget.effect_calls} effects\n"
        f"digest: {job.digest}"
    )


def render_run(run: JobRun) -> str:
    """Render one persisted run audit record."""

    return (
        f"run: {run.id}\njob: {run.job_name or '(ad-hoc)'}\n"
        f"outcome: {run.outcome or 'running'}\nprovider/model: {run.provider}/{run.model}\n"
        f"started: {run.started_at.isoformat()}\n"
        f"finished: {run.finished_at.isoformat() if run.finished_at else '-'}\n"
        f"iterations: {run.iterations}\n"
        f"dry run: {run.dry_run}\neffect calls: {run.effect_calls}\n"
        f"result notification: {run.result_notification}\n"
        f"context: lineage={run.context_lineage or '-'}, revision={run.context_revision or '-'}\n"
        f"trigger: {run.trigger}{f' ({run.trigger_id})' if run.trigger_id else ''}\n"
        f"workflow: {run.workflow_name or '-'}\n"
        f"workflow run: {run.workflow_run_id or '-'}\n"
        f"workflow status: {run.workflow_status or '-'}\n"
        f"runtime policy: {run.runtime_policy_digest or '-'}\n"
        f"tokens: prompt={run.prompt_tokens}, completion={run.completion_tokens}\n"
        f"transcript: {run.transcript_path or '-'}\n"
        f"error: {run.error or '-'}\n\n{run.final_message or ''}"
    ).rstrip()
