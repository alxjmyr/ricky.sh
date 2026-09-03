"""Strict platform-neutral contracts for durable messaging transports."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ricky.attachments import StoredAttachment
from ricky.notifications.types import MessageTextFormat

InboundStatus = Literal["pending", "claimed", "processed", "rejected", "uncertain"]
InboundAttemptOutcome = Literal["accepted", "rejected", "duplicate"]
DeliveryPartStatus = Literal["pending", "delivered", "in_doubt"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InboundMessage(_StrictModel):
    """One normalized inbound text message or bounded rejected update."""

    id: str = Field(pattern=r"^inbound_[0-9a-f]{32}$")
    transport: str = Field(min_length=1, max_length=100)
    account: str = Field(min_length=1, max_length=100)
    update_id: str = Field(min_length=1, max_length=100)
    destination_id: str = Field(min_length=1, max_length=500)
    thread_id: str | None = Field(default=None, min_length=1, max_length=500)
    sender_id: str = Field(min_length=1, max_length=500)
    platform_message_id: str = Field(min_length=1, max_length=500)
    reply_to_platform_message_id: str | None = Field(default=None, min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=20_000)
    received_at: datetime
    status: InboundStatus

    @model_validator(mode="after")
    def _validate_state(self) -> InboundMessage:
        _require_utc(self.received_at, "received_at")
        if self.status == "rejected" and self.text != "[rejected update]":
            raise ValueError("rejected inbound messages must use bounded placeholder text")
        return self


class TransportCursor(_StrictModel):
    """Last durably handled update for one transport account."""

    transport: str = Field(min_length=1, max_length=100)
    account: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=100)


class InboundAttempt(_StrictModel):
    """Immutable activity for one observed transport update."""

    id: str = Field(pattern=r"^inbound_attempt_[0-9a-f]{32}$")
    transport: str = Field(min_length=1, max_length=100)
    account: str = Field(min_length=1, max_length=100)
    update_id: str = Field(min_length=1, max_length=100)
    message_id: str | None = Field(default=None, pattern=r"^inbound_[0-9a-f]{32}$")
    outcome: InboundAttemptOutcome
    reason: str | None = Field(default=None, max_length=500)
    created_at: datetime

    @model_validator(mode="after")
    def _validate_attempt(self) -> InboundAttempt:
        _require_utc(self.created_at, "created_at")
        if self.outcome == "accepted" and self.message_id is None:
            raise ValueError("accepted inbound attempts require a message id")
        return self


class InboxClaim(_StrictModel):
    """Expiring fenced ownership of one pending inbox message."""

    message_id: str = Field(pattern=r"^inbound_[0-9a-f]{32}$")
    owner: str = Field(min_length=1, max_length=200)
    token: str = Field(pattern=r"^[0-9a-f]{32}$")
    fence: int = Field(ge=1)
    expires_at: datetime

    @model_validator(mode="after")
    def _validate_expiry(self) -> InboxClaim:
        _require_utc(self.expires_at, "expires_at")
        return self


class ReceivedUpdate(_StrictModel):
    """One handled update returned by an adapter before durable ingestion."""

    update_id: str = Field(min_length=1, max_length=100)
    message: InboundMessage
    rejection_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _validate_result(self) -> ReceivedUpdate:
        if self.message.update_id != self.update_id:
            raise ValueError("received update and message ids must match")
        if self.message.status == "rejected" and self.rejection_reason is None:
            raise ValueError("rejected updates require a bounded reason")
        if self.message.status != "rejected" and self.rejection_reason is not None:
            raise ValueError("accepted updates cannot carry a rejection reason")
        return self


class ReceiveBatch(_StrictModel):
    """Ordered updates and the cursor safe to commit with them."""

    transport: str = Field(min_length=1, max_length=100)
    account: str = Field(min_length=1, max_length=100)
    updates: list[ReceivedUpdate] = Field(default_factory=list)
    next_cursor: TransportCursor | None = None

    @model_validator(mode="after")
    def _validate_batch(self) -> ReceiveBatch:
        if self.next_cursor is not None and (
            self.next_cursor.transport != self.transport or self.next_cursor.account != self.account
        ):
            raise ValueError("receive cursor must belong to the batch account")
        if any(
            update.message.transport != self.transport or update.message.account != self.account
            for update in self.updates
        ):
            raise ValueError("all received messages must belong to the batch account")
        return self


class TransportMessage(_StrictModel):
    """One exact outbound text or attachment part supplied to an adapter."""

    id: str = Field(pattern=r"^transport_message_[0-9a-f]{32}$")
    transport: str = Field(min_length=1, max_length=100)
    account: str = Field(min_length=1, max_length=100)
    destination_id: str = Field(min_length=1, max_length=500)
    text: str = Field(default="", max_length=20_000)
    text_format: MessageTextFormat = "plain_text"
    attachment: StoredAttachment | None = None
    outbox_id: str = Field(min_length=1, max_length=100)
    part_number: int = Field(ge=1)
    part_count: int = Field(ge=1)
    reply_to_platform_message_id: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def _validate_part(self) -> TransportMessage:
        if self.part_number > self.part_count:
            raise ValueError("part_number cannot exceed part_count")
        if not self.text and self.attachment is None:
            raise ValueError("transport message requires text or an attachment")
        return self


class DeliveryReceipt(_StrictModel):
    """Confirmed platform receipt for one outbound text part."""

    transport: str = Field(min_length=1, max_length=100)
    account: str = Field(min_length=1, max_length=100)
    transport_message_id: str = Field(pattern=r"^transport_message_[0-9a-f]{32}$")
    platform_message_id: str = Field(min_length=1, max_length=500)
    destination_id: str = Field(min_length=1, max_length=500)
    delivered_at: datetime

    @model_validator(mode="after")
    def _validate_receipt(self) -> DeliveryReceipt:
        _require_utc(self.delivered_at, "delivered_at")
        return self


class DeliveryPart(_StrictModel):
    """Durable exact outbound part and its current confirmation state."""

    outbox_id: str = Field(min_length=1, max_length=100)
    fence: int = Field(ge=1)
    message: TransportMessage
    status: DeliveryPartStatus
    platform_message_id: str | None = Field(default=None, min_length=1, max_length=500)
    error: str | None = Field(default=None, max_length=2_000)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _validate_delivery_part(self) -> DeliveryPart:
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.status == "delivered" and self.platform_message_id is None:
            raise ValueError("delivered parts require a platform message id")
        if self.status != "delivered" and self.platform_message_id is not None:
            raise ValueError("only delivered parts can hold a platform message id")
        if self.status == "in_doubt" and self.error is None:
            raise ValueError("in_doubt parts require an error")
        return self


class StaleInboxClaim(_StrictModel):
    """One claimed inbox message whose worker lease has expired."""

    message: InboundMessage
    owner: str = Field(min_length=1, max_length=200)
    fence: int = Field(ge=1)
    expired_at: datetime

    @model_validator(mode="after")
    def _validate_stale_claim(self) -> StaleInboxClaim:
        _require_utc(self.expired_at, "expired_at")
        if self.message.status != "claimed":
            raise ValueError("a stale inbox claim must describe a claimed message")
        return self


class PollerLeaseRecord(_StrictModel):
    """One stored transport poller lease and its expiry."""

    transport: str = Field(min_length=1, max_length=100)
    account: str = Field(min_length=1, max_length=100)
    owner: str = Field(min_length=1, max_length=200)
    fence: int = Field(ge=1)
    expires_at: datetime

    @model_validator(mode="after")
    def _validate_poller_lease(self) -> PollerLeaseRecord:
        _require_utc(self.expires_at, "expires_at")
        return self


@runtime_checkable
class MessageTransport(Protocol):
    async def receive(self, cursor: TransportCursor | None) -> ReceiveBatch: ...

    async def send(self, message: TransportMessage) -> DeliveryReceipt: ...

    async def aclose(self) -> None: ...


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{name} must use UTC")
