"""Real-store handoff crash windows and acknowledgement evidence boundaries."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from gateway_ops_support import PROFILE_SCOPE as SCOPE
from gateway_ops_support import (
    execution_request,
    inbound,
    make_conversation,
    settings,
    store_inbound,
)
from ricky.agent.handoff import BackgroundHandoff, background_handoff_acknowledgement
from ricky.executions.store import ExecutionStore
from ricky.gateway.handoffs import enqueue_foreground_response, reconcile_handoffs
from ricky.gateway.recovery import GatewayRecovery
from ricky.gateway.store import GatewayStore
from ricky.gateway.types import GatewayInboundResult
from ricky.messaging.store import MessagingStore
from ricky.messaging.types import DeliveryReceipt, TransportMessage
from ricky.notifications.store import NotificationStore
from ricky.notifications.types import CorrelationRef, NotificationRequest, OutboxEntry
from ricky.profiles import ProfileScope
from ricky.sessions.store import SessionStore
from ricky.sessions.types import StoredTurn

pytestmark = pytest.mark.asyncio
ACK = "I'll check the balance in the background and report back here."


class HandoffCase:
    def __init__(self, tmp_path: Path) -> None:
        self.settings = settings(tmp_path)
        self.messaging = MessagingStore(self.settings)
        self.notifications = NotificationStore(self.settings)
        self.gateway = GatewayStore(self.settings)
        self.sessions = SessionStore(self.settings)
        self.executions = ExecutionStore(self.settings)

    async def prepare(
        self,
        *,
        committed: bool = True,
        handoff_evidence: bool = True,
        wrong_request: bool = False,
        count: int = 1,
        acknowledgement: str = ACK,
    ) -> None:
        for store in (
            self.messaging,
            self.notifications,
            self.gateway,
            self.sessions,
            self.executions,
        ):
            await store.initialize()
        self.message = await store_inbound(self.messaging, inbound())
        conversation_id, session_id = await make_conversation(self.gateway, self.sessions)
        self.conversation = await self.gateway.get(conversation_id, scope=SCOPE)
        await self.gateway.begin_result(
            message_id=self.message.id,
            conversation_id=conversation_id,
            session_id=session_id,
            scope=SCOPE,
        )
        self.request = execution_request(
            source_message_id=self.message.id,
            source_conversation_id=conversation_id,
        ).model_copy(
            update={
                "status": "awaiting_acknowledgement",
                "handoff_title": "Check balance",
                "acknowledgement_expires_at": datetime.now(UTC) + timedelta(hours=1),
            }
        )
        await self.executions.submit(self.request, scope=SCOPE)
        self.requests = [self.request]
        for _ in range(count - 1):
            extra = self.request.model_copy(
                update={
                    "id": f"execution_{uuid4().hex}",
                    "request_key": uuid4().hex,
                    "contract_id": f"contract_{uuid4().hex}",
                    "task_id": f"task_{uuid4().hex}",
                }
            )
            await self.executions.submit(extra, scope=SCOPE)
            self.requests.append(extra)
        stored = await self.sessions.get(session_id, scope=SCOPE)
        lease = await self.sessions.acquire(session_id, "test", scope=SCOPE)
        self.turn = StoredTurn(
            id=f"turn_{uuid4().hex}",
            session_id=session_id,
            profile_label=SCOPE.label(),
            inbound_ref=self.message.id,
            base_revision=stored.revision,
            status="running",
            started_at=datetime.now(UTC),
            background_handoffs=[
                BackgroundHandoff(
                    request_id=f"execution_{uuid4().hex}" if wrong_request else item.id,
                    title="Check balance",
                )
                for item in self.requests
            ]
            if handoff_evidence
            else [],
            handoff_acknowledgement=acknowledgement if handoff_evidence else None,
        )
        await self.sessions.begin_turn(lease, self.turn)
        if committed:
            await self.sessions.commit(lease, stored.revision, stored.session, self.turn)
        await self.sessions.release(lease)

    async def reconcile(self, *, now: datetime | None = None) -> int:
        return await reconcile_handoffs(
            self.settings,
            gateway=self.gateway,
            messaging=self.messaging,
            notifications=self.notifications,
            sessions=self.sessions,
            executions=self.executions,
            scope=SCOPE,
            now=now,
        )

    async def enqueue(self) -> str:
        return await enqueue_foreground_response(
            self.settings,
            notifications=self.notifications,
            executions=self.executions,
            inbound=self.message,
            conversation=self.conversation,
            body=self.turn.handoff_acknowledgement or ACK,
            handoffs=self.turn.background_handoffs,
        )

    async def delivered(self, outbox_id: str, *, parts: int = 1) -> OutboxEntry:
        entry = await self.notifications.claim(
            outbox_id,
            scope=SCOPE,
            worker="test",
            transport="telegram",
            destination_ref="200",
        )
        messages = [
            TransportMessage(
                id=f"transport_message_{uuid4().hex}",
                transport="telegram",
                account="personal/bot",
                destination_id="200",
                text=ACK,
                outbox_id=entry.id,
                part_number=index + 1,
                part_count=parts,
            )
            for index in range(parts)
        ]
        await self.messaging.prepare_parts(entry, messages)
        for message in messages:
            await self.messaging.record_receipt(
                entry,
                DeliveryReceipt(
                    transport="telegram",
                    account=message.account,
                    destination_id="200",
                    transport_message_id=message.id,
                    platform_message_id=message.id,
                    delivered_at=datetime.now(UTC),
                ),
            )
        await self.notifications.mark_delivered(
            entry, scope=SCOPE, platform_message_id=messages[-1].id
        )
        return entry


@pytest.mark.parametrize("committed,handoff_evidence", [(False, True), (True, False)])
async def test_no_acknowledgement_without_committed_handoff_evidence(
    tmp_path: Path,
    committed: bool,
    handoff_evidence: bool,
) -> None:
    case = HandoffCase(tmp_path)
    await case.prepare(committed=committed, handoff_evidence=handoff_evidence)
    assert await case.reconcile() == 0
    assert await case.notifications.list(scope=SCOPE) == []
    assert await case.executions.claim(scope=SCOPE, worker_id="early", limit=1) == []


@pytest.mark.parametrize("already_enqueued", [False, True])
async def test_committed_turn_recovers_acknowledgement_without_model_replay(
    tmp_path: Path,
    already_enqueued: bool,
) -> None:
    case = HandoffCase(tmp_path)
    await case.prepare()
    prior = await case.enqueue() if already_enqueued else None
    assert await case.reconcile() == 0
    result = await case.gateway.get_result(case.message.id, scope=SCOPE)
    assert result is not None and result.status == "committed"
    assert result.response_outbox_id is not None
    if prior is not None:
        assert result.response_outbox_id == prior
    assert (await case.messaging.get_inbox(case.message.id)).status == "processed"
    assert len(await case.notifications.list(scope=SCOPE)) == 1
    await case.delivered(result.response_outbox_id)
    assert await case.reconcile() == 1
    assert await case.reconcile() == 0
    assert len(await case.executions.claim(scope=SCOPE, worker_id="worker", limit=1)) == 1
    assert len(await case.notifications.list(scope=SCOPE)) == 1


async def test_live_foreground_owner_is_not_stolen(tmp_path: Path) -> None:
    case = HandoffCase(tmp_path)
    await case.prepare()
    await case.messaging.claim_inbox(case.message.id, owner="live", lease_seconds=600)
    assert await case.reconcile() == 0
    assert await case.notifications.list(scope=SCOPE) == []
    assert await case.reconcile(now=datetime.now(UTC) + timedelta(seconds=601)) == 0
    result = await case.gateway.get_result(case.message.id, scope=SCOPE)
    assert result is not None and result.status == "committed"


async def test_multipart_delivery_needs_every_receipt_and_terminal_outbox(tmp_path: Path) -> None:
    case = HandoffCase(tmp_path)
    await case.prepare()
    await case.reconcile()
    request = await case.executions.get(case.request.id, scope=SCOPE)
    assert request.acknowledgement_outbox_id is not None
    entry = await case.notifications.claim(
        request.acknowledgement_outbox_id,
        scope=SCOPE,
        worker="test",
        transport="telegram",
        destination_ref="200",
    )
    messages = [
        TransportMessage(
            id=f"transport_message_{uuid4().hex}",
            transport="telegram",
            account="personal/bot",
            destination_id="200",
            text="part",
            outbox_id=entry.id,
            part_number=index + 1,
            part_count=2,
        )
        for index in range(2)
    ]
    await case.messaging.prepare_parts(entry, messages)
    for index, message in enumerate(messages):
        await case.messaging.record_receipt(
            entry,
            DeliveryReceipt(
                transport="telegram",
                account="personal/bot",
                destination_id="200",
                transport_message_id=message.id,
                platform_message_id=str(index),
                delivered_at=datetime.now(UTC),
            ),
        )
        assert await case.reconcile() == 0
        assert await case.executions.claim(scope=SCOPE, worker_id="early", limit=1) == []
    await case.notifications.mark_delivered(entry, scope=SCOPE, platform_message_id="1")
    assert await case.reconcile() == 1


@pytest.mark.parametrize("change", ["body", "source", "accepted_request"])
async def test_wrong_acknowledgement_evidence_cannot_release_work(
    tmp_path: Path, change: str
) -> None:
    case = HandoffCase(tmp_path)
    await case.prepare(wrong_request=change == "accepted_request")
    record = await case.notifications.enqueue(
        NotificationRequest(
            id=f"notification_{uuid4().hex}",
            route=f"inbox:{case.message.id}",
            body="Unrelated response" if change == "body" else ACK,
            source_kind="gateway_turn",
            source_id="wrong" if change == "source" else case.message.id,
            profile_label=SCOPE.label(),
            dedupe_key=case.message.id,
            created_at=datetime.now(UTC),
            correlations=[
                CorrelationRef(
                    kind="execution_request",
                    id=case.request.id,
                    profile_label=SCOPE.label(),
                )
            ],
        ),
        scope=SCOPE,
    )
    await case.gateway.finish_result(
        message_id=case.message.id,
        conversation_id=case.conversation.id,
        expected_conversation_revision=case.conversation.revision,
        status="committed",
        session_revision=case.turn.base_revision + 1,
        response_outbox_id=record.outbox.id,
        error=None,
        scope=SCOPE,
    )
    await case.delivered(record.outbox.id)
    assert await case.reconcile() == 0
    assert await case.executions.claim(scope=SCOPE, worker_id="early", limit=1) == []


@pytest.mark.parametrize("terminal", ["cancelled", "expired"])
async def test_late_acknowledgement_never_starts_cancelled_or_expired_work(
    tmp_path: Path,
    terminal: str,
) -> None:
    case = HandoffCase(tmp_path)
    await case.prepare()
    await case.reconcile()
    request = await case.executions.get(case.request.id, scope=SCOPE)
    assert request.acknowledgement_outbox_id is not None
    if terminal == "cancelled":
        await case.executions.cancel(request.id, scope=SCOPE)
    await case.delivered(request.acknowledgement_outbox_id)
    now = datetime.now(UTC) + timedelta(hours=2) if terminal == "expired" else None
    assert await case.reconcile(now=now) == 0
    changed = await case.executions.get(request.id, scope=SCOPE)
    assert changed.status == ("blocked" if terminal == "expired" else "cancelled")
    assert changed.acknowledgement_delivered_at is not None
    assert await case.executions.claim(scope=SCOPE, worker_id="late", limit=1) == []


async def test_archiving_conversation_preserves_acknowledged_work(tmp_path: Path) -> None:
    case = HandoffCase(tmp_path)
    await case.prepare()
    await case.reconcile()
    request = await case.executions.get(case.request.id, scope=SCOPE)
    assert request.acknowledgement_outbox_id is not None
    await case.delivered(request.acknowledgement_outbox_id)
    conversation = await case.gateway.get(case.conversation.id, scope=SCOPE)
    await case.gateway.archive(
        conversation.id, expected_revision=conversation.revision, scope=SCOPE
    )
    await asyncio.gather(case.reconcile(), case.reconcile())
    assert len(await case.executions.claim(scope=SCOPE, worker_id="worker", limit=1)) == 1
    assert await case.executions.claim(scope=SCOPE, worker_id="duplicate", limit=1) == []


async def test_large_batch_uses_committed_handoff_evidence_beyond_correlation_limit(
    tmp_path: Path,
) -> None:
    case = HandoffCase(tmp_path)
    await case.prepare(count=25)
    assert await case.reconcile() == 0
    records = await case.notifications.list(scope=SCOPE)
    assert len(records) == 1
    assert len(records[0].request.correlations) <= 20
    await case.delivered(records[0].outbox.id)
    assert await case.reconcile() == 25
    assert all(
        [
            (await case.executions.get(request.id, scope=SCOPE)).status == "queued"
            for request in case.requests
        ]
    )


async def test_delivered_outbox_without_transport_receipt_is_not_proof(tmp_path: Path) -> None:
    case = HandoffCase(tmp_path)
    await case.prepare()
    await case.reconcile()
    records = await case.notifications.list(scope=SCOPE)
    entry = await case.notifications.claim(
        records[0].outbox.id,
        scope=SCOPE,
        worker="test",
        transport="telegram",
        destination_ref="200",
    )
    await case.notifications.mark_delivered(entry, scope=SCOPE, platform_message_id="operator")
    assert await case.reconcile() == 0
    assert await case.executions.claim(scope=SCOPE, worker_id="early", limit=1) == []


async def test_startup_recovery_repairs_commit_gap_before_marking_turn_uncertain(
    tmp_path: Path,
) -> None:
    case = HandoffCase(tmp_path)
    await case.prepare()
    await case.messaging.claim_inbox(case.message.id, owner="dead", lease_seconds=1)
    plan = await GatewayRecovery(case.settings, scope=SCOPE).apply(
        now=datetime.now(UTC) + timedelta(seconds=2)
    )
    assert plan.failures == ()
    result = await case.gateway.get_result(case.message.id, scope=SCOPE)
    assert result is not None and result.status == "committed"
    assert (await case.messaging.get_inbox(case.message.id)).status == "processed"
    assert (await case.gateway.get(case.conversation.id, scope=SCOPE)).status == "active"
    assert len(await case.notifications.list(scope=SCOPE)) == 1


async def test_broken_handoff_evidence_does_not_block_another_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = HandoffCase(tmp_path)
    await case.prepare()
    await case.reconcile()
    entry = (await case.notifications.list(scope=SCOPE))[0].outbox
    await case.delivered(entry.id)
    missing_conversation = f"conversation_{uuid4().hex}"
    missing_message = f"inbound_{uuid4().hex}"
    broken = execution_request(
        source_message_id=missing_message,
        source_conversation_id=missing_conversation,
        created_at=datetime(2020, 1, 1, tzinfo=UTC),
    ).model_copy(
        update={
            "status": "awaiting_acknowledgement",
            "handoff_title": "Missing source",
            "acknowledgement_expires_at": datetime.now(UTC) + timedelta(hours=1),
        }
    )
    await case.executions.submit(broken, scope=SCOPE)
    original = case.gateway.get_result

    async def fail_broken(message_id: str, *, scope: ProfileScope) -> GatewayInboundResult | None:
        if message_id == missing_message:
            raise ValueError("corrupt gateway result")
        return await original(message_id, scope=scope)

    monkeypatch.setattr(case.gateway, "get_result", fail_broken)
    assert await case.reconcile() == 1
    assert (await case.executions.get(broken.id, scope=SCOPE)).status == "awaiting_acknowledgement"


async def test_mixed_handoff_question_with_trailing_newline_keeps_matching_delivery_proof(
    tmp_path: Path,
) -> None:
    case = HandoffCase(tmp_path)
    acknowledgement = (
        "I'll run this in the background and report back here: Check balance\n\n"
        "For the other request: which account should I use?\n"
    )
    await case.prepare(acknowledgement=acknowledgement)
    await case.enqueue()
    assert await case.reconcile() == 0
    records = await case.notifications.list(scope=SCOPE)
    assert len(records) == 1
    assert records[0].request.body == acknowledgement.strip()
    assert "which account should I use?" in records[0].request.body
    assert await case.executions.claim(scope=SCOPE, worker_id="early", limit=1) == []
    await case.delivered(records[0].outbox.id)
    assert await case.reconcile() == 1


@pytest.mark.parametrize("old_limit,new_limit", [(100, 2_000), (2_000, 100)])
async def test_delivered_acknowledgement_survives_body_limit_configuration_change(
    tmp_path: Path,
    old_limit: int,
    new_limit: int,
) -> None:
    case = HandoffCase(tmp_path)
    case.settings = case.settings.model_copy(
        update={
            "messaging": case.settings.messaging.model_copy(update={"body_char_limit": old_limit})
        }
    )
    acknowledgement = background_handoff_acknowledgement(
        [BackgroundHandoff(request_id="placeholder", title="Check balance") for _ in range(25)]
    )
    await case.prepare(count=25, acknowledgement=acknowledgement)
    outbox_id = await case.enqueue()
    assert await case.reconcile() == 0
    record = await case.notifications.get_by_outbox(outbox_id, scope=SCOPE)
    assert len(record.request.body) <= old_limit
    await case.delivered(outbox_id)
    case.settings = case.settings.model_copy(
        update={
            "messaging": case.settings.messaging.model_copy(update={"body_char_limit": new_limit})
        }
    )
    assert await case.reconcile() == 25
