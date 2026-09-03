"""Offline recurring-agent safety acceptance coverage."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from pydantic import BaseModel, ConfigDict, JsonValue
from typer.testing import CliRunner

import ricky.durable_tasks.upgrade as durable_tasks_upgrade
from ricky.agent.session import AgentSession
from ricky.config import RickySettings
from ricky.durable_tasks.store import (
    DurableTaskStore,
    TaskConflictError,
    TaskLeaseError,
    TaskStoreError,
)
from ricky.durable_tasks.types import DurableTask, TaskSearchQuery
from ricky.durable_tasks.upgrade import DurableTasksUpgradeAdapter
from ricky.interfaces.cli.app import app
from ricky.jobs.batches import persist_batch, prune_batch_payloads
from ricky.jobs.briefing import job_system_sections
from ricky.jobs.effects import EffectIdentity, GuardedEffectTool
from ricky.jobs.escalation import escalate_blocked
from ricky.jobs.runner import JobRunner, _effect_truth_outcome, _ExternalEffectAttempt
from ricky.jobs.sources import (
    CandidateBatch,
    CollectedBatch,
    Disposition,
    SourceItem,
)
from ricky.jobs.spec import JobPermissions, JobSpec, JobTools, TaskSourceSpec
from ricky.jobs.store import (
    SCHEMA_VERSION as JOB_SCHEMA_VERSION,
)
from ricky.jobs.store import (
    JobActionConflictError,
    JobEffectBudgetError,
    JobRunStore,
    JobStoreError,
)
from ricky.jobs.task_pool import DurableTaskPoolAdapter
from ricky.jobs.types import JobAction, JobRun
from ricky.llm import (
    CompletionRequest,
    Message,
    MessageDone,
    StreamEvent,
    TextPart,
    ToolCallPart,
)
from ricky.profiles import ProfileScope
from ricky.project_scope import ProjectScope
from ricky.tools import EffectReceipt, ToolContext, ToolResult
from ricky.tools.integrations.slack.job_source import (
    SlackChannelJobSource,
    SlackChannelSourceConfig,
)

SCOPE = ProfileScope.create("personal")


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": ".ricky",
            "default_provider": "openrouter",
            "providers": {"openrouter": {"default_model": "test-model"}},
            "workflow": {"enabled": False},
            "memory": {"enabled": False},
            "google": {"accounts": {}},
            "google_oauth_clients": {},
        }
    )


def _project(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='recurring-test'\n")


def _bundle(tmp_path: Path, body: str) -> None:
    _project(tmp_path)
    root = tmp_path / "user" / "profiles" / "personal" / "jobs" / "delegate"
    root.mkdir(parents=True)
    (root / "job.toml").write_text(body, encoding="utf-8")


def _base_bundle(extra: str = "", *, tools: list[str] | None = None, effects: int = 0) -> str:
    allowed = ", ".join(json.dumps(name) for name in (tools or []))
    return f"""version = 3
