"""Deterministic job-only context sections."""

from __future__ import annotations

import json

from ricky.agent.prompts import MOBILE_MARKDOWN_GUIDANCE
from ricky.executions.browser import BrowserExecutionScope
from ricky.jobs.spec import JobSpec


def browser_system_sections(scope: BrowserExecutionScope | None) -> dict[str, str]:
    """Expose only the browser identities already pinned for this execution."""
    if scope is None:
        return {}
    resources = [
        {
            "resource": pin.resource.qualified,
            "authenticated_origins": list(pin.authenticated_origin_ceiling),
        }
        for pin in scope.resources
    ]
    return {
        "execution_browser": (
            f"Browser execution mode: {scope.mode}.\n"
            f"Authorized browser resources: {json.dumps(resources)}\n"
            "To open a configured browser, pass the exact resource value above to "
            "browser_session_open_resource. A profile name alone is not a resource. "
            "Resource discovery is not required; these identities are already pinned "
            "by this execution's contract. Use only exposed tools within its guards.\n"
            f"Ephemeral browser sessions permitted: {scope.allow_ephemeral}. "
            "An ephemeral session does not contain the configured browser's login."
        )
    }


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
