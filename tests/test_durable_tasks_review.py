"""Parked review tasks: composite parking, idempotency, and job legality."""

from __future__ import annotations

from pathlib import Path

import pytest

from ricky.agent.session import AgentSession
from ricky.config import RickySettings
from ricky.durable_tasks.artifacts import TaskArtifactStore
from ricky.durable_tasks.review import ReviewArtifact, park_for_review, review_tag
from ricky.durable_tasks.store import DurableTaskStore, TaskStoreError
from ricky.durable_tasks.tools import ParkForReviewResult, durable_task_tools
from ricky.durable_tasks.types import TaskSearchQuery
from ricky.jobs.effects import is_guardable
from ricky.jobs.runner import JobConfigurationError, validate_recurring_tool_profile
from ricky.jobs.spec import JobSpec
from ricky.jobs.store import JobRunStore
from ricky.tools import ToolContext, ToolRegistry

_DEDUPE_KEY = "gmail:work:t-response"
_ARGS: dict[str, str] = {
    "title": "Review reply draft: Please reply",
    "objective": "Review, edit, and send the drafted reply.",
    "closure_criteria": "The reply is sent or explicitly dropped.",
    "current_summary": "A reply draft is ready for review.",
    "next_action": "User: read draft.md, edit it, then send.",
    "dedupe_key": _DEDUPE_KEY,
}


async def _context(tmp_path: Path):
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    store = await DurableTaskStore.create(settings, profile="personal")
    artifacts = TaskArtifactStore(store)
    registry = ToolRegistry(durable_task_tools(store, artifacts))
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="synthetic",
    )
    ctx = ToolContext(cwd=tmp_path, settings=settings, session=session)
    return store, artifacts, registry, ctx


def _artifacts(body: str) -> list[ReviewArtifact]:
    return [
        ReviewArtifact(path="draft.md", content=body),
        ReviewArtifact(path="context.md", content='thread_id = "t-response"\n'),
    ]


async def _park(
    store: DurableTaskStore,
    artifacts: TaskArtifactStore,
    *,
    review_artifacts: list[ReviewArtifact],
    session_id: str,
    current_summary: str = "A reply draft is ready for review.",
    tags: list[str] | None = None,
):
    return await park_for_review(
        store=store,
        artifacts=artifacts,
        title=_ARGS["title"],
        objective=_ARGS["objective"],
        closure_criteria=_ARGS["closure_criteria"],
        current_summary=current_summary,
        next_action=_ARGS["next_action"],
        dedupe_key=_DEDUPE_KEY,
        review_artifacts=review_artifacts,
        executor_id="test",
        session_id=session_id,
        tags=tags or [],
    )


async def test_parking_leaves_a_joint_user_waiting_task_with_no_lease(tmp_path: Path) -> None:
    store, artifacts, _, _ = await _context(tmp_path)
    parked = await _park(
        store,
        artifacts,
        review_artifacts=_artifacts("First draft."),
        session_id="session-1",
        tags=["email-review"],
    )
    assert parked.reused is False
    assert parked.task.execution_mode == "joint"
    assert parked.task.status == "waiting"
    assert parked.task.waiting_on == "user"
    assert parked.task.lease is None
    assert review_tag(_DEDUPE_KEY) in parked.task.tags
    assert "email-review" in parked.task.tags
    assert sorted(entry.path for entry in parked.artifacts) == ["context.md", "draft.md"]
    read = await artifacts.read(parked.task.id, "draft.md")
    assert read.content == "First draft."
    # A later session can claim it, which proves no stale lease survived.
    claimed = await store.claim(
        parked.task.id,
        holder_session_id="session-2",
        authority="joint_work",
        executor_id="test",
    )
    assert claimed.lease is not None


async def test_reparking_the_same_key_refreshes_one_task(tmp_path: Path) -> None:
    store, artifacts, _, _ = await _context(tmp_path)
    first = await _park(
        store,
        artifacts,
        review_artifacts=_artifacts("First draft."),
        session_id="session-1",
    )
    second = await _park(
        store,
        artifacts,
        review_artifacts=_artifacts("Second draft."),
        session_id="session-1",
        current_summary="The draft was refreshed.",
    )
    assert second.reused is True
    assert second.task.id == first.task.id
    assert second.task.status == "waiting"
    assert second.task.waiting_on == "user"
    assert second.task.lease is None
    assert second.task.current_summary == "The draft was refreshed."
    read = await artifacts.read(first.task.id, "draft.md")
    assert read.content == "Second draft."
    open_tasks = await store.search()
    assert len(open_tasks) == 1