name = "delegate"
description = "Recurring delegation acceptance job."
provider = "openrouter"
model = "test-model"
goal = "Account for every candidate and progress safe work."
[budget]
wall_clock_seconds = 10
iterations = 10
max_completion_tokens_per_request = 256
effect_calls = {effects}
[tools]
allow = [{allowed}]
{extra}
"""


def _run_record(run_id: str = "jobrun_test") -> JobRun:
    return JobRun(
        id=run_id,
        job_name="delegate",
        spec_digest="a" * 64,
        provider="openrouter",
        model="test-model",
        profile_scope=SCOPE,
        session_id="session_test",
        started_at=datetime.now(UTC),
        runtime_policy_digest="b" * 64,
    )


class DynamicProvider:
    name = "dynamic"

    def __init__(self, responder: Any) -> None:
        self.responder = responder
        self.requests: list[CompletionRequest] = []
        self.closed = False

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        yield self.responder(request, len(self.requests) - 1)

    async def aclose(self) -> None:
        self.closed = True


def _call(name: str, args: dict[str, object], stage: int) -> MessageDone:
    return MessageDone(
        message=Message(
            role="assistant",
            content=[ToolCallPart(id=f"call_{stage}_{name}", name=name, args=args)],
        )
    )


def _calls(calls: list[tuple[str, dict[str, object]]], stage: int) -> MessageDone:
    return MessageDone(
        message=Message(
            role="assistant",
            content=[
                ToolCallPart(id=f"call_{stage}_{index}", name=name, args=args)
                for index, (name, args) in enumerate(calls)
            ],
        )
    )


def _done(text: str = "Recurring run complete.") -> MessageDone:
    return MessageDone(message=Message.text("assistant", text))


def _visible_text(request: CompletionRequest) -> str:
    return "\n".join(
        getattr(part, "text", "") for message in request.messages for part in message.content
    )


def test_job_discovery_ignores_a_project_directory_under_every_project_scope(
    tmp_path: Path,
) -> None:
    """A bound project root grants filesystem authority, never job discovery."""

    settings = _settings(tmp_path)
    project = tmp_path / "project"
    stray = project / ".ricky" / "jobs" / "stray"
    stray.mkdir(parents=True)
    (stray / "job.toml").write_text(
        'version = 3\nname = "stray"\ndescription = "Project job."\n'
        'provider = "openrouter"\nmodel = "test-model"\ngoal = "Report."\n'
        "[budget]\nwall_clock_seconds = 10\niterations = 2\n"
        "max_completion_tokens_per_request = 100\neffect_calls = 0\n"
        "[tools]\nallow = []\n",
        encoding="utf-8",
    )

    disabled = JobRunner(settings, project_scope=ProjectScope.disabled())
    bound = JobRunner(settings, project_scope=ProjectScope.bound(project))

    assert disabled.project_root is None
    assert bound.project_root == project.resolve()
    for runner in (disabled, bound):
        registry = runner.registry_for(SCOPE)
        assert not hasattr(registry, "project_dir")
        assert registry.find("stray") is None
        loaded, _ = registry.discover()
        assert [item.resource.qualified for item in loaded] == []


@pytest.mark.asyncio
async def test_open_tags_are_canonical_indexed_and_lease_fenced(tmp_path: Path) -> None:
    store = await DurableTaskStore.create(_settings(tmp_path), profile="personal")
    first = await store.create_task(
        title="Arrange Acme meeting",
        objective="Find a time",
        closure_criteria="Meeting booked",
        execution_mode="agent",
        authority="agent_autonomy",
        executor_id="test",
        tags=[" Customer:Acme ", "meeting-scheduling", "customer:acme"],
    )
    await store.create_task(
        title="Billing follow-up",
        objective="Resolve invoice",
        closure_criteria="Invoice resolved",
        execution_mode="agent",
        authority="agent_autonomy",
        executor_id="test",
        tags=["customer:acme", "topic:billing"],
    )
    assert first.tags == ["customer:acme", "meeting-scheduling"]
    assert len(await store.search(TaskSearchQuery(tags_any=["TOPIC:BILLING"]))) == 1
    assert [
        task.id
        for task in await store.search(
            TaskSearchQuery(tags_all=["customer:acme", "meeting-scheduling"])
        )
    ] == [first.id]
    assert [
        task.id
        for task in await store.search(
            TaskSearchQuery(tags_any=["customer:acme"], tags_none=["topic:billing"])
        )
    ] == [first.id]

    claimed = await store.claim(
        first.id,
        holder_session_id="session_a",
        authority="agent_autonomy",
        executor_id="test",
        expected_revision=first.revision,
    )
    assert claimed.lease is not None
    updated = await store.update_tags(
        first.id,
        tags=["project:ricky"],
        lease=claimed.lease,
        expected_revision=claimed.revision,
        authority="agent_autonomy",
        executor_id="test",
    )
    assert updated.tags == ["project:ricky"]
    assert (await store.activities(first.id))[0].kind == "tags_updated"
    with pytest.raises((TaskConflictError, TaskLeaseError)):
        await store.update_tags(
            first.id,
            tags=["customer:wrong"],
            lease=claimed.lease,
            expected_revision=claimed.revision,
            authority="agent_autonomy",
            executor_id="test",
        )
    assert (await store.get_task(first.id)).tags == ["project:ricky"]


def test_job_v3_rejects_old_versions_duplicate_sources_and_permission_widening() -> None:
    with pytest.raises(ValueError, match="version must be 3"):
        JobSpec.model_validate(
            {
                "version": 1,
                "name": "delegate",
                "description": "old",
                "goal": "report",
            }
        )
    with pytest.raises(ValueError, match="subset"):
        JobSpec.model_validate(
            {
                "version": 3,
                "name": "delegate",
                "description": "bad permissions",
                "goal": "report",
                "permissions": {"allow_mutating": ["slack_send_message"]},
            }
        )
    with pytest.raises(ValueError, match="names must be unique"):
        JobSpec.model_validate(
            {
                "version": 3,
                "name": "delegate",
                "description": "duplicate",
                "goal": "report",
                "task_sources": [{"name": "same"}, {"name": "same"}],
            }
        )


@pytest.mark.asyncio
async def test_task_pool_discovers_unassigned_work_deduplicates_and_cools_down(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    tasks = await DurableTaskStore.create(settings, profile="personal")
    ledger = JobRunStore(settings)
    await ledger.initialize()
    due = await tasks.create_task(
        title="Due meeting",
        objective="Schedule it",
        closure_criteria="Booked",
        execution_mode="agent",
        authority="agent_autonomy",
        executor_id="test",
        priority=5,
        due_at=datetime.now(UTC) - timedelta(hours=1),
        tags=["meeting-scheduling", "customer:acme"],
    )
    later = await tasks.create_task(
        title="Later meeting",
        objective="Schedule it",
        closure_criteria="Booked",
        execution_mode="joint",
        authority="joint_work",
        executor_id="test",
        priority=100,
        tags=["meeting-scheduling"],
    )
    sources = [
        TaskSourceSpec(name="all", tags_all=["meeting-scheduling"], limit=10),
        TaskSourceSpec(name="acme", tags_any=["customer:acme"], limit=10),
    ]
    adapter = DurableTaskPoolAdapter()
    first = await adapter.collect(
        sources,
        task_store=tasks,
        job_store=ledger,
        job_name="delegate",
        profile_scope=SCOPE,
        total_limit=10,
    )
    assert [item.id for item in first.candidates] == [due.id, later.id]
    assert len({item.identity for item in first.candidates}) == 2
    await ledger.record_consideration(
        job_name="delegate",
        task_id=due.id,
        revision=due.revision,
        disposition="not_actionable",
        considered_at=datetime.now(UTC),
        scope=SCOPE,
    )
    cooled = await adapter.collect(
        sources,
        task_store=tasks,
        job_store=ledger,
        job_name="delegate",
        profile_scope=SCOPE,
        total_limit=10,
    )
    assert [item.id for item in cooled.candidates] == [later.id]
    limited = await adapter.collect(
        [sources[0].model_copy(update={"limit": 1})],
        task_store=tasks,
        job_store=ledger,
        job_name="delegate",
        profile_scope=SCOPE,
        total_limit=10,
    )
    assert [item.id for item in limited.candidates] == [later.id]
    returned = await adapter.collect(
        [sources[0].model_copy(update={"reconsider_after_hours": 0})],
        task_store=tasks,
        job_store=ledger,
        job_name="delegate",
        profile_scope=SCOPE,
        total_limit=10,
    )
    assert {item.id for item in returned.candidates} == {due.id, later.id}


@pytest.mark.asyncio
async def test_batches_require_exact_complete_accounting_before_cursor_commit(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = JobRunStore(settings)
    await store.initialize()
    run = _run_record()
    await store.insert(run, scope=SCOPE)
    payload = CollectedBatch(
        items=[
            SourceItem(
                id="C1:1.0:1.0",
                text="hello",
                occurred_at=datetime.now(UTC),
            )
        ],
        input_cursor={"ts": "0.0"},
        next_cursor={"ts": "1.0"},
        upper_bound=datetime.now(UTC),
        complete=True,
    )
    batch = await persist_batch(
        store,
        run_id=run.id,
        job_name="delegate",
        source_name="eng",
        kind="stream",
        payload=payload,
        item_ids=["C1:1.0:1.0"],
        complete=True,
        dry_run=False,
        profile_scope=SCOPE,
        upper_bound=payload.upper_bound,
        input_cursor=payload.input_cursor,
        next_cursor=payload.next_cursor,
    )
    assert Path(batch.payload_path).stat().st_mode & 0o777 == 0o600
    with pytest.raises(JobStoreError, match="unaccounted"):
        await store.verify_run_accounting(run.id, scope=SCOPE)
    with pytest.raises(JobStoreError, match="unknown batch identity"):
        await store.record_disposition(
            Disposition(
                batch_id=batch.id,
                profile_label=SCOPE.label(),
                item_id="wrong",
                kind="declined",
                created_at=datetime.now(UTC),
            ),
            scope=SCOPE,
        )
    await store.record_disposition(
        Disposition(
            batch_id=batch.id,
            profile_label=SCOPE.label(),
            item_id="C1:1.0:1.0",
            kind="declined",
            created_at=datetime.now(UTC),
        ),
        scope=SCOPE,
    )
    await store.verify_run_accounting(run.id, scope=SCOPE)
    finished = run.model_copy(update={"outcome": "succeeded", "finished_at": datetime.now(UTC)})
    await store.finish(finished, scope=SCOPE)
    await store.commit_stream_cursors(run.id, scope=SCOPE)
    assert await store.cursor("delegate", "eng", scope=SCOPE) == {"ts": "1.0"}


@pytest.mark.asyncio
async def test_dry_and_incomplete_batches_never_advance_cursor(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    for dry_run, complete, error in [
        (True, True, "non-dry"),
        (False, False, "incomplete"),
    ]:
        store = JobRunStore(
            settings.model_copy(
                update={"user_data_dir": f"{settings.user_data_dir}-{dry_run}-{complete}"}
            )
        )
        await store.initialize()
        run = _run_record(f"jobrun_{dry_run}_{complete}").model_copy(update={"dry_run": dry_run})
        await store.insert(run, scope=SCOPE)
        payload = CollectedBatch(
            items=[],
            input_cursor={"ts": "0"},
            next_cursor={"ts": "1"},
            upper_bound=datetime.now(UTC),
            complete=complete,
        )
        await persist_batch(
            store,
            run_id=run.id,
            job_name="delegate",
            source_name="eng",
            kind="stream",
            payload=payload,
            item_ids=[],
            complete=complete,
            dry_run=dry_run,
            profile_scope=SCOPE,
            upper_bound=payload.upper_bound,
            input_cursor=payload.input_cursor,
            next_cursor=payload.next_cursor,
        )
        finished = run.model_copy(update={"outcome": "succeeded", "finished_at": datetime.now(UTC)})
        await store.finish(finished, scope=SCOPE)
        with pytest.raises(JobStoreError, match=error):
            await store.commit_stream_cursors(run.id, scope=SCOPE)
        assert await store.cursor("delegate", "eng", scope=SCOPE) is None


@pytest.mark.asyncio
async def test_atomic_effect_reservation_budget_ambiguity_and_user_reconciliation(
    tmp_path: Path,
) -> None:
    store = JobRunStore(_settings(tmp_path))
    await store.initialize()
    run = _run_record()
    await store.insert(run, scope=SCOPE)
    identity = EffectIdentity(
        operation="message.send",
        target="customer:acme",
        occurrence="task_1@1",
        summary="Send follow-up",
        action_key="c" * 64,
    )
    results = await asyncio.gather(
        store.reserve_action(
            job_name="delegate",
            run_id=run.id,
            identity=identity,
            effect_budget=1,
            scope=SCOPE,
        ),
        store.reserve_action(
            job_name="delegate",
            run_id=run.id,
            identity=identity,
            effect_budget=1,
            scope=SCOPE,
        ),
        return_exceptions=True,
    )
    actions = [result for result in results if not isinstance(result, BaseException)]
    failures = [result for result in results if isinstance(result, BaseException)]
    assert len(actions) == 1 and len(failures) == 1
    assert isinstance(failures[0], (JobActionConflictError, JobEffectBudgetError))
    action = actions[0]
    assert not isinstance(action, BaseException)
    in_doubt = await store.resolve_action(
        action.id, "in_doubt", scope=SCOPE, provider_reference=None
    )
    assert in_doubt.status == "in_doubt"
    with pytest.raises(JobActionConflictError):
        await store.reserve_action(
            job_name="delegate",
            run_id=run.id,
            identity=identity,
            effect_budget=2,
            scope=SCOPE,
        )
    reconciled, audit = await store.reconcile_action(action.id, "not_performed", scope=SCOPE)
    assert reconciled.status == "not_performed" and audit.actor == "ricky_job_cli"


class FakeStreamConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str
    initial_lookback_hours: float
    item_limit: int


class FakeStreamAdapter:
    name = "slack_channel"
    Config: ClassVar[type[BaseModel]] = FakeStreamConfig

    def __init__(self, *, complete: bool = True) -> None:
        self.complete = complete
        self.cursors: list[JsonValue] = []

    async def collect(
        self,
        config: BaseModel,
        *,
        cursor: JsonValue,
        upper_bound: datetime,
        limit: int,
    ) -> CollectedBatch:
        del config, limit
        self.cursors.append(cursor)
        return CollectedBatch(
            items=[
                SourceItem(
                    id="C123:10.0:10.0",
                    text="Please follow up",
                    occurred_at=upper_bound - timedelta(seconds=1),
                )
            ],
            input_cursor=cursor,
            next_cursor={"ts": f"{upper_bound.timestamp():.6f}"},
            upper_bound=upper_bound,
            complete=self.complete,
        )


def _stream_responder(request: CompletionRequest, stage: int) -> MessageDone:
    if stage == 0:
        text = _visible_text(request)
        batch_id = re.search(r"batch_[0-9a-f]{32}", text)
        assert batch_id is not None
        return _call(
            "record_item_disposition",
            {
                "batch_id": batch_id.group(),
                "item_id": "C123:10.0:10.0",
                "kind": "declined",
                "summary": "No action needed",
            },
            stage,
        )
    return _done()


@pytest.mark.asyncio
async def test_runner_stream_commit_failure_and_dry_run_semantics(tmp_path: Path) -> None:
    _bundle(
        tmp_path,
        _base_bundle(
            """[[stream_sources]]
