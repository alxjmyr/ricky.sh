"""Transactional, fencing, retry, and ambiguity tests for the outbox."""

from __future__ import annotations

import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from ricky.attachments import StoredAttachment
from ricky.config import MessagingSettings, RickySettings
from ricky.notifications import (
    NotificationLeaseError,
    NotificationNotFoundError,
    NotificationStateError,
    NotificationStore,
)
from ricky.notifications.types import CorrelationRef, NotificationRequest
from ricky.profiles import ProfileLabel, ProfileScope

_SCOPE = ProfileScope.create("personal")


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 11, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _settings(tmp_path: Path, *, attempt_limit: int = 3) -> RickySettings:
    return RickySettings(
        user_data_dir=str(tmp_path / "user"),
        messaging=MessagingSettings(delivery_attempt_limit=attempt_limit),
    )


def _request(clock: MutableClock, *, occurrence: str = "one") -> NotificationRequest:
    return NotificationRequest(
        id=f"notification_{uuid4().hex}",
        route="owner",
        title="Update",
        body="Finished.",
        urgency="normal",
        source_kind="job",
        profile_label=ProfileLabel(required_profiles=("shared", "personal")),
        source_id="hourly",
        dedupe_key=occurrence,
        correlations=[],
        created_at=clock.now,
    )


async def test_enqueue_is_atomic_deduplicated_and_cross_process(tmp_path: Path) -> None:
    clock = MutableClock()
    settings = _settings(tmp_path)
    first_store = NotificationStore(settings, clock=clock)
    await first_store.initialize()
    first = await first_store.enqueue(_request(clock), scope=_SCOPE)
    duplicate = await first_store.enqueue(_request(clock), scope=_SCOPE)
    different = await first_store.enqueue(_request(clock, occurrence="two"), scope=_SCOPE)

    assert duplicate == first
    assert different.request.id != first.request.id
    assert first_store.db_path == tmp_path / "user" / "notifications" / "notifications.sqlite3"

    later_process = NotificationStore(settings, clock=clock)
    await later_process.initialize()
    assert await later_process.get(first.request.id, scope=_SCOPE) == first
    assert (await later_process.list(scope=_SCOPE, status="pending"))[0].outbox.status == "pending"


async def test_store_persists_every_labeled_correlation_kind(tmp_path: Path) -> None:
    clock = MutableClock()
    settings = _settings(tmp_path)
    store = NotificationStore(settings, clock=clock)
    await store.initialize()
    correlations = [
        CorrelationRef.model_validate(
            {
                "kind": kind,
                "id": f"record-{index}",
                "profile_label": _SCOPE.label(),
            }
        )
        for index, kind in enumerate(
            ("task", "job_run", "execution_request", "workflow_run", "conversation")
        )
    ]
    request = _request(clock).model_copy(update={"correlations": correlations})

    await store.enqueue(request, scope=_SCOPE)
    reopened = NotificationStore(settings, clock=clock)
    await reopened.initialize()

    assert (await reopened.get(request.id, scope=_SCOPE)).request.correlations == correlations


async def test_reads_and_mutations_reject_notifications_outside_scope(tmp_path: Path) -> None:
    clock = MutableClock()
    store = NotificationStore(_settings(tmp_path), clock=clock)
    await store.initialize()
    work_scope = ProfileScope.create("work")
    work_request = _request(clock).model_copy(update={"profile_label": work_scope.label()})
    work_record = await store.enqueue(work_request, scope=work_scope)

    assert await store.list(scope=_SCOPE) == []
    with pytest.raises(NotificationNotFoundError, match="active profile scope"):
        await store.get(work_request.id, scope=_SCOPE)
    with pytest.raises(NotificationNotFoundError, match="active profile scope"):
        await store.cancel(work_record.outbox.id, scope=_SCOPE)

    assert await store.get(work_request.id, scope=work_scope) == work_record


