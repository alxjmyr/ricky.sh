"""Read-only audit projection across subsystem-owned canonical ids."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from gateway_ops_support import (
    PROFILE_SCOPE,
    enqueue_notification,
    execution_request,
    job_run,
    make_conversation,
    reserve_effect,
    settings,
    store_inbound,
    transport_message,
)
from gateway_ops_support import inbound as build_inbound
from ricky.executions.store import ExecutionStore
from ricky.gateway.audit import GatewayAudit
from ricky.gateway.store import GatewayStore
from ricky.jobs.store import JobRunStore
from ricky.messaging.store import MessagingStore
from ricky.messaging.types import DeliveryReceipt
from ricky.notifications.store import NotificationStore
from ricky.notifications.types import CorrelationRef
from ricky.sessions.store import SessionStore

pytestmark = pytest.mark.asyncio


def _kinds(chain) -> list[str]:  # type: ignore[no-untyped-def]
    return [link.kind for link in chain.links]


def _link(chain, kind: str):  # type: ignore[no-untyped-def]
    return next(link for link in chain.links if link.kind == kind)


async def test_an_unrecognised_id_is_reported_not_guessed(tmp_path: Path) -> None:
    chain = await GatewayAudit(settings(tmp_path), scope=PROFILE_SCOPE).trace("not-a-canonical-id")

    assert chain.resolved_kind is None
    assert chain.present == ()
    assert "not a recognised canonical id" in chain.links[0].detail


async def test_an_empty_id_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        await GatewayAudit(settings(tmp_path), scope=PROFILE_SCOPE).trace("   ")


async def test_a_missing_inbound_message_reports_missing(tmp_path: Path) -> None:
    chain = await GatewayAudit(settings(tmp_path), scope=PROFILE_SCOPE).trace(f"inbound_{'a' * 32}")

    assert chain.resolved_kind == "inbound_message"
    assert _link(chain, "inbound_message").state == "missing"
    assert chain.present == ()


async def test_an_unprocessed_inbound_message_reports_a_missing_turn(tmp_path: Path) -> None:
    config = settings(tmp_path)
    messaging = MessagingStore(config)
    await messaging.initialize()
    message = await store_inbound(messaging, build_inbound())

    chain = await GatewayAudit(config, scope=PROFILE_SCOPE).trace(message.id)

    assert _link(chain, "inbound_message").state == "present"
    assert _link(chain, "foreground_turn").state == "missing"
    assert "never claimed by a foreground turn" in _link(chain, "foreground_turn").detail


async def test_the_chain_follows_message_to_conversation_turn_and_delivery(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    messaging = MessagingStore(config)
    notifications = NotificationStore(config)
    gateway = GatewayStore(config)
    sessions = SessionStore(config)
    await messaging.initialize()
    await notifications.initialize()
    await gateway.initialize()
    await sessions.initialize()
    message = await store_inbound(messaging, build_inbound())
    conversation_id, session_id = await make_conversation(gateway, sessions)
    await gateway.begin_result(
        message_id=message.id,
        conversation_id=conversation_id,
        session_id=session_id,
        scope=PROFILE_SCOPE,
    )
    entry = await enqueue_notification(notifications)
    claimed = await notifications.claim(
        entry.id, worker="w", transport="telegram", destination_ref="200", scope=PROFILE_SCOPE
    )
    part = transport_message(claimed)
    await messaging.prepare_parts(claimed, [part])
    await messaging.record_receipt(
        claimed,
        DeliveryReceipt(
            transport="telegram",
            account="personal",
            transport_message_id=part.id,
            platform_message_id="platform-out-1",
            destination_id="200",
            delivered_at=datetime.now(UTC),
        ),
    )
    await notifications.mark_delivered(
        claimed, scope=PROFILE_SCOPE, platform_message_id="platform-out-1"
    )
    conversation = await gateway.get(conversation_id, scope=PROFILE_SCOPE)
    await gateway.finish_result(
        message_id=message.id,
        conversation_id=conversation_id,
        expected_conversation_revision=conversation.revision,
        status="committed",
        session_revision=1,
        response_outbox_id=entry.id,
        error=None,
        scope=PROFILE_SCOPE,
    )

    chain = await GatewayAudit(config, scope=PROFILE_SCOPE).trace(message.id)

    kinds = _kinds(chain)
    assert kinds[:5] == [
        "inbound_message",
        "conversation",
        "foreground_turn",
        "notification",
        "delivery_receipt",
    ]
    assert _link(chain, "notification").status == "delivered"
    assert _link(chain, "delivery_receipt").status == "delivered"
    assert _link(chain, "user_reply").state == "missing"


async def test_a_correlated_user_reply_is_resolved_only_through_a_stored_receipt(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    messaging = MessagingStore(config)
    notifications = NotificationStore(config)
    await messaging.initialize()
    await notifications.initialize()
    entry = await enqueue_notification(notifications)
    claimed = await notifications.claim(
        entry.id, worker="w", transport="telegram", destination_ref="200", scope=PROFILE_SCOPE
    )
    part = transport_message(claimed)
    await messaging.prepare_parts(claimed, [part])
    await messaging.record_receipt(
        claimed,
        DeliveryReceipt(
            transport="telegram",
            account="personal",
            transport_message_id=part.id,
            platform_message_id="platform-out-9",
            destination_id="200",
            delivered_at=datetime.now(UTC),
        ),
    )
    await notifications.mark_delivered(
        claimed, scope=PROFILE_SCOPE, platform_message_id="platform-out-9"
    )
    reply = await store_inbound(messaging, build_inbound(update_id="55", reply_to="platform-out-9"))

    chain = await GatewayAudit(config, scope=PROFILE_SCOPE).trace(entry.id)

    user_reply = _link(chain, "user_reply")
    assert user_reply.state == "present"
    assert user_reply.id == reply.id


async def test_an_execution_chain_reaches_the_run_and_its_effect_receipt(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    executions = ExecutionStore(config)
    jobs = JobRunStore(config)
    await executions.initialize()
    await jobs.initialize()
    run = job_run()
    await jobs.insert(run, scope=PROFILE_SCOPE)
    action_id = await reserve_effect(jobs, run)
    await jobs.resolve_action(
        action_id,
        "performed",
        scope=PROFILE_SCOPE,
        provider_reference="confirmation-123",
    )
    request = execution_request()
    await executions.submit(request, scope=PROFILE_SCOPE)
    claimed = await executions.claim(scope=PROFILE_SCOPE, worker_id="w", limit=1)
    assert claimed[0].claim_token is not None
    await executions.start(
        claimed[0].id,
        scope=PROFILE_SCOPE,
        token=claimed[0].claim_token,
        fence=claimed[0].claim_fence,
        run_id=run.id,
    )

    chain = await GatewayAudit(config, scope=PROFILE_SCOPE).trace(request.id)

    assert _link(chain, "execution_request").state == "present"
    assert _link(chain, "job_run").state == "present"
    external = _link(chain, "external_action")
    assert external.state == "present"
    assert external.status == "performed"
    assert "confirmation-123" in external.detail


async def test_the_audit_never_copies_a_transcript_body(tmp_path: Path) -> None:
    config = settings(tmp_path)
    executions = ExecutionStore(config)
    jobs = JobRunStore(config)
    await executions.initialize()
    await jobs.initialize()
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"secret":"do not copy me"}\n', encoding="utf-8")
    run = job_run().model_copy(update={"transcript_path": str(transcript)})
    await jobs.insert(run, scope=PROFILE_SCOPE)
    request = execution_request()
    await executions.submit(request, scope=PROFILE_SCOPE)
    claimed = await executions.claim(scope=PROFILE_SCOPE, worker_id="w", limit=1)
    assert claimed[0].claim_token is not None
    await executions.start(
        claimed[0].id,
        scope=PROFILE_SCOPE,
        token=claimed[0].claim_token,
        fence=claimed[0].claim_fence,
        run_id=run.id,
    )

    chain = await GatewayAudit(config, scope=PROFILE_SCOPE).trace(request.id)

    job_link = _link(chain, "job_run")
    assert str(transcript) in job_link.detail
    assert "do not copy me" not in job_link.detail


async def test_a_request_that_never_started_reports_a_missing_run(tmp_path: Path) -> None:
    config = settings(tmp_path)
    executions = ExecutionStore(config)
    await executions.initialize()
    request = execution_request()
    await executions.submit(request, scope=PROFILE_SCOPE)

    chain = await GatewayAudit(config, scope=PROFILE_SCOPE).trace(request.id)

    assert _link(chain, "execution_request").status == "queued"
    assert _link(chain, "job_run").state == "missing"


async def test_a_missing_durable_task_is_reported_not_invented(tmp_path: Path) -> None:
    config = settings(tmp_path)
    executions = ExecutionStore(config)
    await executions.initialize()
    request = execution_request(task_id=f"task_{uuid4().hex}")
    await executions.submit(request, scope=PROFILE_SCOPE)

    chain = await GatewayAudit(config, scope=PROFILE_SCOPE).trace(request.id)

    task = _link(chain, "durable_task")
    assert task.state == "missing"
    assert task.id == request.task_id


async def test_a_missing_grant_is_reported(tmp_path: Path) -> None:
    chain = await GatewayAudit(settings(tmp_path), scope=PROFILE_SCOPE).trace(f"grant_{'b' * 32}")

    assert chain.resolved_kind == "delegation_grant"
    assert _link(chain, "delegation_grant").state == "missing"


async def test_a_conversation_chain_reaches_a_correlated_execution(tmp_path: Path) -> None:
    config = settings(tmp_path)
    gateway = GatewayStore(config)
    sessions = SessionStore(config)
    notifications = NotificationStore(config)
    executions = ExecutionStore(config)
    await gateway.initialize()
    await sessions.initialize()
    await notifications.initialize()
    await executions.initialize()
    conversation_id, session_id = await make_conversation(gateway, sessions)
    await gateway.begin_result(
        message_id=f"inbound_{uuid4().hex}",
        conversation_id=conversation_id,
        session_id=session_id,
        scope=PROFILE_SCOPE,
    )
    request = execution_request()
    await executions.submit(request, scope=PROFILE_SCOPE)
    await enqueue_notification(
        notifications,
        correlations=[
            CorrelationRef(
                kind="conversation", id=conversation_id, profile_label=PROFILE_SCOPE.label()
            ),
            CorrelationRef(
                kind="execution_request", id=request.id, profile_label=PROFILE_SCOPE.label()
            ),
        ],
    )

    chain = await GatewayAudit(config, scope=PROFILE_SCOPE).trace(conversation_id)

    assert _link(chain, "conversation").state == "present"
    assert _link(chain, "execution_request").id == request.id


async def test_the_audit_changes_no_state(tmp_path: Path) -> None:
    config = settings(tmp_path)
    messaging = MessagingStore(config)
    await messaging.initialize()
    message = await store_inbound(messaging, build_inbound())
    before = await messaging.get_inbox(message.id)

    await GatewayAudit(config, scope=PROFILE_SCOPE).trace(message.id)

    assert (await messaging.get_inbox(message.id)) == before