name = "eng"
adapter = "slack_channel"
channel_id = "C123"
initial_lookback_hours = 24
item_limit = 10
"""
        ),
    )
    settings = _settings(tmp_path)
    adapter = FakeStreamAdapter()
    first = await JobRunner(settings, project_root=tmp_path, stream_adapters=[adapter]).run(
        "delegate", profile_scope=SCOPE, provider=DynamicProvider(_stream_responder)
    )
    assert first.outcome == "succeeded"
    committed = await JobRunStore(settings).cursor("personal/delegate", "eng", scope=SCOPE)
    assert committed is not None

    failed = await JobRunner(settings, project_root=tmp_path, stream_adapters=[adapter]).run(
        "delegate",
        profile_scope=SCOPE,
        provider=DynamicProvider(lambda _request, _stage: _done("Skipped")),
    )
    assert failed.outcome == "failed"
    assert await JobRunStore(settings).cursor("personal/delegate", "eng", scope=SCOPE) == committed

    dry = await JobRunner(settings, project_root=tmp_path, stream_adapters=[adapter]).run(
        "delegate",
        profile_scope=SCOPE,
        provider=DynamicProvider(_stream_responder),
        dry_run=True,
    )
    assert dry.outcome == "succeeded" and dry.dry_run
    assert await JobRunStore(settings).cursor("personal/delegate", "eng", scope=SCOPE) == committed

    incomplete = FakeStreamAdapter(complete=False)
    partial = await JobRunner(settings, project_root=tmp_path, stream_adapters=[incomplete]).run(
        "delegate", profile_scope=SCOPE, provider=DynamicProvider(_stream_responder)
    )
    assert partial.outcome == "failed"
    assert await JobRunStore(settings).cursor("personal/delegate", "eng", scope=SCOPE) == committed


def _task_responder(request: CompletionRequest, stage: int) -> MessageDone:
    text = _visible_text(request)
    identity = re.search(r"task_[0-9a-f]{32}@([0-9]+)", text)
    batch = re.search(r"batch_[0-9a-f]{32}", text)
    assert identity is not None and batch is not None
    task_id, revision_text = identity.group().split("@")
    revision = int(revision_text)
    if stage == 0:
        return _call(
            "claim_durable_task",
            {"task_id": task_id, "expected_revision": revision},
            stage,
        )
    if stage == 1:
        return _call(
            "update_durable_task_progress",
            {
                "task_id": task_id,
                "current_summary": "Follow-up prepared",
                "next_action": "Send after review",
            },
            stage,
        )
    if stage == 2:
        return _call(
            "record_candidate_disposition",
            {
                "batch_id": batch.group(),
                "candidate_id": identity.group(),
                "kind": "progressed",
                "linked_id": f"{task_id}@{revision + 2}",
            },
            stage,
        )
    if stage == 3:
        return _call("release_durable_task", {"task_id": task_id}, stage)
    return _done("Task progressed across a fresh run.")


@pytest.mark.asyncio
async def test_runner_discovers_and_progresses_unassigned_tagged_task(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    tasks = await DurableTaskStore.create(settings, profile="personal")
    task = await tasks.create_task(
        title="Schedule customer meeting",
        objective="Find a time",
        closure_criteria="Calendar event exists",
        execution_mode="agent",
        authority="agent_autonomy",
        executor_id="test",
        tags=["meeting-scheduling", "customer:acme"],
    )
    _bundle(
        tmp_path,
        _base_bundle(
            """[permissions]
allow_mutating = ["claim_durable_task", "update_durable_task_progress", "release_durable_task"]
[[task_sources]]
name = "meetings"
tags_all = ["meeting-scheduling"]
execution_modes = ["agent", "joint"]
statuses = ["open", "in_progress"]
limit = 10
reconsider_after_hours = 24
""",
            tools=[
                "claim_durable_task",
                "update_durable_task_progress",
                "release_durable_task",
            ],
        ),
    )
    run = await JobRunner(settings, project_root=tmp_path).run(
        "delegate", profile_scope=SCOPE, provider=DynamicProvider(_task_responder)
    )
    assert run.outcome == "succeeded"
    updated = await tasks.get_task(task.id)
    assert updated.current_summary == "Follow-up prepared"
    assert updated.lease is None
    assert not hasattr(updated, "assigned_job")


def _dry_task_responder(request: CompletionRequest, stage: int) -> MessageDone:
    text = _visible_text(request)
    identity = re.search(r"task_[0-9a-f]{32}@[0-9]+", text)
    batch = re.search(r"batch_[0-9a-f]{32}", text)
    assert identity is not None and batch is not None
    task_id, revision = identity.group().split("@")
    if stage == 0:
        return _call(
            "claim_durable_task",
            {"task_id": task_id, "expected_revision": int(revision)},
            stage,
        )
    if stage == 1:
        return _call(
            "record_candidate_disposition",
            {
                "batch_id": batch.group(),
                "candidate_id": identity.group(),
                "kind": "not_actionable",
            },
            stage,
        )
    return _done("Dry reasoning complete")


@pytest.mark.asyncio
async def test_runner_dry_run_cannot_claim_or_change_task_or_fairness(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    tasks = await DurableTaskStore.create(settings, profile="personal")
    task = await tasks.create_task(
        title="Dry task",
        objective="Remain unchanged",
        closure_criteria="No mutation",
        execution_mode="agent",
        authority="agent_autonomy",
        executor_id="test",
        tags=["meeting-scheduling"],
    )
    _bundle(
        tmp_path,
        _base_bundle(
            """[permissions]
