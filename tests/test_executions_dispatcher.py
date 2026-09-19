"""End-to-end execution dispatcher tests with offline providers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ricky.config import ExecutionSettings, MessagingSettings, RickySettings
from ricky.durable_tasks.scoped import ScopedDurableTaskStore
from ricky.executions.dispatcher import ExecutionDispatcher, _bounded_result, _execution_outcome
from ricky.executions.types import ExecutionRequest
from ricky.jobs.store import JobRunStore
from ricky.jobs.types import JobRun
from ricky.llm import CompletionRequest, Message, MessageDone, StreamEvent
from ricky.notifications import NotificationService, NotificationStore
from ricky.notifications.types import NotificationRecord, NotificationRequest
from ricky.profiles import ProfileName, ProfileScope

SCOPE = ProfileScope.create("personal")


class ScriptedProvider:
    name = "scripted"

    def __init__(self, text: str = "Research complete.") -> None:
        self.text = text
        self.requests: list[CompletionRequest] = []
        self.closed = False

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        yield MessageDone(message=Message.text("assistant", self.text))

    async def aclose(self) -> None:
        self.closed = True


class SlowProvider:
    name = "slow"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        del request
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if False:
            yield MessageDone(message=Message.text("assistant", "never"))

    async def aclose(self) -> None:
        return None


class SelectivelyFailingNotifications(NotificationService):
    def __init__(self, settings: RickySettings, *, rejected_source_id: str) -> None:
        super().__init__(settings)
        self.rejected_source_id = rejected_source_id
        self.attempted_source_ids: list[str] = []

    async def enqueue(
        self, request: NotificationRequest, *, scope: ProfileScope
    ) -> NotificationRecord:
        self.attempted_source_ids.append(request.source_id)
        if request.source_id == self.rejected_source_id:
            raise RuntimeError("route is no longer resolvable")
        return await super().enqueue(request, scope=scope)


def test_uncertain_effect_receipt_overrides_model_success_claim() -> None:
    run = JobRun(
        id="jobrun_uncertain",
        provider="openrouter",
        model="model",
        profile_scope=SCOPE,
        session_id="session",
        outcome="uncertain",
        started_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
        finished_at=datetime(2026, 8, 20, 12, 1, tzinfo=UTC),
        final_message="The email was sent successfully.",
        error="external effect lacks a confirmed receipt",
        trigger="execution",
        trigger_id="execution_" + "a" * 32,
    )

    status, error = _execution_outcome(run)

    assert status == "uncertain"
    assert error == "external effect lacks a confirmed receipt"
    assert _bounded_result(run, 1_000) == "external effect lacks a confirmed receipt"


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": ".ricky",
            "providers": {"openrouter": {"default_model": "test-model"}},
            "google": {"accounts": {}},
            "memory": {"enabled": False},
            "workflow": {"enabled": False},
            "executions": ExecutionSettings(
                claim_seconds=10,
                heartbeat_seconds=0.05,
                concurrency=1,
                poll_seconds=0.01,
            ),
            "messaging": MessagingSettings.model_validate(
                {
                    "telegram_accounts": {
                        "personal/owner-bot": {"bot_token": "personal-token"},
                        "work/work-bot": {"bot_token": "work-token"},
                    },
                    "transports": {
                        "main": {"type": "telegram", "account": "personal/owner-bot"},
                        "work": {"type": "telegram", "account": "work/work-bot"},
                    },
                    "routes": {
                        "owner": {
                            "transport": "main",
                            "destination": "chat-owner",
                            "owner_profile": "personal",
                            "accepted_profiles": ["shared", "personal"],
                        },
                        "work-alerts": {
                            "transport": "work",
                            "destination": "chat-work",
                            "owner_profile": "work",
                            "accepted_profiles": ["shared", "work"],
                        },
                    },
                    "agent_routes": ["owner"],
                }
            ),
        }
    )


def _job(tmp_path: Path) -> None:
    bundle = tmp_path / "user" / "profiles" / "personal" / "jobs" / "brief"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "job.toml").write_text(
        """version = 3
