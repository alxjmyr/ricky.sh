"""End-to-end execution dispatcher tests with offline providers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

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