allow_mutating = ["claim_durable_task"]
[[task_sources]]
name = "meetings"
tags_all = ["meeting-scheduling"]
limit = 10
""",
            tools=["claim_durable_task"],
        ),
    )
    run = await JobRunner(settings, project_root=tmp_path).run(
        "delegate",
        profile_scope=SCOPE,
        provider=DynamicProvider(_dry_task_responder),
        dry_run=True,
    )
    assert run.outcome == "succeeded"
    assert await tasks.get_task(task.id) == task
    assert (
        await JobRunStore(settings).consideration("delegate", task.id, task.revision, scope=SCOPE)
        is None
    )


class EffectParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    occurrence: str


class FakeEffectTool:
    name = "fake_effect"
    description = "A guarded fake external mutation."
    Params = EffectParams
    risk = "mutating"
    capability_id = None
    effect_kind = "external"
    unattended = "allowed"
    state_guard_id = None

    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        del ctx
        occurrence = str(args["occurrence"])
        return EffectIdentity(
            operation="fake.send",
            target="target",
            occurrence=occurrence,
            summary="Fake send",
            action_key=hashlib.sha256(occurrence.encode()).hexdigest(),
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del params, ctx
        self.calls += 1
        if self.fail:
            raise RuntimeError("ambiguous transport failure")
        return ToolResult(
            content="performed",
            effect_receipt=EffectReceipt(disposition="performed", provider_reference="remote-1"),
        )


class PreflightEffectTool(FakeEffectTool):
    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        if args.get("occurrence") == "invalid":
            raise ValueError("deterministic local validation failed")
        return super().effect_identity(args, ctx)


class MalformedIdentityTool(FakeEffectTool):
    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        del args, ctx
        return cast(Any, {"action_key": "not-a-digest"})


class MissingReceiptEffectTool(FakeEffectTool):
    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del params, ctx
        self.calls += 1
        return ToolResult(content="provider returned without outcome evidence")


class MatrixEffectTool(FakeEffectTool):
    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        self.calls += 1
        occurrence = EffectParams.model_validate(params).occurrence
        disposition = {
            "known-not-performed": "not_performed",
            "ambiguous": "in_doubt",
        }.get(occurrence, "performed")
        return ToolResult(
            content=disposition,
            is_error=disposition != "performed",
            effect_receipt=EffectReceipt(
                disposition=cast(Any, disposition),
                provider_reference=(f"remote-{occurrence}" if disposition == "performed" else None),
            ),
        )


class CancellingEffectTool(FakeEffectTool):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del params, ctx
        self.calls += 1
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class BindingEffectTool(FakeEffectTool):
    def __init__(self) -> None:
        super().__init__()
        self.bound: tuple[str, str] | None = None
        self.settled: list[str] = []

    def bind_effect_action(self, action_id: str, action_key: str) -> None:
        self.bound = (action_id, action_key)

    async def settle_effect_action(self, action_id: str) -> None:
        self.settled.append(action_id)


class DelayedReservationStore:
    def __init__(self, inner: JobRunStore) -> None:
        self.inner = inner
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def reserve_action(self, **kwargs):
        self.started.set()
        await self.release.wait()
        return await self.inner.reserve_action(**kwargs)

    async def resolve_action(self, *args, **kwargs):
        return await self.inner.resolve_action(*args, **kwargs)


class DelayedResolutionStore:
    def __init__(self, inner: JobRunStore) -> None:
        self.inner = inner
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def reserve_action(self, **kwargs):
        return await self.inner.reserve_action(**kwargs)

    async def resolve_action(self, *args, **kwargs):
        result = await self.inner.resolve_action(*args, **kwargs)
        self.started.set()
        await self.release.wait()
        return result


def test_job_briefing_describes_contract_authorized_ad_hoc_mutation() -> None:
    spec = JobSpec(
        version=3,
        name="contract-test",
        description="Capability-compiled ad hoc execution contract.",
        provider="openrouter",
        model="test-model",
        goal="Send the result.",
        tools=JobTools(allow=["fake_effect"]),
        permissions=JobPermissions(allow_mutating=["fake_effect"]),
    )

    briefing = job_system_sections(spec, named=False)["job"]

    assert "ad-hoc capability-compiled execution" in briefing
    assert "authorized when its capability contract was compiled" in briefing
    assert "They are read-only" not in briefing
    assert "execution status, key findings" in briefing
    assert "mobile-first portable Markdown" in briefing
    assert "tables to at most three short columns" in briefing


@pytest.mark.asyncio
async def test_deterministic_effect_preflight_does_not_consume_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = PreflightEffectTool()
    monkeypatch.setattr("ricky.runtime.composition.builtin_tools", lambda: [tool])
    _bundle(
        tmp_path,
        _base_bundle(
            """[permissions]
allow_mutating = ["fake_effect"]
""",
            tools=["fake_effect"],
            effects=1,
        ),
    )

    def responder(_request: CompletionRequest, stage: int) -> MessageDone:
        if stage == 0:
            return _call("fake_effect", {"occurrence": "invalid"}, stage)
        if stage == 1:
            return _call("fake_effect", {"occurrence": "valid"}, stage)
        return _done()

    settings = _settings(tmp_path)
    run = await JobRunner(settings, project_root=tmp_path).run(
        "delegate", profile_scope=SCOPE, provider=DynamicProvider(responder)
    )

    assert run.outcome == "succeeded" and run.effect_calls == 1
    assert tool.calls == 1
    actions = await JobRunStore(settings).list_actions(scope=SCOPE, job_name="delegate")
    assert len(actions) == 1 and actions[0].status == "performed"


@pytest.mark.asyncio
async def test_malformed_effect_identity_fails_before_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = MalformedIdentityTool()
    monkeypatch.setattr("ricky.runtime.composition.builtin_tools", lambda: [tool])
    _bundle(
        tmp_path,
        _base_bundle(
            """[permissions]
allow_mutating = ["fake_effect"]
""",
            tools=["fake_effect"],
            effects=1,
        ),
    )

    def responder(_request: CompletionRequest, stage: int) -> MessageDone:
        return _call("fake_effect", {"occurrence": "bad"}, stage) if stage == 0 else _done()

    settings = _settings(tmp_path)
    run = await JobRunner(settings, project_root=tmp_path).run(
        "delegate", profile_scope=SCOPE, provider=DynamicProvider(responder)
    )

    assert run.outcome == "failed"
    assert run.error == "external effect call did not produce a performed receipt"
    assert tool.calls == 0
    assert await JobRunStore(settings).list_actions(scope=SCOPE, job_name="delegate") == []


@pytest.mark.asyncio
async def test_missing_post_reservation_receipt_is_in_doubt_and_a_contract_error(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = JobRunStore(settings)
    await store.initialize()
    run = _run_record()
    await store.insert(run, scope=SCOPE)
    tool = MissingReceiptEffectTool()
    guarded = GuardedEffectTool(
        cast(Any, tool),
        store=store,
        job_name="delegate",
        run_id=run.id,
        profile_scope=SCOPE,
        effect_budget=1,
    )
    ctx = ToolContext(
        cwd=tmp_path,
        settings=settings,
        session=AgentSession.create(settings, profile_scope=settings.resolve_profile_scope()),
    )

    result = await guarded.run(EffectParams(occurrence="missing"), ctx)

    assert result.is_error
    assert "violated its external-effect contract" in result.content
    assert result.effect_receipt is not None
    assert result.effect_receipt.disposition == "in_doubt"
    actions = await store.list_actions(scope=SCOPE, job_name="delegate")
    assert len(actions) == 1 and actions[0].status == "in_doubt"


@pytest.mark.asyncio
async def test_background_run_cannot_claim_success_without_performed_effect_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = MissingReceiptEffectTool()
    monkeypatch.setattr("ricky.runtime.composition.builtin_tools", lambda: [tool])
    _bundle(
        tmp_path,
        _base_bundle(
            """[permissions]
