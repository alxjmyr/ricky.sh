"""Bounded agent-job runtime with cycle-safe public exports."""

# ruff: noqa: F401 - TYPE_CHECKING imports preserve the public type surface.

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ricky.jobs.registry import JobRegistry, LoadedJob
    from ricky.jobs.runner import JobRunner, runtime_policy_digest
    from ricky.jobs.spec import (
        JobBudget,
        JobContext,
        JobPermissions,
        JobSpec,
        JobTools,
        JobWorkflow,
    )
    from ricky.jobs.store import JobRunStore
    from ricky.jobs.types import (
        ActionResolution,
        JobAction,
        JobApprovalEnvelope,
        JobApprovalTool,
        JobContextEvidence,
        JobRun,
        JobValidationError,
        JobValidationReport,
        ResultNotificationPolicy,
        RunOutcome,
        RunTrigger,
    )

_EXPORTS = {
    "JobRegistry": ("ricky.jobs.registry", "JobRegistry"),
    "LoadedJob": ("ricky.jobs.registry", "LoadedJob"),
    "JobRunner": ("ricky.jobs.runner", "JobRunner"),
    "runtime_policy_digest": ("ricky.jobs.runner", "runtime_policy_digest"),
    "JobBudget": ("ricky.jobs.spec", "JobBudget"),
    "JobContext": ("ricky.jobs.spec", "JobContext"),
    "JobPermissions": ("ricky.jobs.spec", "JobPermissions"),
    "JobSpec": ("ricky.jobs.spec", "JobSpec"),
    "JobTools": ("ricky.jobs.spec", "JobTools"),
    "JobWorkflow": ("ricky.jobs.spec", "JobWorkflow"),
    "JobRunStore": ("ricky.jobs.store", "JobRunStore"),
    "ActionResolution": ("ricky.jobs.types", "ActionResolution"),
    "JobAction": ("ricky.jobs.types", "JobAction"),
    "JobApprovalEnvelope": ("ricky.jobs.types", "JobApprovalEnvelope"),
    "JobApprovalTool": ("ricky.jobs.types", "JobApprovalTool"),
    "JobContextEvidence": ("ricky.jobs.types", "JobContextEvidence"),
    "JobRun": ("ricky.jobs.types", "JobRun"),
    "JobValidationError": ("ricky.jobs.types", "JobValidationError"),
    "JobValidationReport": ("ricky.jobs.types", "JobValidationReport"),
    "ResultNotificationPolicy": ("ricky.jobs.types", "ResultNotificationPolicy"),
    "RunOutcome": ("ricky.jobs.types", "RunOutcome"),
    "RunTrigger": ("ricky.jobs.types", "RunTrigger"),
}

__all__ = [
    "ActionResolution",
    "JobAction",
    "JobApprovalEnvelope",
    "JobApprovalTool",
    "JobBudget",
    "JobContext",
    "JobContextEvidence",
    "JobPermissions",
    "JobRegistry",
    "JobRun",
    "JobRunStore",
    "JobRunner",
    "JobSpec",
    "JobTools",
    "JobWorkflow",
    "JobValidationError",
    "JobValidationReport",
    "LoadedJob",
    "ResultNotificationPolicy",
    "RunOutcome",
    "RunTrigger",
    "runtime_policy_digest",
]


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    value = getattr(import_module(target[0]), target[1])
    globals()[name] = value
    return value
