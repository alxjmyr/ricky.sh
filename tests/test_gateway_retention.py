"""Protective retention: pruning can never remove unresolved responsibility."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gateway_ops_support import (
    PROFILE_SCOPE,
    enqueue_notification,
    execution_request,
    make_conversation,
    settings,
    store_inbound,
    transport_message,
)
from gateway_ops_support import inbound as build_inbound
from ricky.config import GatewayRetentionSettings
from ricky.executions.store import ExecutionStore
from ricky.gateway.retention import GatewayRetention
from ricky.gateway.store import GatewayStore
from ricky.messaging.store import MessagingStore
from ricky.notifications.store import NotificationStore
from ricky.sessions.store import SessionStore

pytestmark = pytest.mark.asyncio

OLD = datetime(2020, 1, 1, tzinfo=UTC)


def _config(tmp_path: Path, **overrides):  # type: ignore[no-untyped-def]
    base = {"enabled": True, "min_age_seconds": 0.0}
    base.update(overrides)
    return settings(tmp_path, retention=GatewayRetentionSettings(**base))


async def _processed_message(messaging: MessagingStore, update_id: str) -> str:
    message = await store_inbound(messaging, build_inbound(update_id=update_id, received_at=OLD))
    claim = await messaging.claim_inbox(message.id, owner="worker", lease_seconds=600)
    await messaging.finish_inbox(claim, status="processed")
    return message.id


async def test_a_dry_run_plan_deletes_nothing(tmp_path: Path) -> None:
    config = _config(tmp_path, inbound_messages=0)
    messaging = MessagingStore(config)
    await messaging.initialize()
    message_id = await _processed_message(messaging, "1")

    plan = await GatewayRetention(config, scope=PROFILE_SCOPE).plan()

    assert plan.applied is False
    group = plan.group("inbound_messages")
    assert group is not None and group.removable_ids == (message_id,)
    assert group.removed == 0
    assert (await messaging.get_inbox(message_id)).id == message_id


async def test_apply_removes_only_the_planned_records(tmp_path: Path) -> None:
    config = _config(tmp_path, inbound_messages=1)
    messaging = MessagingStore(config)
    await messaging.initialize()
    older = await _processed_message(messaging, "1")
    newer = await _processed_message(messaging, "2")

    plan = await GatewayRetention(config, scope=PROFILE_SCOPE).apply()

    group = plan.group("inbound_messages")
    assert group is not None and group.removed == 1
    remaining = {item.id for item in await messaging.list_inbox(limit=100)}
    assert len(remaining) == 1
    assert remaining <= {older, newer}


async def test_apply_requires_retention_to_be_enabled(tmp_path: Path) -> None:
    config = settings(tmp_path, retention=GatewayRetentionSettings(enabled=False))

    with pytest.raises(ValueError, match="retention.enabled must be true"):
        await GatewayRetention(config, scope=PROFILE_SCOPE).apply()


async def test_a_pending_message_is_never_prunable(tmp_path: Path) -> None:
    config = _config(tmp_path, inbound_messages=0)
    messaging = MessagingStore(config)
    await messaging.initialize()
    message = await store_inbound(messaging, build_inbound(received_at=OLD))

    plan = await GatewayRetention(config, scope=PROFILE_SCOPE).apply()

    group = plan.group("inbound_messages")
    assert group is not None and group.removable_ids == ()
    assert (await messaging.get_inbox(message.id)).status == "pending"


async def test_an_uncertain_message_is_never_prunable(tmp_path: Path) -> None:
    config = _config(tmp_path, inbound_messages=0)
    messaging = MessagingStore(config)
    await messaging.initialize()
    message = await store_inbound(messaging, build_inbound(received_at=OLD))
    await messaging.claim_inbox(message.id, owner="dead", lease_seconds=1)
    later = datetime.now(UTC) + timedelta(seconds=10)
    await messaging.recover_inbox_claim(message.id, status="uncertain", now=later)

    plan = await GatewayRetention(config, scope=PROFILE_SCOPE).apply()

    group = plan.group("inbound_messages")
    assert group is not None and group.removable_ids == ()
    assert (await messaging.get_inbox(message.id)).status == "uncertain"


async def test_a_message_referenced_by_a_queued_execution_is_protected(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, inbound_messages=0)
    messaging = MessagingStore(config)
    executions = ExecutionStore(config)
    await messaging.initialize()
    await executions.initialize()
    message_id = await _processed_message(messaging, "1")
    await executions.submit(execution_request(source_message_id=message_id), scope=PROFILE_SCOPE)

    plan = await GatewayRetention(config, scope=PROFILE_SCOPE).apply()

    group = plan.group("inbound_messages")
    assert group is not None
    assert message_id in group.protected_ids
    assert message_id not in group.removable_ids
    assert (await messaging.get_inbox(message_id)).id == message_id


async def test_an_in_doubt_delivery_is_never_prunable(tmp_path: Path) -> None:
    config = _config(tmp_path, notifications=0)
    notifications = NotificationStore(config)
    messaging = MessagingStore(config)
    await notifications.initialize()
    await messaging.initialize()
    entry = await enqueue_notification(notifications)
    claimed = await notifications.claim(
        entry.id,
        scope=PROFILE_SCOPE,
        worker="w",
        transport="telegram",
        destination_ref="200",
    )
    await messaging.prepare_parts(claimed, [transport_message(claimed)])
    await notifications.mark_in_doubt(claimed, scope=PROFILE_SCOPE, error="ambiguous send")

    plan = await GatewayRetention(config, scope=PROFILE_SCOPE).apply()

    group = plan.group("notifications")
    assert group is not None
    assert entry.id in group.protected_ids
    assert group.removable_ids == ()
    assert (await notifications.get_outbox(entry.id, scope=PROFILE_SCOPE)).status == "in_doubt"


async def test_a_pending_notification_is_never_prunable(tmp_path: Path) -> None:
    config = _config(tmp_path, notifications=0)
    notifications = NotificationStore(config)
    await notifications.initialize()
    entry = await enqueue_notification(notifications)

    plan = await GatewayRetention(config, scope=PROFILE_SCOPE).apply()

    group = plan.group("notifications")
    assert group is not None and group.removable_ids == ()
    assert (await notifications.get_outbox(entry.id, scope=PROFILE_SCOPE)).status == "pending"


async def test_a_queued_or_uncertain_execution_is_never_prunable(tmp_path: Path) -> None:
    config = _config(tmp_path, execution_requests=0)
    executions = ExecutionStore(config)
    await executions.initialize()
    queued = execution_request(created_at=OLD)
    await executions.submit(queued, scope=PROFILE_SCOPE)

    plan = await GatewayRetention(config, scope=PROFILE_SCOPE).apply()

    group = plan.group("execution_requests")
    assert group is not None and group.removable_ids == ()
    assert (await executions.get(queued.id, scope=PROFILE_SCOPE)).status == "queued"


async def test_a_succeeded_execution_beyond_the_ceiling_is_prunable(tmp_path: Path) -> None:
    config = _config(tmp_path, execution_requests=0)
    executions = ExecutionStore(config)
    await executions.initialize()
    request = execution_request(created_at=OLD)
    await executions.submit(request, scope=PROFILE_SCOPE)
    claimed = await executions.claim(scope=PROFILE_SCOPE, worker_id="w", limit=1)
    assert claimed[0].claim_token is not None
    running = await executions.start(
        claimed[0].id,
        scope=PROFILE_SCOPE,
        token=claimed[0].claim_token,
        fence=claimed[0].claim_fence,
        run_id="jobrun_finished",
    )
    assert running.claim_token is not None
    await executions.finish(
        running.id,
        scope=PROFILE_SCOPE,
        token=running.claim_token,
        fence=running.claim_fence,
        status="succeeded",
    )

    plan = await GatewayRetention(config, scope=PROFILE_SCOPE).apply()

    group = plan.group("execution_requests")
    assert group is not None and group.removed == 1
    assert await executions.list(scope=PROFILE_SCOPE, limit=100) == []


async def test_min_age_protects_a_recently_finished_record(tmp_path: Path) -> None:
    config = _config(tmp_path, inbound_messages=0, min_age_seconds=3_600.0)
    messaging = MessagingStore(config)
    await messaging.initialize()
    message = await store_inbound(messaging, build_inbound())
    claim = await messaging.claim_inbox(message.id, owner="worker", lease_seconds=600)
    await messaging.finish_inbox(claim, status="processed")

    plan = await GatewayRetention(config, scope=PROFILE_SCOPE).apply()

    group = plan.group("inbound_messages")
    assert group is not None and group.removable_ids == ()
    assert (await messaging.get_inbox(message.id)).id == message.id


async def test_an_archived_conversation_with_a_retained_turn_is_protected(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, archived_conversations=0)
    gateway = GatewayStore(config)
    sessions = SessionStore(config)
    messaging = MessagingStore(config)
    await gateway.initialize()
    await sessions.initialize()
    await messaging.initialize()
    conversation_id, session_id = await make_conversation(gateway, sessions)
    message = await store_inbound(messaging, build_inbound(received_at=OLD))
    await gateway.begin_result(
        message_id=message.id,
        conversation_id=conversation_id,
        session_id=session_id,
        scope=PROFILE_SCOPE,
    )
    conversation = await gateway.get(conversation_id, scope=PROFILE_SCOPE)
    await gateway.finish_result(
        message_id=message.id,
        conversation_id=conversation_id,
        expected_conversation_revision=conversation.revision,
        status="committed",
        session_revision=1,
        response_outbox_id=None,
        error=None,
        scope=PROFILE_SCOPE,
    )
    current = await gateway.get(conversation_id, scope=PROFILE_SCOPE)
    await gateway.archive(conversation_id, scope=PROFILE_SCOPE, expected_revision=current.revision)

    plan = await GatewayRetention(config, scope=PROFILE_SCOPE).apply()

    group = plan.group("archived_conversations")
    assert group is not None and group.removable_ids == ()
    assert (await gateway.get(conversation_id, scope=PROFILE_SCOPE)).status == "archived"


async def test_service_logs_are_bounded_by_file_count(tmp_path: Path) -> None:
    config = _config(tmp_path, log_file_limit=2)
    directory = tmp_path / "user" / "logs"
    directory.mkdir(parents=True)
    for index in range(4):
        (directory / f"gateway-{index}.log").write_text("x" * 10, encoding="utf-8")

    plan = await GatewayRetention(config, scope=PROFILE_SCOPE).apply()

    group = plan.group("service_logs")
    assert group is not None and group.removed == 2
    assert len(list(directory.iterdir())) == 2


async def test_service_logs_are_bounded_by_total_bytes(tmp_path: Path) -> None:
    config = _config(tmp_path, log_file_limit=10, log_byte_limit=10_000)
    directory = tmp_path / "user" / "logs"
    directory.mkdir(parents=True)
    for index in range(3):
        (directory / f"gateway-{index}.log").write_text("x" * 8_000, encoding="utf-8")

    plan = await GatewayRetention(config, scope=PROFILE_SCOPE).apply()

    group = plan.group("service_logs")
    assert group is not None and group.removed >= 1
    total = sum(item.stat().st_size for item in directory.iterdir())
    assert total <= 16_000


async def test_pruning_is_idempotent(tmp_path: Path) -> None:
    config = _config(tmp_path, inbound_messages=0)
    messaging = MessagingStore(config)
    await messaging.initialize()
    await _processed_message(messaging, "1")

    first = await GatewayRetention(config, scope=PROFILE_SCOPE).apply()
    second = await GatewayRetention(config, scope=PROFILE_SCOPE).apply()

    assert first.group("inbound_messages").removed == 1  # type: ignore[union-attr]
    assert second.group("inbound_messages").removed == 0  # type: ignore[union-attr]
    assert await messaging.list_inbox(limit=100) == []