allow_mutating = ["fake_effect"]
""",
            tools=["fake_effect"],
            effects=1,
        ),
    )

    def responder(_request: CompletionRequest, stage: int) -> MessageDone:
        if stage == 0:
            return _call("fake_effect", {"occurrence": "missing"}, stage)
        return _done("The effect succeeded.")

    settings = _settings(tmp_path)
    run = await JobRunner(settings, project_root=tmp_path).run(
        "delegate",
        profile_scope=SCOPE,
        provider=DynamicProvider(responder),
    )

    assert run.outcome == "uncertain"
    assert run.error is not None and "confirmed receipt" in run.error
    assert run.final_message == "The effect succeeded."
    [action] = await JobRunStore(settings).actions_for_run(run.id, scope=SCOPE)
    assert action.status == "in_doubt"


@pytest.mark.asyncio
async def test_background_rejected_external_call_cannot_be_reported_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = FakeEffectTool()
    monkeypatch.setattr("ricky.runtime.composition.builtin_tools", lambda: [tool])
    _bundle(
        tmp_path,
        _base_bundle(
            """[permissions]
allow_mutating = ["fake_effect"]
""",
            tools=["fake_effect"],
            effects=1,
        ),
    )

    def responder(_request: CompletionRequest, stage: int) -> MessageDone:
        if stage == 0:
            return _call("fake_effect", {}, stage)
        return _done("The effect succeeded.")

    settings = _settings(tmp_path)
    run = await JobRunner(settings, project_root=tmp_path).run(
        "delegate",
        profile_scope=SCOPE,
        provider=DynamicProvider(responder),
    )

    assert run.outcome == "failed"
    assert run.error is not None and "performed receipt" in run.error
    assert tool.calls == 0
    assert await JobRunStore(settings).actions_for_run(run.id, scope=SCOPE) == []


def _matrix_action(action_id: str, status: str) -> JobAction:
    now = datetime.now(UTC)
    return JobAction.model_validate(
        {
            "id": action_id,
            "job_name": "delegate",
            "run_id": "jobrun_matrix",
            "profile_label": SCOPE.label(),
            "action_key": hashlib.sha256(action_id.encode()).hexdigest(),
            "operation": "fake.send",
            "target": "target",
            "occurrence": action_id,
            "summary": "Fake send",
            "status": status,
            "created_at": now,
            "updated_at": now,
        }
    )


def _matrix_attempt(
    call_id: str,
    disposition: str,
    *,
    iteration: int = 1,
    action_id: str | None = None,
    attempt_reason: str | None = None,
    input_key: str | None = None,
) -> _ExternalEffectAttempt:
    return _ExternalEffectAttempt(
        call_id=call_id,
        tool_name="fake_effect",
        iteration=iteration,
        input_digest=hashlib.sha256((input_key or call_id).encode()).hexdigest(),
        disposition=cast(Any, disposition),
        action_id=action_id,
        attempt_reason=cast(Any, attempt_reason),
    )


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("performed_and_rejected", "failed"),
        ("performed_and_not_performed", "failed"),
        ("performed_and_in_doubt", "uncertain"),
        ("two_performed", "succeeded"),
        ("corrected_invalid", "succeeded"),
        ("budget_denial", "failed"),
        ("exact_duplicate_denial", "succeeded"),
    ],
)
def test_external_effect_truth_matrix_is_independent_of_model_prose(
    case: str,
    expected: str,
) -> None:
    performed = _matrix_attempt("performed", "performed", action_id="action_performed")
    cases = {
        "performed_and_rejected": (
            [_matrix_action("action_performed", "performed")],
            [performed, _matrix_attempt("rejected", "rejected")],
        ),
        "performed_and_not_performed": (
            [
                _matrix_action("action_performed", "performed"),
                _matrix_action("action_not_performed", "not_performed"),
            ],
            [
                performed,
                _matrix_attempt(
                    "not_performed",
                    "not_performed",
                    action_id="action_not_performed",
                ),
            ],
        ),
        "performed_and_in_doubt": (
            [
                _matrix_action("action_performed", "performed"),
                _matrix_action("action_in_doubt", "in_doubt"),
            ],
            [
                performed,
                _matrix_attempt("in_doubt", "in_doubt", action_id="action_in_doubt"),
            ],
        ),
        "two_performed": (
            [
                _matrix_action("action_performed", "performed"),
                _matrix_action("action_second", "performed"),
            ],
            [
                performed,
                _matrix_attempt("second", "performed", action_id="action_second"),
            ],
        ),
        "corrected_invalid": (
            [_matrix_action("action_performed", "performed")],
            [
                _matrix_attempt("invalid", "rejected", iteration=1),
                _matrix_attempt(
                    "performed",
                    "performed",
                    iteration=2,
                    action_id="action_performed",
                ),
            ],
        ),
        "budget_denial": (
            [_matrix_action("action_performed", "performed")],
            [
                performed,
                _matrix_attempt(
                    "budget_denied",
                    "not_performed",
                    attempt_reason="denied",
                ),
            ],
        ),
        "exact_duplicate_denial": (
            [_matrix_action("action_performed", "performed")],
            [
                performed,
                _matrix_attempt(
                    "duplicate_denied",
                    "not_performed",
                    attempt_reason="denied",
                    input_key="performed",
                ),
            ],
        ),
    }
    outcome, _error = _effect_truth_outcome(*cases[case])
    assert outcome == expected


@pytest.mark.parametrize(
    ("case", "effect_budget", "expected_outcome", "expected_statuses"),
    [
        ("performed_and_rejected", 2, "failed", ["performed"]),
        (
            "performed_and_not_performed",
            2,
            "failed",
            ["not_performed", "performed"],
        ),
        ("performed_and_in_doubt", 2, "uncertain", ["in_doubt", "performed"]),
        ("two_performed", 2, "succeeded", ["performed", "performed"]),
        ("corrected_invalid", 1, "succeeded", ["performed"]),
        ("budget_denial", 1, "failed", ["performed"]),
    ],
)
@pytest.mark.asyncio
async def test_external_effect_truth_is_reconciled_per_canonical_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    effect_budget: int,
    expected_outcome: str,
    expected_statuses: list[str],
) -> None:
    tool = MatrixEffectTool()
    monkeypatch.setattr("ricky.runtime.composition.builtin_tools", lambda: [tool])
    _bundle(
        tmp_path,
        _base_bundle(
            """[permissions]
