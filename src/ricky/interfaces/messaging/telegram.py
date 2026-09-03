"""Telegram Bot API wire adapter with no reasoning or application policy."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ricky.attachments import read_stored_attachment
from ricky.config import TelegramAccountSettings
from ricky.messaging.errors import (
    AmbiguousDeliveryError,
    DeliveryNotPerformedError,
    TransportError,
)
from ricky.messaging.markdown import (
    normalize_portable_markdown,
    portable_markdown_to_plain_text,
    split_message_text,
)
from ricky.messaging.types import (
    DeliveryReceipt,
    InboundMessage,
    ReceiveBatch,
    ReceivedUpdate,
    TransportCursor,
    TransportMessage,
)
from ricky.notifications.types import MessageTextFormat

TELEGRAM_TEXT_LIMIT = 4_000


class TelegramTransportError(TransportError):
    """Telegram request or response failed without exposing credentials."""


class TelegramConflictError(TelegramTransportError):
    """Telegram reports another getUpdates consumer for this bot."""


class TelegramDeliveryError(DeliveryNotPerformedError, TelegramTransportError):
    """A send was confirmed not to have completed and may be retried explicitly."""


class TelegramAmbiguousDeliveryError(AmbiguousDeliveryError, TelegramTransportError):
    """A send might have reached Telegram and must not be retried automatically."""


class TelegramRichFormattingError(TelegramDeliveryError):
    """Telegram definitively rejected rich formatting before delivery."""


class TelegramBotIdentity(BaseModel):
    """Credential-free result of an explicit getMe diagnostic."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    username: str | None = Field(default=None, min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=500)