async def test_confirmed_delivery_records_one_receipt_and_rejects_stale_fence(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = NotificationStore(_settings(tmp_path), clock=clock)
    await store.initialize()
    record = await store.enqueue(_request(clock), scope=_SCOPE)
    first = await store.claim(
        record.outbox.id,
        scope=_SCOPE,
        worker="worker-a",
        transport="telegram",
        destination_ref="chat-1",
        lease_seconds=10,
    )
    clock.now += timedelta(seconds=1)
    renewed = await store.renew(first, scope=_SCOPE)
    assert renewed.lease_expires_at is not None
    assert first.lease_expires_at is not None
    assert renewed.lease_expires_at > first.lease_expires_at
    released = await store.release(renewed, scope=_SCOPE)
    assert released.status == "pending"
    second = await store.claim(
        record.outbox.id,
        scope=_SCOPE,
        worker="worker-b",
        transport="telegram",
        destination_ref="chat-1",
    )
    assert second.fence == first.fence + 1
    with pytest.raises(NotificationLeaseError, match="stale|active"):
        await store.mark_delivered(first, scope=_SCOPE, platform_message_id="old")

    delivered = await store.mark_delivered(second, scope=_SCOPE, platform_message_id="message-42")
    assert delivered.status == "delivered"
    assert delivered.platform_message_id == "message-42"
    assert [
        attempt.outcome for attempt in await store.attempts(record.outbox.id, scope=_SCOPE)
    ] == [
        "released",
        "delivered",
    ]


async def test_ambiguous_delivery_is_not_retried_until_operator_resolution(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = NotificationStore(_settings(tmp_path), clock=clock)
    await store.initialize()
    record = await store.enqueue(_request(clock), scope=_SCOPE)
    claimed = await store.claim(
        record.outbox.id,
        scope=_SCOPE,
        worker="worker",
        transport="telegram",
        destination_ref="chat-1",
    )
    ambiguous = await store.mark_in_doubt(
        claimed,
        scope=_SCOPE,
        error="connection closed after upload",
    )
    assert ambiguous.status == "in_doubt"
    with pytest.raises(NotificationStateError, match="resolved"):
        await store.retry(ambiguous.id, scope=_SCOPE)
    resolved = await store.resolve(
        ambiguous.id,
        scope=_SCOPE,
        disposition="not_delivered",
        actor="owner",
        note="Checked the conversation.",
    )
    assert resolved.status == "failed"
    assert (await store.retry(resolved.id, scope=_SCOPE)).status == "pending"
    assert (await store.resolutions(resolved.id, scope=_SCOPE))[0].disposition == ("not_delivered")


async def test_expired_claim_becomes_in_doubt_and_stale_receipt_is_rejected(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = NotificationStore(_settings(tmp_path), clock=clock)
    await store.initialize()
    record = await store.enqueue(_request(clock), scope=_SCOPE)
    claimed = await store.claim(
        record.outbox.id,
        scope=_SCOPE,
        worker="worker",
        transport="telegram",
        destination_ref="chat-1",
        lease_seconds=5,
    )
    clock.now += timedelta(seconds=6)
    with pytest.raises(NotificationLeaseError, match="expired"):
        await store.mark_delivered(claimed, scope=_SCOPE, platform_message_id="late")
    assert (await store.get_outbox(record.outbox.id, scope=_SCOPE)).status == "in_doubt"
    with pytest.raises(NotificationStateError, match="resolved"):
        await store.retry(record.outbox.id, scope=_SCOPE)


async def test_confirmed_pre_send_expiry_does_not_exhaust_delivery_budget(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = NotificationStore(_settings(tmp_path, attempt_limit=1), clock=clock)
    await store.initialize()
    record = await store.enqueue(_request(clock), scope=_SCOPE)

    for index in range(3):
        claimed = await store.claim(
            record.outbox.id,
            scope=_SCOPE,
            worker=f"worker-{index}",
            transport="telegram",
            destination_ref="chat-1",
            lease_seconds=1,
        )
        clock.now += timedelta(seconds=2)
        recovered = await store.recover_claim(
            claimed.id,
            scope=_SCOPE,
            disposition="pending",
            error="startup proof confirms send never began",
            now=clock.now,
        )
        assert recovered.status == "pending"
        assert recovered.attempt_count == 0

    final = await store.claim(
        record.outbox.id,
        scope=_SCOPE,
        worker="worker-final",
        transport="telegram",
        destination_ref="chat-1",
    )
    delivered = await store.mark_delivered(final, scope=_SCOPE, platform_message_id="message-42")

    assert delivered.status == "delivered"
    assert [
        attempt.attempt_number for attempt in await store.attempts(record.outbox.id, scope=_SCOPE)
    ] == [
        1,
        2,
        3,
        4,
    ]


async def test_known_pre_send_failures_retry_only_within_attempt_limit(tmp_path: Path) -> None:
    clock = MutableClock()
    store = NotificationStore(_settings(tmp_path, attempt_limit=2), clock=clock)
    await store.initialize()
    record = await store.enqueue(_request(clock), scope=_SCOPE)
    first = await store.claim(
        record.outbox.id,
        scope=_SCOPE,
        worker="worker",
        transport="telegram",
        destination_ref="chat-1",
    )
    failed = await store.mark_failed(first, scope=_SCOPE, error="DNS failed before request")
    assert failed.status == "failed"
    await store.retry(failed.id, scope=_SCOPE)
    second = await store.claim(
        failed.id,
        scope=_SCOPE,
        worker="worker",
        transport="telegram",
        destination_ref="chat-1",
    )
    failed_again = await store.mark_failed(second, scope=_SCOPE, error="DNS failed before request")
    with pytest.raises(NotificationStateError, match="exhausted"):
        await store.retry(failed_again.id, scope=_SCOPE)


async def test_pruning_notification_removes_its_attachment_snapshot(tmp_path: Path) -> None:
    clock = MutableClock()
    settings = _settings(tmp_path)
    store = NotificationStore(settings, clock=clock)
    await store.initialize()
    request = _request(clock)
    attachment = StoredAttachment(
        storage_path=f"notifications/attachments/{request.id}/report.txt",
        filename="report.txt",
        media_type="text/plain",
        size_bytes=6,
        sha256="a" * 64,
    )
    path = Path(settings.user_data_dir) / attachment.storage_path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"report")
    record = await store.enqueue(
        request.model_copy(update={"attachments": [attachment]}),
        scope=_SCOPE,
    )
    await store.cancel(record.outbox.id, scope=_SCOPE)

    assert await store.prune_notifications([record.outbox.id], scope=_SCOPE) == 1
    assert not path.parent.exists()


@pytest.mark.skipif(os.name != "posix", reason="private store modes are POSIX file modes")
async def test_reopening_a_loosened_store_restores_private_modes(tmp_path: Path) -> None:
    clock = MutableClock()
    settings = _settings(tmp_path)
    store = NotificationStore(settings, clock=clock)
    await store.initialize()
    await store.enqueue(_request(clock), scope=_SCOPE)
    # A live WAL connection materializes the sidecars, which inherit whatever
    # mode the database file carried when SQLite created them.
    holder = sqlite3.connect(store.db_path)
    try:
        holder.execute("SELECT COUNT(*) FROM outbox").fetchone()
        sidecars = [Path(f"{store.db_path}-wal"), Path(f"{store.db_path}-shm")]
        assert [path for path in sidecars if path.is_file()] == sidecars
        store.root.chmod(0o755)
        for path in (store.db_path, *sidecars):
            path.chmod(0o644)

        await NotificationStore(settings, clock=clock).initialize()

        assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
        for path in (store.db_path, *sidecars):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    finally:
        holder.close()


@pytest.mark.skipif(os.name != "posix", reason="private store modes are POSIX file modes")
async def test_reopening_tolerates_a_disappearing_sqlite_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock()
    settings = _settings(tmp_path)
    store = NotificationStore(settings, clock=clock)
    await store.initialize()
    store.root.chmod(0o755)
    store.db_path.chmod(0o644)
    real_chmod = os.chmod
    real_is_file = Path.is_file

    def sidecar_was_present(path: Path) -> bool:
        return str(path).endswith("-wal") or real_is_file(path)

    def disappearing_sidecar(path: os.PathLike[str] | str, mode: int) -> None:
        if str(path).endswith("-wal"):
            raise FileNotFoundError(path)
        real_chmod(path, mode)

    monkeypatch.setattr(Path, "is_file", sidecar_was_present)
    monkeypatch.setattr(os, "chmod", disappearing_sidecar)

    await NotificationStore(settings, clock=clock).initialize()

    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.db_path.stat().st_mode) == 0o600