allow_mutating = ["fake_effect"]
""",
            tools=["fake_effect"],
            effects=effect_budget,
        ),
    )

    def responder(_request: CompletionRequest, stage: int) -> MessageDone:
        if case == "corrected_invalid":
            if stage == 0:
                return _call("fake_effect", {}, stage)
            if stage == 1:
                return _call("fake_effect", {"occurrence": "corrected"}, stage)
            return _done("All requested effects succeeded.")
        if stage:
            return _done("All requested effects succeeded.")
        calls = {
            "performed_and_rejected": [
                ("fake_effect", {"occurrence": "first"}),
                ("fake_effect", {}),
            ],
            "performed_and_not_performed": [
                ("fake_effect", {"occurrence": "first"}),
                ("fake_effect", {"occurrence": "known-not-performed"}),
            ],
            "performed_and_in_doubt": [
                ("fake_effect", {"occurrence": "first"}),
                ("fake_effect", {"occurrence": "ambiguous"}),
            ],
            "two_performed": [
                ("fake_effect", {"occurrence": "first"}),
                ("fake_effect", {"occurrence": "second"}),
            ],
            "budget_denial": [
                ("fake_effect", {"occurrence": "first"}),
                ("fake_effect", {"occurrence": "second"}),
            ],
        }[case]
        return _calls(calls, stage)

    settings = _settings(tmp_path)
    run = await JobRunner(settings, project_root=tmp_path).run(
        "delegate",
        profile_scope=SCOPE,
        provider=DynamicProvider(responder),
    )

    assert run.outcome == expected_outcome
    assert run.final_message == "All requested effects succeeded."
    actions = await JobRunStore(settings).actions_for_run(run.id, scope=SCOPE)
    assert sorted(action.status for action in actions) == expected_statuses
    if expected_outcome == "succeeded":
        assert run.error is None
    elif expected_outcome == "uncertain":
        assert run.error is not None and "operator reconciliation" in run.error
    else:
        assert run.error is not None and "external effect" in run.error


@pytest.mark.asyncio
async def test_cancellation_after_effect_reservation_becomes_in_doubt(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = JobRunStore(settings)
    await store.initialize()
    run = _run_record()
    await store.insert(run, scope=SCOPE)
    tool = CancellingEffectTool()
    guarded = GuardedEffectTool(
        cast(Any, tool),
        store=store,
        job_name="delegate",
        run_id=run.id,
        profile_scope=SCOPE,
        effect_budget=1,
    )
    ctx = ToolContext(
        cwd=tmp_path,
        settings=settings,
        session=AgentSession.create(settings, profile_scope=settings.resolve_profile_scope()),
    )
    dispatch = asyncio.create_task(guarded.run(EffectParams(occurrence="cancelled"), ctx))
    await tool.started.wait()

    dispatch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dispatch

    actions = await store.list_actions(scope=SCOPE, job_name="delegate")
    assert len(actions) == 1 and actions[0].status == "in_doubt"


@pytest.mark.asyncio
async def test_cancellation_during_reservation_records_no_dispatch(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    inner = JobRunStore(settings)
    await inner.initialize()
    run = _run_record()
    await inner.insert(run, scope=SCOPE)
    store = DelayedReservationStore(inner)
    tool = FakeEffectTool()
    guarded = GuardedEffectTool(
        cast(Any, tool),
        store=cast(Any, store),
        job_name="delegate",
        run_id=run.id,
        profile_scope=SCOPE,
        effect_budget=1,
    )
    ctx = ToolContext(
        cwd=tmp_path,
        settings=settings,
        session=AgentSession.create(settings, profile_scope=settings.resolve_profile_scope()),
    )
    dispatch = asyncio.create_task(guarded.run(EffectParams(occurrence="cancelled"), ctx))
    await store.started.wait()

    dispatch.cancel()
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await dispatch

    assert tool.calls == 0
    actions = await inner.list_actions(scope=SCOPE, job_name="delegate")
    assert len(actions) == 1 and actions[0].status == "not_performed"


@pytest.mark.asyncio
async def test_cancellation_during_post_provider_resolution_finishes_ledger_write(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    inner = JobRunStore(settings)
    await inner.initialize()
    run = _run_record()
    await inner.insert(run, scope=SCOPE)
    store = DelayedResolutionStore(inner)
    tool = BindingEffectTool()
    guarded = GuardedEffectTool(
        cast(Any, tool),
        store=cast(Any, store),
        job_name="delegate",
        run_id=run.id,
        profile_scope=SCOPE,
        effect_budget=1,
    )
    ctx = ToolContext(
        cwd=tmp_path,
        settings=settings,
        session=AgentSession.create(settings, profile_scope=settings.resolve_profile_scope()),
    )
    dispatch = asyncio.create_task(guarded.run(EffectParams(occurrence="performed"), ctx))
    await store.started.wait()

    dispatch.cancel()
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await dispatch

    assert tool.calls == 1
    actions = await inner.list_actions(scope=SCOPE, job_name="delegate")
    assert len(actions) == 1 and actions[0].status == "performed"
    assert tool.bound == (actions[0].id, actions[0].action_key)
    assert tool.settled == [actions[0].id]


@pytest.mark.asyncio
async def test_runner_atomic_guard_calls_duplicate_external_effect_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = FakeEffectTool()
    monkeypatch.setattr("ricky.runtime.composition.builtin_tools", lambda: [tool])
    _bundle(
        tmp_path,
        _base_bundle(
            """[permissions]
allow_mutating = ["fake_effect"]
""",
            tools=["fake_effect"],
            effects=2,
        ),
    )

    def responder(_request: CompletionRequest, stage: int) -> MessageDone:
        if stage == 0:
            return _calls(
                [
                    ("fake_effect", {"occurrence": "source:1"}),
                    ("fake_effect", {"occurrence": "source:1"}),
                ],
                stage,
            )
        return _done()

    settings = _settings(tmp_path)
    run = await JobRunner(settings, project_root=tmp_path).run(
        "delegate", profile_scope=SCOPE, provider=DynamicProvider(responder)
    )
    assert run.outcome == "succeeded" and run.effect_calls == 1
    assert tool.calls == 1
    actions = await JobRunStore(settings).list_actions(scope=SCOPE, job_name="delegate")
    assert len(actions) == 1 and actions[0].status == "performed"


@pytest.mark.asyncio
async def test_ambiguous_effect_is_in_doubt_never_replayed_and_cli_reconciles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = FakeEffectTool(fail=True)
    monkeypatch.setattr("ricky.runtime.composition.builtin_tools", lambda: [tool])
    _bundle(
        tmp_path,
        _base_bundle(
            """[permissions]
allow_mutating = ["fake_effect"]
""",
            tools=["fake_effect"],
            effects=2,
        ),
    )

    def responder(_request: CompletionRequest, stage: int) -> MessageDone:
        return (
            _call("fake_effect", {"occurrence": "source:ambiguous"}, stage)
            if stage == 0
            else _done()
        )

    settings = _settings(tmp_path)
    first = await JobRunner(settings, project_root=tmp_path).run(
        "delegate", profile_scope=SCOPE, provider=DynamicProvider(responder)
    )
    assert first.outcome == "uncertain" and tool.calls == 1
    assert first.error == (
        "external effect lacks a confirmed receipt; operator reconciliation required"
    )
    action = (await JobRunStore(settings).list_actions(scope=SCOPE, job_name="delegate"))[0]
    assert action.status == "in_doubt"
    second = await JobRunner(settings, project_root=tmp_path).run(
        "delegate", profile_scope=SCOPE, provider=DynamicProvider(responder)
    )
    assert second.outcome == "failed" and tool.calls == 1
    assert second.error == "external effect call did not produce a performed receipt"

    _project(tmp_path)
    runner = CliRunner()
    result = await asyncio.to_thread(
        runner.invoke,
        app,
        ["job", "action", "resolve", action.id, "--not-performed"],
        env={"RICKY_USER_DATA_DIR": str(tmp_path / "user")},
    )
    assert result.exit_code == 0
    assert (
        await JobRunStore(settings).get_action(action.id, scope=SCOPE)
    ).status == "not_performed"


@pytest.mark.asyncio
async def test_dry_run_denies_external_effect_without_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = FakeEffectTool()
    monkeypatch.setattr("ricky.runtime.composition.builtin_tools", lambda: [tool])
    _bundle(
        tmp_path,
        _base_bundle(
            """[permissions]
allow_mutating = ["fake_effect"]
""",
            tools=["fake_effect"],
            effects=2,
        ),
    )

    def responder(_request: CompletionRequest, stage: int) -> MessageDone:
        return _call("fake_effect", {"occurrence": "dry:1"}, stage) if stage == 0 else _done()

    settings = _settings(tmp_path)
    run = await JobRunner(settings, project_root=tmp_path).run(
        "delegate",
        profile_scope=SCOPE,
        provider=DynamicProvider(responder),
        dry_run=True,
    )
    assert run.outcome == "succeeded" and run.effect_calls == 0
    assert tool.calls == 0
    assert await JobRunStore(settings).list_actions(scope=SCOPE, job_name="delegate") == []


@pytest.mark.asyncio
async def test_user_owned_task_is_read_only_even_with_standing_claim_permission(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    tasks = await DurableTaskStore.create(settings, profile="personal")
    task = await tasks.create_task(
        title="User task",
        objective="User decides",
        closure_criteria="User completes",
        execution_mode="user",
        authority="deterministic_user_command",
        executor_id="test",
        tags=["meeting-scheduling"],
    )
    _bundle(
        tmp_path,
        _base_bundle(
            """[permissions]
