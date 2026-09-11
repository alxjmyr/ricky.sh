"""Bounded outcome reporting from durable external-effect evidence."""

from __future__ import annotations

from ricky.jobs.store import JobRunStore
from ricky.jobs.types import JobRun
from ricky.profiles import ProfileScope


async def failure_summary(
    run: JobRun,
    summary: str,
    *,
    store: JobRunStore,
    scope: ProfileScope,
    limit: int,
) -> str:
    """Preserve confirmed effects without promoting a failed run to success.

    Use only the exact run's scoped ledger. Model prose and external provider
    references are deliberately excluded from receipt details.
    """

    if run.outcome == "succeeded":
        return summary[:limit]
    actions = await store.actions_for_run(run.id, scope=scope)
    performed = [action for action in actions if action.status == "performed"]
    if not performed:
        return summary[:limit]
    unresolved = sum(action.status in {"reserved", "in_doubt"} for action in actions)
    evidence = f"Run {run.outcome}. Confirmed effects: {len(performed)} performed."
    if unresolved:
        evidence += f" Unresolved: {unresolved}; review required."
    # Reserve space for evidence even when the primary error fills the limit.
    available = max(0, limit - len(evidence) - 2)
    reason_budget = min(len(summary), available - min(512, available // 2))
    reason = summary[:reason_budget]
    if reason_budget and len(summary) > reason_budget:
        reason = reason[:-1] + "…"
    result = evidence + "\n\n" + reason
    for index, action in enumerate(performed):
        detail = f"\n- {action.operation} (action {action.id}): performed."
        omitted = f"\n{len(performed) - index} additional performed receipts omitted."
        remaining_notice = len(omitted) if index + 1 < len(performed) else 0
        if len(result) + len(detail) + remaining_notice > limit:
            if len(result) + len(omitted) <= limit:
                result += omitted
            break
        result += detail
    return result[:limit]
