"""Normal messaging receipts bind a code reply to its live browser owner."""

from datetime import UTC, datetime, timedelta

import pytest

from authority_support import CONVERSATION_ID, PRINCIPAL, inbound, settings
from browser_challenge_support import Journal, record
from gateway_conversation_support import HandoffTransport, _handoff_messaging
from ricky.browser.challenges import LiveBrowserChallenge
from ricky.executions.challenges import ExecutionBrowserChallenges
from ricky.executions.types import ExecutionRequest
from ricky.messaging.store import MessagingStore
from ricky.notifications.service import NotificationService


@pytest.mark.parametrize("scenario", ["otp", "manual", "delivery_race", "expired"])
async def test_delivered_challenge_reply_claims_once_and_does_not_echo_code(
    tmp_path, monkeypatch, scenario
):
    config = settings(tmp_path)
    config.messaging.telegram_accounts["personal/owner-bot"].allowed_destination_ids = [
        "chat-owner"
    ]
    initial = record()
    if scenario == "manual":
        initial = initial.model_copy(update={"kind": "manual"})
    scope = initial.binding.profile_scope
    journal = Journal(initial)
    owner = LiveBrowserChallenge(initial, writer=journal.write)
    service = ExecutionBrowserChallenges(NotificationService(config), MessagingStore(config))
    execution = ExecutionRequest(
        id="execution_" + "a" * 32,
        kind="ad_hoc",
        status="running",
        goal="Verify account",
        contract_id="contract_" + "b" * 32,
        contract_digest="c" * 64,
        task_id="task_" + "d" * 32,
        task_revision=1,
        profile_scope=scope,
        source_conversation_id=CONVERSATION_ID,
        notification_route="owner",
        request_key="test",
        created_at=datetime.now(UTC),
        claimed_by="worker",
        claim_token="claim",
        claim_fence=1,
        claim_expires_at=datetime.now(UTC) + timedelta(minutes=10),
        run_id="jobrun_" + "e" * 32,
    )
    await service.request(owner, execution, PRINCIPAL)
    transport = HandoffTransport()
    messaging = _handoff_messaging(config, transport)
    assert await messaging.deliver_once() == 1
    assert len(transport.sent) == 1
    assert "Reply directly" in transport.sent[0].text
    reply = inbound("done" if scenario == "manual" else "123456").model_copy(
        update={"reply_to_platform_message_id": "1"}
    )
    wrong = reply.model_copy(update={"sender_id": "someone-else"})
    assert "does not belong" in (
        await service.respond(wrong, conversation_id=CONVERSATION_ID, scope=scope) or ""
    )
    if scenario == "delivery_race":
        lookup = service.messaging.find_delivery_part
        calls = 0

        async def delayed_receipt(**kwargs):
            nonlocal calls
            calls += 1
            return None if calls <= 2 else await lookup(**kwargs)

        monkeypatch.setattr(service.messaging, "find_delivery_part", delayed_receipt)
    if scenario == "expired":
        await owner.finish("expired")
        result = await service.respond(reply, conversation_id=CONVERSATION_ID, scope=scope)
        assert result and "no longer" in result
        assert owner.record.state == "expired"
        return
    accepted = await service.respond(reply, conversation_id=CONVERSATION_ID, scope=scope)
    assert accepted == (
        "Response received. Ricky will check the browser before continuing."
        if scenario == "manual"
        else "Verification code received. The browser task will continue."
    )
    assert owner.record.state == "responded"
    assert ((await owner.wait()).code is None) == (scenario == "manual")
    duplicate = await service.respond(reply, conversation_id=CONVERSATION_ID, scope=scope)
    assert duplicate and "already answered" in duplicate
    restarted = ExecutionBrowserChallenges(NotificationService(config), MessagingStore(config))
    unavailable = await restarted.respond(reply, conversation_id=CONVERSATION_ID, scope=scope)
    assert unavailable and "No code was submitted" in unavailable