allow_mutating = ["claim_durable_task"]
[[task_sources]]
name = "users"
tags_all = ["meeting-scheduling"]
execution_modes = ["user"]
limit = 10
""",
            tools=["claim_durable_task"],
        ),
    )
    run = await JobRunner(settings, project_root=tmp_path).run(
        "delegate",
        profile_scope=SCOPE,
        provider=DynamicProvider(_dry_task_responder),
        dry_run=False,
    )
    assert run.outcome == "succeeded"
    assert await tasks.get_task(task.id) == task


class FakeSlackClient:
    def __init__(self, pages: list[dict[str, object]]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.calls.append((method, params))
        return self.pages.pop(0)


@pytest.mark.asyncio
async def test_slack_adapter_owns_exclusive_cursor_pagination_and_stable_ids() -> None:
    client = FakeSlackClient(
        [
            {
                "messages": [{"ts": "11.0", "text": "new", "user": "U1"}],
                "has_more": True,
                "response_metadata": {"next_cursor": "page-2"},
            },
            {
                "messages": [{"ts": "10.5", "text": "older", "user": "U2"}],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            },
        ]
    )
    adapter = SlackChannelJobSource(client)  # type: ignore[arg-type]
    upper = datetime.fromtimestamp(12, UTC)
    batch = await adapter.collect(
        SlackChannelSourceConfig(channel_id="C123", initial_lookback_hours=24, item_limit=10),
        cursor={"ts": "10.0"},
        upper_bound=upper,
        limit=10,
    )
    assert batch.complete
    assert [item.id for item in batch.items] == ["C123:10.5:10.5", "C123:11.0:11.0"]
    assert batch.next_cursor == {"ts": "12.000000"}
    assert client.calls[0][1]["oldest"] == "10.0"
    assert client.calls[0][1]["inclusive"] == "false"
    assert client.calls[1][1]["cursor"] == "page-2"


def test_cli_task_tag_and_job_dry_run_flags_are_available(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RICKY_USER_DATA_DIR", str(tmp_path / "user"))
    created = CliRunner().invoke(
        app,
        [
            "task",
            "create",
            "--title",
            "Tagged",
            "--objective",
            "Classify",
            "--closure-criteria",
            "Done",
            "--mode",
            "agent",
            "--tag",
            "Customer:Acme",
        ],
    )
    assert created.exit_code == 0
    task_id = re.search(r"task_[0-9a-f]{32}", created.stdout)
    assert task_id is not None
    tagged = CliRunner().invoke(
        app,
        ["task", "tag", task_id.group(), "--add", "project:ricky", "--remove", "customer:acme"],
    )
    assert tagged.exit_code == 0
    assert "project:ricky" in tagged.stdout and "customer:acme" not in tagged.stdout
    assert "--dry-run" in CliRunner().invoke(app, ["job", "run", "--help"]).stdout


def test_schema_versions_are_current_and_no_job_domain_or_assignment_field(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    asyncio.run(JobRunStore(settings).initialize())
    asyncio.run(DurableTaskStore.create(settings, profile="personal"))
    with sqlite3.connect(Path(settings.user_data_dir) / "agent-runs" / "runs.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == JOB_SCHEMA_VERSION
    with sqlite3.connect(
        Path(settings.user_data_dir) / "profiles" / "personal" / "tasks" / "tasks.sqlite3"
    ) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='task_tags'"
            ).fetchone()
            is not None
        )
    assert "profile_scope" in JobRun.model_fields
    assert "assigned_job" not in DurableTask.model_fields


def test_preflight_allows_explicit_safe_ricky_state_mutation(
    tmp_path: Path,
) -> None:
    _bundle(
        tmp_path,
        _base_bundle(
            """[permissions]