name = "brief"
description = "Prepare a brief."
provider = "openrouter"
model = "test-model"
goal = "Prepare the brief."
[budget]
wall_clock_seconds = 10
iterations = 2
max_completion_tokens_per_request = 100
effect_calls = 0
[tools]
allow = []
""",
        encoding="utf-8",
    )


async def _task(settings: RickySettings, *, profile: ProfileName = "personal"):
    store = await ScopedDurableTaskStore.create(settings, scope=ProfileScope.create(profile))
    return await store.create_task(
        title="Investigate",
        objective="Find the answer",
        closure_criteria="A concise report exists",
        execution_mode="agent",
        authority="deterministic_user_command",
        executor_id="test",
    )


async def test_named_request_uses_named_job_path_and_ledger(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _job(tmp_path)
    provider = ScriptedProvider("Brief complete.")
    dispatcher = ExecutionDispatcher(
        settings, project_root=tmp_path, provider_factory=lambda _: provider
    )
    request = await dispatcher.start_named_job(
        "brief",
        notification_route="owner",
        request_key="named:1",
        profile_scope=SCOPE,
    )
    [terminal] = await dispatcher.worker_once(scope=SCOPE)
    assert terminal.status == "succeeded"
    assert terminal.run_id is not None
    run = await JobRunStore(settings).get(terminal.run_id, scope=SCOPE)
    assert run.job_name == "personal/brief"
    assert run.spec_digest == request.job_digest


async def test_running_cancellation_cancels_and_awaits_owned_run(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _job(tmp_path)
    provider = SlowProvider()
    dispatcher = ExecutionDispatcher(
        settings, project_root=tmp_path, provider_factory=lambda _: provider
    )
    request = await dispatcher.start_named_job(
        "brief",
        notification_route="owner",
        request_key="cancel",
        profile_scope=SCOPE,
    )
    worker = asyncio.create_task(dispatcher.worker_once(scope=SCOPE))
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    cancelled = await dispatcher.cancel_execution_request(request.id, scope=SCOPE)
    completed = await asyncio.wait_for(worker, timeout=2)
    assert cancelled.status == "cancelled"
    assert completed[0].status == "cancelled"
    assert provider.cancelled


async def test_failed_terminal_projection_creates_one_attention_notification(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    dispatcher = ExecutionDispatcher(settings, project_root=tmp_path)
    await dispatcher.store.initialize()
    request = await dispatcher.store.submit(
        ExecutionRequest(
            id="execution_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            kind="named_job",
            status="queued",
            named_job="personal/brief",
            job_digest="a" * 64,
            profile_scope=SCOPE,
            notification_route="owner",
            request_key="failed-projection",
            created_at=datetime.now(UTC),
        ),
        scope=SCOPE,
    )
    claimed = (await dispatcher.store.claim(worker_id="worker", scope=SCOPE, limit=1))[0]
    assert claimed.claim_token is not None
    await dispatcher.store.start(
        request.id,
        scope=SCOPE,
        token=claimed.claim_token,
        fence=claimed.claim_fence,
        run_id="jobrun_missing_after_crash",
    )
    await dispatcher.store.finish(
        request.id,
        scope=SCOPE,
        token=claimed.claim_token,
        fence=claimed.claim_fence,
        status="failed",
        error="provider failed",
    )
    await dispatcher.project_notifications(scope=SCOPE)
    await dispatcher.project_notifications(scope=SCOPE)
    records = await NotificationStore(settings).list(scope=SCOPE, status="pending")
    assert len(records) == 1
    assert records[0].request.urgency == "attention"
    assert records[0].request.body_format == "portable_markdown_v1"
    assert records[0].request.correlations[0].kind == "execution_request"


async def test_terminal_projection_failure_does_not_starve_other_requests(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    rejected_id = "execution_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    notifications = SelectivelyFailingNotifications(settings, rejected_source_id=rejected_id)
    dispatcher = ExecutionDispatcher(settings, project_root=tmp_path, notifications=notifications)
    await dispatcher.store.initialize()
    request_ids = [rejected_id, "execution_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"]
    for index, request_id in enumerate(request_ids):
        request = await dispatcher.store.submit(
            ExecutionRequest(
                id=request_id,
                kind="named_job",
                status="queued",
                named_job="personal/brief",
                job_digest="a" * 64,
                profile_scope=SCOPE,
                notification_route="owner",
                request_key=f"projection-isolation:{index}",
                created_at=datetime.now(UTC),
            ),
            scope=SCOPE,
        )
        claimed = (await dispatcher.store.claim(worker_id="worker", scope=SCOPE, limit=1))[0]
        assert claimed.claim_token is not None
        await dispatcher.store.start(
            request.id,
            scope=SCOPE,
            token=claimed.claim_token,
            fence=claimed.claim_fence,
            run_id=f"jobrun_projection_{index}",
        )
        await dispatcher.store.finish(
            request.id,
            scope=SCOPE,
            token=claimed.claim_token,
            fence=claimed.claim_fence,
            status="failed",
            error="provider failed",
        )

    assert await dispatcher.project_notifications(scope=SCOPE) == 1
    assert set(notifications.attempted_source_ids) == set(request_ids)
    records = await NotificationStore(settings).list(scope=SCOPE, status="pending")
    assert len(records) == 1
    assert records[0].request.source_id == request_ids[1]


async def test_failed_execution_reports_effect_but_keeps_task_blocked(tmp_path: Path) -> None:
    from ricky.tools.base import EffectIdentity

    settings = _settings(tmp_path)
    task = await _task(settings)
    jobs = JobRunStore(settings)
    await jobs.initialize()
    run = JobRun(
        id="jobrun_bookkeeping_failure",
        job_name="personal/brief",
        provider="openrouter",
        model="test",
        profile_scope=SCOPE,
        session_id="session",
        started_at=datetime.now(UTC),
        outcome="failed",
        error="Task revision conflict",
        final_message="Everything succeeded.",
        trigger="execution",
        trigger_id="execution_" + "c" * 32,
    )
    await jobs.insert(run.model_copy(update={"outcome": None}), scope=SCOPE)
    action = await jobs.reserve_action(
        job_name="personal/brief",
        run_id=run.id,
        effect_budget=1,
        scope=SCOPE,
        identity=EffectIdentity(
            action_key="b" * 64,
            operation="gmail.trash",
            target="message",
            occurrence="once",
            summary="Move message to Trash",
        ),
    )
    await jobs.resolve_action(action.id, "performed", scope=SCOPE, provider_reference="message")
    await jobs.finish(run.model_copy(update={"finished_at": datetime.now(UTC)}), scope=SCOPE)
    request = ExecutionRequest(
        id="execution_" + "c" * 32,
        kind="named_job",
        status="failed",
        named_job="personal/brief",
        job_digest="a" * 64,
        profile_scope=SCOPE,
        notification_route="owner",
        request_key="bookkeeping-failure",
        created_at=datetime.now(UTC),
        task_id=task.id,
        task_revision=task.revision,
        run_id=run.id,
    )
    dispatcher = ExecutionDispatcher(settings, project_root=tmp_path)
    await dispatcher._update_task(request, task, run)
    await dispatcher._notify(request, run)
    tasks = await ScopedDurableTaskStore.create(settings, scope=SCOPE)
    updated = await tasks.get_task(task.id)
    assert updated.status == "blocked"
    assert updated.lease is None
    assert updated.current_summary is not None
    assert "Task revision conflict" in updated.current_summary
    assert action.id in updated.current_summary
    records = await NotificationStore(settings).list(scope=SCOPE)
    assert len(records) == 1
    assert records[0].request.title == "Execution failed"
    body = records[0].request.body
    assert body.startswith("Execution failed.\n\nConfirmed effects: 1 performed.")
    assert "Task revision conflict" in body
    assert action.id in body
    assert body.endswith("Agent report (unverified):\n> Everything succeeded.")
    assert "Everything succeeded" not in updated.current_summary
    assert len(await jobs.actions_for_run(run.id, scope=SCOPE)) == 1


async def test_receipt_read_failure_does_not_claim_task(tmp_path: Path) -> None:
    import pytest

    from ricky.jobs.store import JobStoreError

    settings = _settings(tmp_path)
    task = await _task(settings)
    run = JobRun(
        id="jobrun_missing_receipt_store",
        provider="openrouter",
        model="test",
        profile_scope=SCOPE,
        session_id="session",
        started_at=datetime.now(UTC),
        outcome="failed",
        error="Task revision conflict",
    )
    request = ExecutionRequest(
        id="execution_" + "d" * 32,
        kind="named_job",
        status="failed",
        named_job="personal/brief",
        job_digest="a" * 64,
        profile_scope=SCOPE,
        notification_route="owner",
        request_key="missing-receipt-store",
        created_at=datetime.now(UTC),
        task_id=task.id,
        task_revision=task.revision,
        run_id=run.id,
    )
    dispatcher = ExecutionDispatcher(settings, project_root=tmp_path)
    with pytest.raises(JobStoreError):
        await dispatcher._update_task(request, task, run)
    tasks = await ScopedDurableTaskStore.create(settings, scope=SCOPE)
    assert await tasks.get_task(task.id) == task


async def test_gateway_named_handoff_waits_for_delivery_and_projects_one_result(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _job(tmp_path)
    dispatcher = ExecutionDispatcher(
        settings,
        project_root=tmp_path,
        provider_factory=lambda _: ScriptedProvider("Brief complete."),
    )
    args: Any = dict(
        notification_route="owner",
        request_key="handoff:1",
        profile_scope=SCOPE,
        source_conversation_id="conversation-test",
        source_message_id="inbound-test",
        await_acknowledgement=True,
        handoff_title="Prepare a brief",
    )
    request = await dispatcher.start_named_job("brief", **args)
    duplicate = await dispatcher.start_named_job("brief", **args)
    assert duplicate.id == request.id
    assert request.status == "awaiting_acknowledgement"
    assert request.expires_at is None
    assert request.acknowledgement_expires_at is not None
    assert await dispatcher.worker_once(scope=SCOPE) == []
    await dispatcher.store.attach_acknowledgement(request.id, "outbox-test", scope=SCOPE)
    assert await dispatcher.worker_once(scope=SCOPE) == []
    await dispatcher.store.release_acknowledged(request.id, "outbox-test", scope=SCOPE)
    [terminal] = await dispatcher.worker_once(scope=SCOPE)
    assert terminal.status == "succeeded"
    assert await dispatcher.worker_once(scope=SCOPE) == []


async def test_cancelled_unacknowledged_handoff_does_not_notify_before_delivery(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _job(tmp_path)
    dispatcher = ExecutionDispatcher(settings, project_root=tmp_path)
    request = await dispatcher.start_named_job(
        "brief",
        notification_route="owner",
        request_key="handoff:cancel",
        profile_scope=SCOPE,
        source_conversation_id="conversation-test",
        source_message_id="inbound-test",
        await_acknowledgement=True,
        handoff_title="Prepare a brief",
    )
    await dispatcher.cancel_execution_request(request.id, scope=SCOPE)
    await dispatcher.notifications.store.initialize()
    assert (
        await dispatcher.notifications.store.source_ids(source_kind="execution", scope=SCOPE)
        == set()
    )
    assert await dispatcher.project_notifications(scope=SCOPE) == 0
    await dispatcher.store.attach_acknowledgement(request.id, "outbox-test", scope=SCOPE)
    await dispatcher.store.release_acknowledged(request.id, "outbox-test", scope=SCOPE)
    assert await dispatcher.project_notifications(scope=SCOPE) == 1
    assert await dispatcher.project_notifications(scope=SCOPE) == 0


@pytest.mark.parametrize("gateway_handoff", [False, True])
@pytest.mark.parametrize("final_message", ["", "$6.94"])
async def test_success_notification_uses_friendly_gateway_fallback_only(
    tmp_path: Path,
    gateway_handoff: bool,
    final_message: str,
) -> None:
    settings = _settings(tmp_path)
    _job(tmp_path)
    dispatcher = ExecutionDispatcher(settings, project_root=tmp_path)
    request = await dispatcher.start_named_job(
        "brief",
        notification_route="owner",
        request_key="fallback",
        profile_scope=SCOPE,
        await_acknowledgement=gateway_handoff,
        handoff_title="Check balance" if gateway_handoff else None,
        source_conversation_id="conversation-test" if gateway_handoff else None,
        source_message_id="inbound-test" if gateway_handoff else None,
    )
    if gateway_handoff:
        await dispatcher.store.attach_acknowledgement(request.id, "outbox-test", scope=SCOPE)
        request = await dispatcher.store.release_acknowledged(
            request.id, "outbox-test", scope=SCOPE
        )
    run = JobRun(
        id="jobrun_fallback",
        provider="openrouter",
        model="test",
        profile_scope=SCOPE,
        session_id="session",
        outcome="succeeded",
        started_at=datetime.now(UTC),
        final_message=final_message,
        trigger="execution",
        trigger_id=request.id,
    )
    request = request.model_copy(update={"status": "succeeded", "run_id": run.id})
    await dispatcher._notify(request, run)
    [record] = await dispatcher.notifications.store.list(scope=SCOPE)
    expected = final_message or ("Completed." if gateway_handoff else f"Job run {run.id} succeeded")
    assert record.request.body == expected
    assert any(ref.kind == "job_run" and ref.id == run.id for ref in record.request.correlations)


@pytest.mark.parametrize(
    ("status", "outcome", "error"),
    [
        ("failed", "failed", "external effect call did not produce a performed receipt"),
        ("uncertain", "uncertain", "external effect lacks a confirmed receipt"),
        ("failed", "budget_exceeded", "wall clock deadline exceeded"),
        ("cancelled", "succeeded", "execution cancelled after work completed"),
        ("blocked", "approval_required", "background execution requires attention"),
    ],
)
@pytest.mark.parametrize("report", [None, "", "  ", "Balance was $6.65; Add Credits was denied."])
async def test_failure_notification_preserves_status_cause_and_optional_report(
    tmp_path: Path, status: str, outcome: str, error: str, report: str | None
) -> None:
    settings = _settings(tmp_path)
    dispatcher = ExecutionDispatcher(settings, project_root=tmp_path)
    jobs = JobRunStore(settings)
    await jobs.initialize()
    request = ExecutionRequest.model_validate(
        {
            "id": "execution_" + "e" * 32,
            "kind": "named_job",
            "status": status,
            "named_job": "personal/brief",
            "job_digest": "a" * 64,
            "profile_scope": SCOPE,
            "notification_route": "owner",
            "request_key": "failure-report",
            "created_at": datetime.now(UTC),
            "handoff_title": "Check balance",
            "acknowledgement_outbox_id": "outbox_test",
            "acknowledgement_delivered_at": datetime.now(UTC),
            "error": error,
            "run_id": "jobrun_failure_report",
        }
    )
    run = JobRun.model_validate(
        {
            "id": request.run_id,
            "provider": "openrouter",
            "model": "test",
            "profile_scope": SCOPE,
            "session_id": "session",
            "outcome": outcome,
            "started_at": datetime.now(UTC),
            "error": None if outcome == "succeeded" else error,
            "final_message": report,
        }
    )
    await jobs.insert(run, scope=SCOPE)
    await dispatcher._notify(request, run)
    await dispatcher._notify(request, run)
    [record] = await dispatcher.notifications.store.list(scope=SCOPE)
    assert record.request.title == "Check balance"
    assert record.request.body.startswith(f"Execution {status}.\n\n{error}")
    assert record.request.urgency == "attention"
    assert record.request.route == "owner"
    assert record.request.dedupe_key == f"result:{status}"
    if report and report.strip():
        assert record.request.body.endswith(f"Agent report (unverified):\n> {report}")
    else:
        assert "Agent report" not in record.request.body


@pytest.mark.parametrize("limit", [100, 400, 4000])
async def test_failure_notification_bounds_long_cause_and_report(
    tmp_path: Path, limit: int
) -> None:
    settings = _settings(tmp_path)
    settings.executions.result_text_limit = limit
    dispatcher = ExecutionDispatcher(settings, project_root=tmp_path)
    await JobRunStore(settings).initialize()
    request = ExecutionRequest(
        id="execution_" + "f" * 32,
        kind="named_job",
        status="uncertain",
        named_job="personal/brief",
        job_digest="a" * 64,
        profile_scope=SCOPE,
        notification_route="owner",
        request_key="long-failure",
        created_at=datetime.now(UTC),
    )
    run = JobRun(
        id="jobrun_long_failure",
        provider="openrouter",
        model="test",
        profile_scope=SCOPE,
        session_id="session",
        outcome="uncertain",
        started_at=datetime.now(UTC),
        error="Receipt missing. " * 100,
        final_message="A useful detail.\n" * 1000,
    )
    await JobRunStore(settings).insert(run, scope=SCOPE)
    await dispatcher._notify(request, run)
    [record] = await dispatcher.notifications.store.list(scope=SCOPE)
    assert len(record.request.body) <= limit
    assert record.request.body.startswith("Execution uncertain.\n\nReceipt missing.")
    if limit == 4000:
        assert "Agent report (unverified):\n> A" in record.request.body
    else:
        assert "Agent report" not in record.request.body


@pytest.mark.parametrize("status", ["failed", "uncertain", "cancelled", "blocked"])
async def test_failure_notification_without_job_run(tmp_path: Path, status: str) -> None:
    dispatcher = ExecutionDispatcher(_settings(tmp_path), project_root=tmp_path)
    request = ExecutionRequest.model_validate(
        {
            "id": "execution_" + "f" * 32,
            "kind": "named_job",
            "status": status,
            "named_job": "personal/brief",
            "job_digest": "a" * 64,
            "profile_scope": SCOPE,
            "notification_route": "owner",
            "request_key": "no-run",
            "created_at": datetime.now(UTC),
            "error": "Worker unavailable",
        }
    )
    await dispatcher._notify(request, None)
    [record] = await dispatcher.notifications.store.list(scope=SCOPE)
    assert record.request.body == f"Execution {status}.\n\nWorker unavailable"


@pytest.mark.parametrize("limit", [100, 400])
async def test_failure_notification_prioritizes_receipts_over_long_report(
    tmp_path: Path, limit: int
) -> None:
    from ricky.tools.base import EffectIdentity

    settings = _settings(tmp_path)
    settings.executions.result_text_limit = limit
    dispatcher = ExecutionDispatcher(settings, project_root=tmp_path)
    jobs = JobRunStore(settings)
    await jobs.initialize()
    run = JobRun(
        id="jobrun_mixed_receipts",
        provider="openrouter",
        model="test",
        profile_scope=SCOPE,
        session_id="session",
        outcome="uncertain",
        started_at=datetime.now(UTC),
        error="Receipt missing. " * 100,
        final_message="Everything succeeded. " * 1000,
    )
    await jobs.insert(run.model_copy(update={"outcome": None}), scope=SCOPE)
    for index, status in enumerate(("performed", "in_doubt")):
        action = await jobs.reserve_action(
            job_name="personal/brief",
            run_id=run.id,
            effect_budget=2,
            scope=SCOPE,
            identity=EffectIdentity(
                action_key=str(index) * 64,
                operation="browser.commit",
                target="checkout",
                occurrence=str(index),
                summary="Commit checkout",
            ),
        )
        await jobs.resolve_action(action.id, status, scope=SCOPE, provider_reference=None)
    await jobs.finish(run.model_copy(update={"finished_at": datetime.now(UTC)}), scope=SCOPE)
    request = ExecutionRequest(
        id="execution_" + "f" * 32,
        kind="named_job",
        status="uncertain",
        named_job="personal/brief",
        job_digest="a" * 64,
        profile_scope=SCOPE,
        notification_route="owner",
        request_key="mixed-receipts",
        created_at=datetime.now(UTC),
        error="Recovery requires review",
    )
    await dispatcher._notify(request, run)
    [record] = await dispatcher.notifications.store.list(scope=SCOPE)
    assert len(record.request.body) <= limit
    assert record.request.body.startswith("Execution uncertain.")
    assert "Confirmed effects: 1 performed." in record.request.body
    assert "Unresolved: 1; review required." in record.request.body
    assert "Everything succeeded" not in record.request.body
