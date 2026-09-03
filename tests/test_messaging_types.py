"""JSON and invariant tests for platform-neutral messaging contracts."""

from datetime import UTC, datetime

from ricky.messaging.types import (
    DeliveryPart,
    DeliveryReceipt,
    InboundAttempt,
    InboundMessage,
    InboxClaim,
    ReceiveBatch,
    ReceivedUpdate,
    TransportCursor,
    TransportMessage,
)


def test_all_messaging_models_survive_json_round_trips() -> None:
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    inbound = InboundMessage(
        id="inbound_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        transport="telegram",
        account="personal",
        update_id="10",
        destination_id="200",
        sender_id="100",
        platform_message_id="300",
        text="hello",
        received_at=now,
        status="pending",
    )
    update = ReceivedUpdate(update_id="10", message=inbound)
    cursor = TransportCursor(transport="telegram", account="personal", value="10")
    batch = ReceiveBatch(
        transport="telegram", account="personal", updates=[update], next_cursor=cursor
    )
    attempt = InboundAttempt(
        id="inbound_attempt_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        transport="telegram",
        account="personal",
        update_id="10",
        message_id=inbound.id,
        outcome="accepted",
        created_at=now,
    )
    claim = InboxClaim(
        message_id=inbound.id,
        owner="worker",
        token="c" * 32,
        fence=1,
        expires_at=now,
    )
    outbound = TransportMessage(
        id="transport_message_dddddddddddddddddddddddddddddddd",
        transport="telegram",
        account="personal",
        destination_id="200",
        text="reply",
        text_format="portable_markdown_v1",
        outbox_id="outbox_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        part_number=1,
        part_count=1,
    )
    receipt = DeliveryReceipt(
        transport="telegram",
        account="personal",
        transport_message_id=outbound.id,
        platform_message_id="400",
        destination_id="200",
        delivered_at=now,
    )
    part = DeliveryPart(
        outbox_id=outbound.outbox_id,
        fence=1,
        message=outbound,
        status="delivered",
        platform_message_id="400",
        created_at=now,
        updated_at=now,
    )

    for model in (inbound, update, cursor, batch, attempt, claim, outbound, receipt, part):
        assert type(model).model_validate_json(model.model_dump_json()) == model


def test_transport_message_defaults_legacy_json_to_plain_text() -> None:
    message = TransportMessage.model_validate(
        {
            "id": "transport_message_dddddddddddddddddddddddddddddddd",
            "transport": "telegram",
            "account": "personal",
            "destination_id": "200",
            "text": "literal *text*",
            "outbox_id": "outbox_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            "part_number": 1,
            "part_count": 1,
        }
    )

    assert message.text_format == "plain_text"