class TelegramTransport:
    """One configured Telegram Bot API account."""

    def __init__(
        self,
        account: str,
        settings: TelegramAccountSettings,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Any = None,
        user_data_root: Path | None = None,
    ) -> None:
        self.account = account
        self.settings = settings
        token = settings.bot_token.get_secret_value()
        self._base_url = f"{settings.api_base_url}/bot{token}"
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.long_poll_timeout_seconds + 10.0)
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._user_data_root = user_data_root
        self._closed = False

    async def doctor(self) -> TelegramBotIdentity:
        payload = await self._request("getMe", {})
        result = payload.get("result")
        if not isinstance(result, dict):
            raise TelegramTransportError("Telegram getMe returned an invalid response")
        identifier = _platform_id(result.get("id"))
        first_name = result.get("first_name")
        username = result.get("username")
        if identifier is None or not isinstance(first_name, str) or not first_name:
            raise TelegramTransportError("Telegram getMe returned an invalid bot identity")
        return TelegramBotIdentity(
            id=identifier,
            username=username if isinstance(username, str) and username else None,
            display_name=first_name,
        )

    async def receive(self, cursor: TransportCursor | None) -> ReceiveBatch:
        if cursor is not None and (
            cursor.transport != "telegram" or cursor.account != self.account
        ):
            raise ValueError("Telegram cursor belongs to a different transport account")
        data: dict[str, Any] = {
            "timeout": self.settings.long_poll_timeout_seconds,
            "allowed_updates": ["message"],
        }
        if cursor is not None:
            try:
                data["offset"] = int(cursor.value) + 1
            except ValueError:
                raise ValueError("Telegram cursor must contain a decimal update id") from None
        payload = await self._request("getUpdates", data)
        result = payload.get("result")
        if not isinstance(result, list):
            raise TelegramTransportError("Telegram getUpdates returned an invalid result")
        received_at = self._now()
        updates: list[ReceivedUpdate] = []
        numeric_ids: list[int] = []
        for raw in result:
            if not isinstance(raw, dict):
                continue
            update_id = _platform_id(raw.get("update_id"))
            if update_id is None:
                digest = hashlib.sha256(repr(sorted(raw.keys())).encode()).hexdigest()[:24]
                update_id = f"malformed-{digest}"
            else:
                numeric_ids.append(int(update_id))
            updates.append(self._normalize(raw, update_id, received_at))
        next_cursor = None
        if numeric_ids:
            next_cursor = TransportCursor(
                transport="telegram",
                account=self.account,
                value=str(max(numeric_ids)),
            )
        return ReceiveBatch(
            transport="telegram",
            account=self.account,
            updates=updates,
            next_cursor=next_cursor,
        )

    async def send(self, message: TransportMessage) -> DeliveryReceipt:
        if message.transport != "telegram" or message.account != self.account:
            raise ValueError("outbound message belongs to a different transport account")
        if message.attachment is None and len(message.text) > TELEGRAM_TEXT_LIMIT:
            raise ValueError(f"Telegram text parts cannot exceed {TELEGRAM_TEXT_LIMIT} characters")
        destination = _platform_id(message.destination_id)
        if destination is None:
            raise TelegramDeliveryError("Telegram destination id is malformed")
        data: dict[str, Any]
        files: dict[str, tuple[str, bytes, str]] | None = None
        method = "sendMessage"
        plain_fallback: str | None = None
        if message.attachment is None:
            if message.text_format == "portable_markdown_v1":
                method = "sendRichMessage"
                markdown = normalize_portable_markdown(message.text)
                plain_fallback = portable_markdown_to_plain_text(markdown)
                data = {
                    "chat_id": destination,
                    "rich_message": {
                        "markdown": markdown,
                        "skip_entity_detection": True,
                    },
                }
            else:
                data = {"chat_id": destination, "text": message.text}
        else:
            if self._user_data_root is None:
                raise TelegramDeliveryError(
                    "Telegram attachment delivery has no configured user-data root"
                )
            try:
                content = await asyncio.to_thread(
                    read_stored_attachment,
                    message.attachment,
                    user_root=self._user_data_root,
                )
            except (OSError, ValueError) as exc:
                raise TelegramDeliveryError(f"Telegram attachment is unavailable: {exc}") from exc
            method = "sendDocument"
            data = {"chat_id": destination}
            if message.text:
                data["caption"] = message.text
            files = {
                "document": (
                    message.attachment.filename,
                    content,
                    message.attachment.media_type,
                )
            }
        if message.reply_to_platform_message_id is not None:
            reply_id = _platform_id(message.reply_to_platform_message_id)
            if reply_id is None:
                raise TelegramDeliveryError("Telegram reply message id is malformed")
            reply_parameters = {"message_id": int(reply_id)}
            data["reply_parameters"] = (
                json.dumps(reply_parameters) if files is not None else reply_parameters
            )
        try:
            payload = await self._request(
                method,
                data,
                files=files,
                sending=True,
                rich_formatting=method == "sendRichMessage",
            )
        except TelegramRichFormattingError:
            assert plain_fallback is not None
            if len(plain_fallback) >= 4_096:
                raise TelegramDeliveryError(
                    "Telegram rejected rich formatting and the plain fallback is too long"
                ) from None
            fallback_data: dict[str, Any] = {
                "chat_id": destination,
                "text": plain_fallback,
                "link_preview_options": {"is_disabled": True},
            }
            if message.reply_to_platform_message_id is not None:
                fallback_data["reply_parameters"] = data["reply_parameters"]
            payload = await self._request("sendMessage", fallback_data, sending=True)
        except TelegramDeliveryError:
            raise
        except TelegramAmbiguousDeliveryError:
            raise
        result = payload.get("result")
        if not isinstance(result, dict):
            raise TelegramAmbiguousDeliveryError(
                "Telegram send response was invalid; delivery status is ambiguous"
            )
        platform_message_id = _platform_id(result.get("message_id"))
        if platform_message_id is None:
            raise TelegramAmbiguousDeliveryError(
                "Telegram send response omitted its receipt; delivery status is ambiguous"
            )
        return DeliveryReceipt(
            transport="telegram",
            account=self.account,
            transport_message_id=message.id,
            platform_message_id=platform_message_id,
            destination_id=message.destination_id,
            delivered_at=self._now(),
        )

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._client.aclose()

    def _normalize(
        self,
        raw: dict[str, Any],
        update_id: str,
        received_at: datetime,
    ) -> ReceivedUpdate:
        message = raw.get("message")
        reason: str | None = None
        sender_id: str | None = None
        destination_id: str | None = None
        platform_message_id: str | None = None
        thread_id: str | None = None
        reply_id: str | None = None
        text: str | None = None
        if not isinstance(message, dict):
            reason = "unsupported update type"
        else:
            sender = message.get("from")
            chat = message.get("chat")
            sender_id = _platform_id(sender.get("id")) if isinstance(sender, dict) else None
            destination_id = _platform_id(chat.get("id")) if isinstance(chat, dict) else None
            platform_message_id = _platform_id(message.get("message_id"))
            thread_present = "message_thread_id" in message
            thread_id = _platform_id(message.get("message_thread_id"))
            reply_present = "reply_to_message" in message
            reply = message.get("reply_to_message")
            if isinstance(reply, dict):
                reply_id = _platform_id(reply.get("message_id"))
            candidate_text = message.get("text")
            text = candidate_text if isinstance(candidate_text, str) else None
            if (
                sender_id is None
                or destination_id is None
                or platform_message_id is None
                or (thread_present and thread_id is None)
                or (reply_present and (not isinstance(reply, dict) or reply_id is None))
            ):
                reason = "malformed platform identifiers"
            elif sender_id not in self.settings.allowed_sender_ids:
                reason = "sender is not authorized"
            elif destination_id not in self.settings.allowed_destination_ids:
                reason = "destination is not authorized"
            elif text is None:
                reason = "unsupported message content"
            elif not text.strip():
                reason = "message text is empty"
            elif len(text) > self.settings.max_inbound_text_length:
                reason = "message text exceeds the configured limit"
        rejected = reason is not None
        normalized = InboundMessage(
            id=_inbound_id(self.account, update_id),
            transport="telegram",
            account=self.account,
            update_id=update_id,
            destination_id=destination_id or "unknown",
            thread_id=thread_id,
            sender_id=sender_id or "unknown",
            platform_message_id=platform_message_id or "unknown",
            reply_to_platform_message_id=reply_id,
            text="[rejected update]" if rejected else (text or "").strip(),
            received_at=received_at,
            status="rejected" if rejected else "pending",
        )
        return ReceivedUpdate(
            update_id=update_id,
            message=normalized,
            rejection_reason=reason,
        )

    async def _request(
        self,
        method: str,
        data: dict[str, Any],
        *,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        sending: bool = False,
        rich_formatting: bool = False,
    ) -> dict[str, Any]:
        try:
            if files is None:
                response = await self._client.post(f"{self._base_url}/{method}", json=data)
            else:
                response = await self._client.post(
                    f"{self._base_url}/{method}",
                    data=data,
                    files=files,
                )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            error = "Telegram could not be reached before the request was sent"
            if sending:
                raise TelegramDeliveryError(error) from None
            raise TelegramTransportError(error) from None
        except httpx.RequestError:
            error = "Telegram request ended without a reliable response"
            if sending:
                raise TelegramAmbiguousDeliveryError(
                    f"{error}; delivery status is ambiguous"
                ) from None
            raise TelegramTransportError(error) from None
        try:
            payload = response.json()
        except ValueError:
            if sending:
                raise TelegramAmbiguousDeliveryError(
                    "Telegram returned an unreadable send response; delivery status is ambiguous"
                ) from None
            raise TelegramTransportError("Telegram returned an unreadable response") from None
        if response.status_code == 409 or (
            isinstance(payload, dict) and payload.get("error_code") == 409
        ):
            raise TelegramConflictError(
                "Telegram rejected getUpdates because another poller is active for this bot"
            )
        if sending and response.status_code >= 500:
            raise TelegramAmbiguousDeliveryError(
                "Telegram failed after accepting the request; delivery status is ambiguous"
            )
        if response.is_error or not isinstance(payload, dict) or payload.get("ok") is not True:
            if sending:
                if rich_formatting and _is_rich_format_rejection(response, payload):
                    raise TelegramRichFormattingError("Telegram rejected rich message formatting")
                raise TelegramDeliveryError("Telegram rejected the outbound message")
            raise TelegramTransportError("Telegram rejected the request")
        return payload

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise TelegramTransportError("Telegram transport clock must return aware UTC")
        return value


def split_telegram_text(
    text: str,
    limit: int = TELEGRAM_TEXT_LIMIT,
    *,
    text_format: MessageTextFormat = "plain_text",
) -> list[str]:
    """Split text into deterministic, independently valid Telegram parts."""

    if limit < 1 or limit >= 4_096:
        raise ValueError("Telegram split limit must be between 1 and 4095")
    if not text:
        raise ValueError("Telegram message text cannot be empty")
    return split_message_text(text, text_format=text_format, limit=limit)


def _is_rich_format_rejection(response: httpx.Response, payload: object) -> bool:
    """Recognize only definitive rich-parser or unsupported-method rejections."""

    if response.status_code == 404:
        return isinstance(payload, dict)
    if response.status_code != 400 or not isinstance(payload, dict):
        return False
    description = payload.get("description")
    if not isinstance(description, str):
        return False
    normalized = description.casefold()
    return any(token in normalized for token in ("parse", "markdown", "rich", "method"))


def _platform_id(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.removeprefix("-").isdigit():
        return value
    return None


def _inbound_id(account: str, update_id: str) -> str:
    digest = hashlib.sha256(f"telegram\0{account}\0{update_id}".encode()).hexdigest()[:32]
    return f"inbound_{digest}"