async def test_parking_after_completion_creates_a_new_task(tmp_path: Path) -> None:
    store, artifacts, _, _ = await _context(tmp_path)
    first = await _park(
        store,
        artifacts,
        review_artifacts=_artifacts("First draft."),
        session_id="session-1",
    )
    claimed = await store.claim(
        first.task.id,
        holder_session_id="session-2",
        authority="joint_work",
        executor_id="test",
    )
    assert claimed.lease is not None
    await store.complete(
        claimed.id,
        lease=claimed.lease,
        expected_revision=claimed.revision,
        completion_summary="Sent by hand.",
        authority="joint_work",
        executor_id="test",
    )
    second = await _park(
        store,
        artifacts,
        review_artifacts=_artifacts("Later draft."),
        session_id="session-3",
    )
    assert second.reused is False
    assert second.task.id != first.task.id


async def test_artifact_failure_leaves_no_lease_and_no_waiting_task(tmp_path: Path) -> None:
    store, artifacts, _, _ = await _context(tmp_path)
    with pytest.raises((ValueError, TaskStoreError)):
        await _park(
            store,
            artifacts,
            review_artifacts=[ReviewArtifact(path="../escape.md", content="Nope.")],
            session_id="session-1",
        )
    assert await store.search() == []
    closed = await store.search(TaskSearchQuery(include_closed=True))
    assert len(closed) == 1
    assert closed[0].status == "cancelled"
    assert closed[0].lease is None
    assert await artifacts.list(closed[0].id) == []


async def test_tool_parks_without_holding_a_lease(tmp_path: Path) -> None:
    _, _, registry, ctx = await _context(tmp_path)
    result = await registry.dispatch(
        "park_for_review",
        {
            **_ARGS,
            "artifacts": [
                {"path": "draft.md", "content": "Draft body."},
                {"path": "context.md", "content": 'thread_id = "t-response"\n'},
            ],
            "tags": ["email-review"],
        },
        ctx,
    )
    assert not result.is_error
    parked = ParkForReviewResult.model_validate(result.data)
    assert parked.reused is False
    assert parked.task.waiting_on == "user"
    assert parked.task.id not in ctx.session.active_task_leases
    assert result.effect_receipt is not None
    assert result.effect_receipt.disposition == "performed"
    assert result.effect_receipt.provider_reference == parked.task.id


async def test_tool_grant_generalizes_across_subjects(tmp_path: Path) -> None:
    _, _, registry, ctx = await _context(tmp_path)
    tool = registry.get("park_for_review")
    assert tool is not None
    assert tool.risk == "mutating"
    scope = registry.permission_scope("park_for_review", {**_ARGS}, ctx)
    assert scope is not None
    assert scope.params_equal == {}
    assert scope.allow_unconstrained is True


async def test_recurring_jobs_accept_declared_parking_and_refuse_undeclared(
    tmp_path: Path,
) -> None:
    _, _, registry, _ = await _context(tmp_path)
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    task_store = await DurableTaskStore.create(settings, profile="personal")
    tool = registry.get("park_for_review")
    assert tool is not None
    assert is_guardable(tool)
    base = {
        "version": 3,
        "name": "triage",
        "description": "Park drafted replies for review.",
        "tools": {"allow": ["park_for_review"]},
    }
    allowed = validate_recurring_tool_profile(
        registry,
        JobSpec.model_validate({**base, "permissions": {"allow_mutating": ["park_for_review"]}}),
        store=JobRunStore(settings),
        task_store=task_store,
        run_id="run-1",
        dry_run=True,
        profile_scope=settings.resolve_profile_scope(),
    )
    assert allowed.get("park_for_review") is not None
    with pytest.raises(JobConfigurationError, match="missing from allow_mutating"):
        validate_recurring_tool_profile(
            registry,
            JobSpec.model_validate(base),
            store=JobRunStore(settings),
            task_store=task_store,
            run_id="run-1",
            dry_run=True,
            profile_scope=settings.resolve_profile_scope(),
        )