allow_mutating = ["create_durable_task"]
""",
            tools=["create_durable_task"],
        ),
    )
    provider = DynamicProvider(lambda _request, _stage: _done())
    run = asyncio.run(
        JobRunner(_settings(tmp_path), project_root=tmp_path).run(
            "delegate", profile_scope=SCOPE, provider=provider
        )
    )
    assert run.outcome == "succeeded"
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_durable_task_v1_adapter_is_idempotent_and_rolls_back_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    store = await DurableTaskStore.create(settings, profile="personal")
    task = await store.create_task(
        title="Existing untagged task",
        objective="Survive migration",
        closure_criteria="Still readable",
        execution_mode="agent",
        authority="agent_autonomy",
        executor_id="test",
    )
    path = Path(settings.user_data_dir) / "profiles" / "personal" / "tasks" / "tasks.sqlite3"
    with sqlite3.connect(path) as database:
        database.execute("DROP TABLE task_tags")
        database.execute("PRAGMA user_version = 1")
    with pytest.raises(TaskStoreError, match="requires migration"):
        await DurableTaskStore.create(settings, profile="personal")
    adapter = DurableTasksUpgradeAdapter((path.resolve(),))
    target = adapter.discover(user_data_dir=Path(settings.user_data_dir).resolve())[0]
    [step] = adapter.plan_steps(source_data_generation=1, target_data_generation=1)
    adapter.apply(step)
    adapter.apply(step)
    assert adapter.verify(target).state == "current"
    migrated = await DurableTaskStore.create(settings, profile="personal")
    assert (await migrated.get_task(task.id)).tags == []
    assert (await DurableTaskStore.create(settings, profile="personal")).db_path == path

    broken_settings = settings.model_copy(update={"user_data_dir": str(tmp_path / "broken-user")})
    broken = await DurableTaskStore.create(broken_settings, profile="personal")
    with sqlite3.connect(broken.db_path) as database:
        database.execute("DROP TABLE task_tags")
        database.execute("PRAGMA user_version = 1")
    with pytest.raises(TaskStoreError):
        await DurableTaskStore.create(broken_settings, profile="personal")
    broken_adapter = DurableTasksUpgradeAdapter((broken.db_path.resolve(),))
    [broken_step] = broken_adapter.plan_steps(
        source_data_generation=1,
        target_data_generation=1,
    )
    monkeypatch.setattr(
        durable_tasks_upgrade,
        "_V1_TO_V2_SQL",
        durable_tasks_upgrade._V1_TO_V2_SQL.replace(
            "COMMIT;",
            "SELECT deliberately_missing_column FROM tasks;\nCOMMIT;",
        ),
    )
    with pytest.raises(TaskStoreError):
        broken_adapter.apply(broken_step)
    with sqlite3.connect(broken.db_path) as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            database.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_tags'"
            ).fetchone()
            is None
        )


@pytest.mark.asyncio
async def test_batch_payload_retention_preserves_ledger_metadata(tmp_path: Path) -> None:
    store = JobRunStore(_settings(tmp_path))
    await store.initialize()
    batches = []
    for index in range(2):
        run = _run_record(f"jobrun_retention_{index}")
        await store.insert(run, scope=SCOPE)
        batch = await persist_batch(
            store,
            run_id=run.id,
            job_name="delegate",
            source_name=f"tasks-{index}",
            kind="task_pool",
            payload=CandidateBatch(candidates=[]),
            item_ids=[],
            complete=True,
            dry_run=False,
            profile_scope=SCOPE,
        )
        await store.finish(
            run.model_copy(update={"outcome": "succeeded", "finished_at": datetime.now(UTC)}),
            scope=SCOPE,
        )
        batches.append(batch)
    await prune_batch_payloads(store, scope=SCOPE, keep=1)
    assert not Path(batches[0].payload_path).exists()
    assert Path(batches[1].payload_path).exists()
    retained_metadata = await store.batches_for_run("jobrun_retention_0", scope=SCOPE)
    assert retained_metadata[0].payload_path == ""


@pytest.mark.asyncio
async def test_overlapping_eligible_jobs_are_fenced_by_one_task_lease(tmp_path: Path) -> None:
    tasks = await DurableTaskStore.create(_settings(tmp_path), profile="personal")
    task = await tasks.create_task(
        title="Shared eligible work",
        objective="Only one job progresses it",
        closure_criteria="One fenced mutation",
        execution_mode="agent",
        authority="agent_autonomy",
        executor_id="test",
        tags=["meeting-scheduling"],
    )

    async def claim(holder: str) -> DurableTask | BaseException:
        try:
            return await tasks.claim(
                task.id,
                holder_session_id=holder,
                authority="agent_autonomy",
                executor_id=f"job:{holder}",
                expected_revision=task.revision,
            )
        except BaseException as exc:
            return exc

    results = await asyncio.gather(claim("one"), claim("two"))
    assert sum(isinstance(result, DurableTask) for result in results) == 1
    assert sum(isinstance(result, (TaskConflictError, TaskLeaseError)) for result in results) == 1


@pytest.mark.asyncio
async def test_repeated_block_escalation_updates_one_correlated_task(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    jobs = JobRunStore(settings)
    await jobs.initialize()
    for run_id in ("jobrun_one", "jobrun_two"):
        await jobs.insert(_run_record(run_id), scope=SCOPE)
    tasks = await DurableTaskStore.create(settings, profile="personal")
    first = await escalate_blocked(
        job_store=jobs,
        task_store=tasks,
        job_name="delegate",
        run_id="jobrun_one",
        source_identity="stream:item-1",
        summary="Need approval",
        profile_scope=SCOPE,
    )
    await tasks.release_session_leases("jobrun_one")
    second = await escalate_blocked(
        job_store=jobs,
        task_store=tasks,
        job_name="delegate",
        run_id="jobrun_two",
        source_identity="stream:item-1",
        summary="Still need approval with new context",
        profile_scope=SCOPE,
    )
    assert first == second
    updated = await tasks.get_task(first)
    assert updated.current_summary == "Still need approval with new context"
    assert updated.waiting_on == "user" and updated.lease is None


@pytest.mark.asyncio
async def test_model_change_preserves_prior_summary_context(
    tmp_path: Path,
) -> None:
    _bundle(tmp_path, _base_bundle())
    settings = _settings(tmp_path)
    first_provider = DynamicProvider(lambda _request, _stage: _done("Prior private summary"))
    first = await JobRunner(settings, project_root=tmp_path).run(
        "delegate", profile_scope=SCOPE, provider=first_provider
    )
    assert first.outcome == "succeeded"
    path = tmp_path / "user" / "profiles" / "personal" / "jobs" / "delegate" / "job.toml"
    path.write_text(
        _base_bundle().replace('model = "test-model"', 'model = "new-model"'),
        encoding="utf-8",
    )
    next_provider = DynamicProvider(lambda _request, _stage: _done())
    second = await JobRunner(settings, project_root=tmp_path).run(
        "delegate", profile_scope=SCOPE, provider=next_provider
    )
    assert second.outcome == "succeeded"
    request_text = "\n".join(
        part.text
        for message in next_provider.requests[0].messages
        for part in message.content
        if isinstance(part, TextPart)
    )
    assert "Prior private summary" in request_text


@pytest.mark.asyncio
async def test_goal_change_requires_explicit_context_revision_and_can_preserve_history(
    tmp_path: Path,
) -> None:
    _bundle(tmp_path, _base_bundle())
    settings = _settings(tmp_path)
    first = await JobRunner(settings, project_root=tmp_path).run(
        "delegate",
        profile_scope=SCOPE,
        provider=DynamicProvider(lambda _request, _stage: _done("Prior conclusion")),
    )
    assert first.outcome == "succeeded"
    path = tmp_path / "user" / "profiles" / "personal" / "jobs" / "delegate" / "job.toml"
    changed = _base_bundle().replace(
        "Account for every candidate and progress safe work.",
        "Account for every candidate and report changed work.",
    )
    path.write_text(changed, encoding="utf-8")

    rejected_provider = DynamicProvider(lambda _request, _stage: _done())
    with pytest.raises(ValueError, match="without a context decision"):
        await JobRunner(settings, project_root=tmp_path).run(
            "delegate", profile_scope=SCOPE, provider=rejected_provider
        )
    assert rejected_provider.requests == []

    repeated_provider = DynamicProvider(lambda _request, _stage: _done())
    with pytest.raises(ValueError, match="without a context decision"):
        await JobRunner(settings, project_root=tmp_path).run(
            "delegate", profile_scope=SCOPE, provider=repeated_provider
        )
    assert repeated_provider.requests == []
    rejected_runs = await JobRunStore(settings).list(
        scope=SCOPE, job_name="personal/delegate", limit=3
    )
    assert rejected_runs[0].outcome == rejected_runs[1].outcome == "failed"
    assert all(
        item.error is not None and "without a context decision" in item.error
        for item in rejected_runs[:2]
    )

    path.write_text(changed + "[context]\nlineage = 1\nrevision = 2\n", encoding="utf-8")
    preserving_provider = DynamicProvider(lambda _request, _stage: _done())
    preserved = await JobRunner(settings, project_root=tmp_path).run(
        "delegate", profile_scope=SCOPE, provider=preserving_provider
    )
    assert preserved.outcome == "succeeded"
    assert "Prior conclusion" in _visible_text(preserving_provider.requests[0])


@pytest.mark.asyncio
async def test_new_context_lineage_starts_fresh_without_erasing_job_history(
    tmp_path: Path,
) -> None:
    _bundle(tmp_path, _base_bundle())
    settings = _settings(tmp_path)
    first = await JobRunner(settings, project_root=tmp_path).run(
        "delegate",
        profile_scope=SCOPE,
        provider=DynamicProvider(lambda _request, _stage: _done("Old lineage conclusion")),
    )
    path = tmp_path / "user" / "profiles" / "personal" / "jobs" / "delegate" / "job.toml"
    changed = _base_bundle().replace(
        "Account for every candidate and progress safe work.",
        "Perform the revised responsibility.",
    )
    path.write_text(changed + "[context]\nlineage = 2\nrevision = 1\n", encoding="utf-8")
    provider = DynamicProvider(lambda _request, _stage: _done("Fresh conclusion"))

    fresh = await JobRunner(settings, project_root=tmp_path).run(
        "delegate", profile_scope=SCOPE, provider=provider
    )

    assert first.outcome == fresh.outcome == "succeeded"
    assert "Old lineage conclusion" not in _visible_text(provider.requests[0])
    history = await JobRunStore(settings).list(scope=SCOPE, job_name="personal/delegate", limit=10)
    assert {item.id for item in history} >= {first.id, fresh.id}


@pytest.mark.asyncio
async def test_live_and_dry_run_context_lanes_are_isolated(tmp_path: Path) -> None:
    _bundle(tmp_path, _base_bundle())
    settings = _settings(tmp_path)
    live_first = await JobRunner(settings, project_root=tmp_path).run(
        "delegate",
        profile_scope=SCOPE,
        provider=DynamicProvider(lambda _request, _stage: _done("Live-only conclusion")),
    )
    dry_provider = DynamicProvider(lambda _request, _stage: _done("Dry-only conclusion"))
    dry = await JobRunner(settings, project_root=tmp_path).run(
        "delegate", profile_scope=SCOPE, provider=dry_provider, dry_run=True
    )
    live_provider = DynamicProvider(lambda _request, _stage: _done())
    live_second = await JobRunner(settings, project_root=tmp_path).run(
        "delegate", profile_scope=SCOPE, provider=live_provider
    )

    assert live_first.outcome == dry.outcome == live_second.outcome == "succeeded"
    assert "Live-only conclusion" not in _visible_text(dry_provider.requests[0])
    live_context = _visible_text(live_provider.requests[0])
    assert "Live-only conclusion" in live_context
    assert "Dry-only conclusion" not in live_context
