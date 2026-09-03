"""Route policy, helper, job projection, and CLI tests for notifications."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError
from typer.testing import CliRunner

from ricky.attachments import AttachmentInput, AttachmentSnapshotBatch, LoadedAttachment
from ricky.config import MessagingSettings, RickySettings
from ricky.interfaces.cli.app import app
from ricky.jobs.notifications import enqueue_job_notification, project_job_notifications
from ricky.jobs.store import JobRunStore
from ricky.jobs.types import JobRun
from ricky.notifications import (
    CorrelationRef,
    NotificationService,
    NotificationStore,
    ResolvedRoute,
    RouteError,
    RoutePolicy,
    job_completed,
    task_blocked,
)
from ricky.notifications.types import NotificationRequest
from ricky.profiles import ProfileLabel, ProfileScope

_PERSONAL_SCOPE = ProfileScope.create("personal")
_ALL_SCOPE = ProfileScope.create("personal", access_profiles=("work",))


def _settings(tmp_path: Path, *, job_route: str | None = None) -> RickySettings:
    return RickySettings(
        user_data_dir=str(tmp_path / "user"),
        messaging=MessagingSettings.model_validate(
            {
                "transports": {
                    "main": {"type": "telegram", "account": "personal/owner-bot"},
                    "work": {"type": "discord", "account": "work/work-bot"},
                },
                "telegram_accounts": {
                    "personal/owner-bot": {"bot_token": "test-token"},
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
                        "destination": "channel-work",
                        "owner_profile": "work",
                        "accepted_profiles": ["shared", "work"],
                    },
                },
                "agent_routes": ["owner"],
                "job_route": job_route,
            }
        ),
    )


def _request(
    *,
    route: str = "owner",
    profile_label: ProfileLabel | None = None,
) -> NotificationRequest:
    return NotificationRequest(
        id="notification_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        route=route,
        body="Update",
        urgency="normal",
        source_kind="test",
        profile_label=profile_label or ProfileLabel(required_profiles=("shared", "personal")),
        source_id="source",
        dedupe_key="one",
        correlations=[],
        created_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
    )


def test_messaging_config_rejects_unregistered_transports_and_escaping_paths() -> None:
    with pytest.raises(ValidationError, match="unknown transport"):
        MessagingSettings.model_validate(
            {
                "routes": {
                    "owner": {
                        "transport": "missing",
                        "destination": "chat",
                        "owner_profile": "personal",
                        "accepted_profiles": ["shared", "personal"],
                    }
                }
            }
        )
    with pytest.raises(ValidationError, match="confined relative path"):
        MessagingSettings(store_path="../notifications.sqlite3")
    with pytest.raises(ValidationError, match="cannot be empty"):
        MessagingSettings.model_validate(
            {
                "transports": {
                    "main": {"type": "telegram", "account": "personal/owner"},
                },
                "routes": {
                    "owner": {
                        "transport": "main",
                        "destination": " ",
                        "owner_profile": "personal",
                        "accepted_profiles": ["shared", "personal"],
                    }
                },
            }
        )
    with pytest.raises(ValidationError, match="owner_profile must be accepted"):
        MessagingSettings.model_validate(
            {
                "transports": {
                    "main": {"type": "telegram", "account": "personal/owner"},
                },
                "routes": {
                    "owner": {
                        "transport": "main",
                        "destination": "chat",
                        "owner_profile": "personal",
                        "accepted_profiles": ["shared"],
                    }
                },
            }
        )
    with pytest.raises(ValidationError, match="accepted_profiles must be unique"):
        MessagingSettings.model_validate(
            {
                "transports": {
                    "main": {"type": "telegram", "account": "personal/owner"},
                },
                "routes": {
                    "owner": {
                        "transport": "main",
                        "destination": "chat",
                        "owner_profile": "personal",
                        "accepted_profiles": ["personal", "personal"],
                    }
                },
            }
        )


async def test_route_policy_blocks_profile_crossover_and_hides_raw_destination(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    service = NotificationService(settings)
    with pytest.raises(RouteError, match=r"profile\(s\) work"):
        await service.enqueue(
            _request(profile_label=ProfileLabel(required_profiles=("shared", "work"))),
            scope=_ALL_SCOPE,
        )
    with pytest.raises(RouteError, match=r"profile\(s\) personal"):
        await service.enqueue(_request(route="work-alerts"), scope=_PERSONAL_SCOPE)

    resolved = await RoutePolicy(settings).resolve(
        "owner", ProfileLabel(required_profiles=("shared", "personal"))
    )
    assert resolved.transport == "telegram"
    assert resolved.destination_ref == "chat-owner"
    assert resolved.owner_profile == "personal"
    assert resolved.accepted_profiles == ["shared", "personal"]
    assert "destination" not in NotificationRequest.model_fields

    with pytest.raises(ValidationError, match="Extra inputs"):
        NotificationRequest.model_validate(_request().model_dump() | {"destination": "raw-chat"})


async def test_conversation_route_uses_trusted_record_and_enforces_clearance(
    tmp_path: Path,
) -> None:
    class Resolver:
        async def resolve_conversation_route(
            self,
            conversation_id: str,
            profile_label: ProfileLabel,
        ) -> ResolvedRoute:
            del profile_label
            return ResolvedRoute(
                route=f"conversation:{conversation_id}",
                transport="telegram",
                account="work/owner-bot",
                destination_ref="trusted-chat",
                owner_profile="work",
                accepted_profiles=["shared", "work"],
            )

    policy = RoutePolicy(_settings(tmp_path), conversation_resolver=Resolver())
    assert (
        await policy.resolve(
            "conversation:abc",
            ProfileLabel(required_profiles=("shared", "work")),
        )
    ).destination_ref == "trusted-chat"
    with pytest.raises(RouteError, match="rejects required.*personal"):
        await policy.resolve(
            "conversation:abc",
            ProfileLabel(required_profiles=("shared", "personal")),
        )


def test_helpers_make_stable_job_and_task_correlations() -> None:
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    first = job_completed(
        route="owner",
        job_name="hourly",
        run_id="jobrun_1",
        summary="done",
        profile_label=ProfileLabel(required_profiles=("shared", "personal")),
        created_at=now,
    )
    second = job_completed(
        route="owner",
        job_name="hourly",
        run_id="jobrun_1",
        summary="done",
        profile_label=ProfileLabel(required_profiles=("shared", "personal")),
        created_at=now,
    )
    task = task_blocked(
        route="work-alerts",
        task_id="task_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        revision=7,
        summary="needs input",
        profile_label=ProfileLabel(required_profiles=("shared", "work")),
        created_at=now,
    )
    assert first.dedupe_key == second.dedupe_key == "completed:jobrun_1"
    assert first.body_format == second.body_format == "portable_markdown_v1"
    assert first.correlations == [
        CorrelationRef(
            kind="job_run",
            id="jobrun_1",
            revision=None,
            profile_label=ProfileLabel(required_profiles=("shared", "personal")),
        )
    ]
    assert task.correlations[0].profile_label == ProfileLabel(required_profiles=("shared", "work"))
    assert task.correlations[0].revision == 7
    assert task.body_format == "portable_markdown_v1"


async def test_notification_label_must_cover_correlated_profiles(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    request = _request()
    crossed = request.model_copy(
        update={
            "correlations": [
                CorrelationRef(
                    kind="task",
                    id="task_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    revision=2,
                    profile_label=ProfileLabel(required_profiles=("shared", "work")),
                )
            ]
        }
    )
    with pytest.raises(ValueError, match="omits a correlated profile"):
        await NotificationService(settings).enqueue(crossed, scope=_PERSONAL_SCOPE)


async def test_attachment_dedupe_cleans_new_snapshots_and_rejects_changed_content(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    source = project / "report.bin"
    source.write_bytes(b"first-version")
    service = NotificationService(settings)
    request = _request()

    first = await service.enqueue_with_attachments(
        request,
        attachments=[AttachmentInput(path="report.bin")],
        cwd=project,
        scope=_PERSONAL_SCOPE,
    )
    [first_attachment] = first.request.attachments
    first_path = Path(settings.user_data_dir) / first_attachment.storage_path
    duplicate = request.model_copy(update={"id": "notification_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"})
    deduped = await service.enqueue_with_attachments(
        duplicate,
        attachments=[AttachmentInput(path="report.bin")],
        cwd=project,
        scope=_PERSONAL_SCOPE,
    )

    duplicate_dir = Path(settings.user_data_dir) / settings.messaging.attachment_dir / duplicate.id
    assert deduped.request.id == request.id
    assert not duplicate_dir.exists()
    assert first_path.read_bytes() == b"first-version"

    source.write_bytes(b"second-version")
    with pytest.raises(ValueError, match="different attachment set"):
        await service.enqueue_with_attachments(
            request,
            attachments=[AttachmentInput(path="report.bin")],
            cwd=project,
            scope=_PERSONAL_SCOPE,
        )

    attachment_root = Path(settings.user_data_dir) / settings.messaging.attachment_dir
    files = sorted(path for path in attachment_root.rglob("*") if path.is_file())
    assert files == [first_path]
    assert first_path.read_bytes() == b"first-version"


async def test_attachment_snapshot_is_removed_when_enqueue_fails(tmp_path: Path) -> None:
    class FailingStore(NotificationStore):
        async def enqueue(
            self,
            request: NotificationRequest,
            *,
            scope: ProfileScope,
        ):  # type: ignore[no-untyped-def]
            del request, scope
            raise RuntimeError("injected enqueue failure")

    settings = _settings(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    source = project / "report.bin"
    source.write_bytes(b"orphan-me-not")
    service = NotificationService(settings, store=FailingStore(settings))

    with pytest.raises(RuntimeError, match="injected enqueue failure"):
        await service.enqueue_with_attachments(
            _request(),
            attachments=[AttachmentInput(path="report.bin")],
            cwd=project,
            scope=_PERSONAL_SCOPE,
        )

    attachment_root = Path(settings.user_data_dir) / settings.messaging.attachment_dir
    assert not attachment_root.exists() or not any(attachment_root.rglob("*"))


async def test_cancellation_during_snapshot_joins_and_cleans_worker_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    (project / "report.bin").write_bytes(b"snapshot-before-cancel")
    service = NotificationService(settings)
    started = threading.Event()
    release = threading.Event()
    from ricky.notifications import service as service_module

    original = service_module.snapshot_attachment_batch

    def blocking_snapshot(
        attachments: list[LoadedAttachment] | tuple[LoadedAttachment, ...],
        *,
        settings: RickySettings,
        notification_id: str,
    ) -> AttachmentSnapshotBatch:
        started.set()
        if not release.wait(timeout=5):
            raise TimeoutError("snapshot test release timed out")
        return original(
            attachments,
            settings=settings,
            notification_id=notification_id,
        )

    monkeypatch.setattr(service_module, "snapshot_attachment_batch", blocking_snapshot)
    enqueue = asyncio.create_task(
        service.enqueue_with_attachments(
            _request(),
            attachments=[AttachmentInput(path="report.bin")],
            cwd=project,
            scope=_PERSONAL_SCOPE,
        )
    )
    assert await asyncio.to_thread(started.wait, 5)

    enqueue.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await enqueue

    attachment_root = Path(settings.user_data_dir) / settings.messaging.attachment_dir
    assert not attachment_root.exists() or not any(attachment_root.rglob("*"))
    assert await service.store.list(scope=_PERSONAL_SCOPE, limit=10) == []


async def test_scheduled_job_can_enqueue_offline_and_projector_fills_gap(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, job_route="owner")
    job_store = JobRunStore(settings)
    await job_store.initialize()
    run = JobRun(
        id="jobrun_offline",
        job_name="hourly",
        spec_digest="a" * 64,
        provider="openrouter",
        model="model",
        profile_scope=_PERSONAL_SCOPE,
        session_id="session",
        outcome="succeeded",
        started_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
        finished_at=datetime(2026, 8, 11, 12, 1, tzinfo=UTC),
        final_message="Everything is current.",
        trigger="schedule",
        trigger_id="schedule_1",
    )
    await job_store.insert(run, scope=_PERSONAL_SCOPE)
    service = NotificationService(settings)
    await enqueue_job_notification(
        run,
        route="owner",
        profile_label=_PERSONAL_SCOPE.label(),
        profile_scope=_PERSONAL_SCOPE,
        service=service,
    )

    later = NotificationStore(settings)
    await later.initialize()
    records = await later.list(scope=_PERSONAL_SCOPE)
    assert len(records) == 1
    assert records[0].request.correlations[0].id == run.id
    assert records[0].request.body_format == "portable_markdown_v1"

    assert (
        await project_job_notifications(
            settings,
            store=job_store,
            service=service,
            route="owner",
            profile_scope=_PERSONAL_SCOPE,
        )
        == 1
    )
    assert len(await later.list(scope=_PERSONAL_SCOPE)) == 1


async def test_silent_job_result_is_not_enqueued_or_projected(tmp_path: Path) -> None:
    settings = _settings(tmp_path, job_route="owner")
    job_store = JobRunStore(settings)
    await job_store.initialize()
    run = JobRun(
        id="jobrun_silent",
        job_name="hourly",
        spec_digest="a" * 64,
        provider="openrouter",
        model="model",
        profile_scope=_PERSONAL_SCOPE,
        session_id="session",
        outcome="succeeded",
        started_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
        finished_at=datetime(2026, 8, 11, 12, 1, tzinfo=UTC),
        final_message="Everything is current.",
        result_notification="never",
        trigger="schedule",
        trigger_id="schedule_1",
    )
    await job_store.insert(run, scope=_PERSONAL_SCOPE)
    service = NotificationService(settings)

    assert (
        await enqueue_job_notification(
            run,
            route="owner",
            profile_label=_PERSONAL_SCOPE.label(),
            profile_scope=_PERSONAL_SCOPE,
            service=service,
        )
        is None
    )
    assert (
        await project_job_notifications(
            settings,
            store=job_store,
            service=service,
            route="owner",
            profile_scope=_PERSONAL_SCOPE,
        )
        == 0
    )
    notification_store = NotificationStore(settings)
    await notification_store.initialize()
    assert await notification_store.list(scope=_PERSONAL_SCOPE) == []


async def test_failed_job_notification_uses_deterministic_error_over_model_success_claim(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, job_route="owner")
    service = NotificationService(settings)
    run = JobRun(
        id="jobrun_receipt_failure",
        job_name="hourly",
        spec_digest="a" * 64,
        provider="openrouter",
        model="model",
        profile_scope=_PERSONAL_SCOPE,
        session_id="session",
        outcome="uncertain",
        started_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
        finished_at=datetime(2026, 8, 11, 12, 1, tzinfo=UTC),
        final_message="The email was sent successfully.",
        error="external effect lacks a confirmed receipt",
        trigger="schedule",
        trigger_id="schedule_1",
    )

    await enqueue_job_notification(
        run,
        route="owner",
        profile_label=_PERSONAL_SCOPE.label(),
        profile_scope=_PERSONAL_SCOPE,
        service=service,
    )

    store = NotificationStore(settings)
    await store.initialize()
    [record] = await store.list(scope=_PERSONAL_SCOPE)
    assert record.request.body == "external effect lacks a confirmed receipt"
    assert "email was sent" not in record.request.body


def test_provider_free_cli_inspection_never_prints_configured_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "super-secret-token"
    settings = _settings(tmp_path).model_copy(update={"slack_user_token": SecretStr(sentinel)})
    store = NotificationStore(settings)
    request = _request()

    import asyncio

    asyncio.run(store.initialize())
    asyncio.run(store.enqueue(request, scope=_PERSONAL_SCOPE))
    monkeypatch.setattr("ricky.interfaces.cli.app.load_settings", lambda: settings)
    result = CliRunner().invoke(app, ["notification", "show", request.id])
    assert result.exit_code == 0
    assert request.id in result.stdout
    assert "profiles: shared, personal" in result.stdout
    assert sentinel not in result.stdout
