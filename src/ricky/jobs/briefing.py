"""Deterministic job-only context sections."""

from __future__ import annotations

from ricky.agent.prompts import MOBILE_MARKDOWN_GUIDANCE
from ricky.jobs.spec import JobSpec


def job_system_sections(spec: JobSpec, *, named: bool) -> dict[str, str]:
    """Build bounded system context without prior-run state."""

    mutating = bool(spec.permissions.allow_mutating)
    if named:
        identity = f"job: {spec.name}"
    elif mutating:
        identity = "ad-hoc capability-compiled execution"
    else:
        identity = "ad-hoc read-only job"
    authority = (
        (
            "Use only the tools exposed in this request. Browser tools may navigate and "
            "observe only inside the authored read-oriented browser scope; they cannot fill, "
            "click, upload, download, use protected values, or commit a transaction."
        )
        if spec.browser is not None
        else (
            "Use only the tools exposed in this request. They are read-only. "
            "Do not claim to change external or local state."
        )
        if not mutating
        else (
            "Use only the tools exposed in this request. The exact mutating tools in this "
            "execution were authorized when its capability contract was compiled. Their "
            "use remains subject to the contract's effect budget and harness guards."
        )
    )
    return {
        "job": (
            f"You are executing {identity}. {spec.description}\n"
            f"{authority} Return a concise final report. Put execution status, key findings, "
            "and any required user action first; place supporting detail afterward.\n"
            f"{MOBILE_MARKDOWN_GUIDANCE}"
        )
    }
