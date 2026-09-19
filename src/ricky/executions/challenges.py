"""Execution challenge notifications and authenticated reply correlation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import SecretStr

from ricky.browser.challenges import (
    ChallengeError,
    ChallengeResponse,
    ChallengeSource,
    LiveBrowserChallenge,
)
from ricky.executions.types import ExecutionRequest
from ricky.messaging.store import MessagingStore
from ricky.messaging.types import InboundMessage
from ricky.notifications.service import NotificationService
from ricky.notifications.store import NotificationNotFoundError
from ricky.notifications.types import NotificationRequest
from ricky.profiles import ProfileScope


@dataclass(frozen=True)
class _Pending:
    owner: LiveBrowserChallenge
    principal_id: str
    conversation_id: str


class ExecutionBrowserChallenges:
    """Resident owners receive replies; durable notifications never replay a code."""

    def __init__(self, notifications: NotificationService, messaging: MessagingStore) -> None:
        self.notifications = notifications
        self.messaging = messaging
        self._pending: dict[str, _Pending] = {}

    async def request(
        self,
        owner: LiveBrowserChallenge,
        execution: ExecutionRequest,
        principal_id: str,
    ) -> None:
        if execution.source_conversation_id is None or execution.status != "running":
            raise ChallengeError("browser verification requires a live gateway-owned execution")
        if execution.profile_scope != owner.record.binding.profile_scope:
            raise ChallengeError("browser verification scope differs from its execution")
        for challenge_id, pending in tuple(self._pending.items()):
            if pending.owner.record.state != "waiting_for_user":
                self._pending.pop(challenge_id, None)
        record = owner.record
        action = (
            "the requested action on your device/browser"
            if record.kind == "manual"
            else "a one-time verification code"
        )
        reply_instruction = (
            "Reply directly with done after completing it."
            if record.kind == "manual"
            else "Reply directly to this message with the code."
        )
        self._pending[record.id] = _Pending(owner, principal_id, execution.source_conversation_id)
        try:
            await self.notifications.enqueue(
                NotificationRequest(
                    id=f"notification_{uuid4().hex}",
                    route=execution.notification_route,
                    title="Browser verification needed",
                    body=(
                        f"{record.binding.top_level_origin} needs {action}.\n"
                        f"{record.instruction}\n\n"
                        + (f"{owner.assistance_reason}\n\n" if owner.assistance_reason else "")
                        + f"Your browser is paused. {reply_instruction} "
                        "Any transaction approval remains separate.\n"
                        f"Expires: {record.expires_at.isoformat()}\n"
                        f"Cancel: /cancel {execution.id}"
                    ),
                    body_format="plain_text",
                    urgency="attention",
                    source_kind="browser_challenge",
                    source_id=record.id,
                    dedupe_key=f"challenge:{record.id}",
                    profile_label=execution.profile_scope.label(),
                    created_at=datetime.now(UTC),
                    expires_at=record.expires_at,
                ),
                scope=execution.profile_scope,
            )
        except BaseException:
            self._pending.pop(record.id, None)
            raise

    async def respond(
        self,
        inbound: InboundMessage,
        *,
        conversation_id: str,
        scope: ProfileScope,
    ) -> str | None:
        reply_id = inbound.reply_to_platform_message_id
        if reply_id is None:
            return None
        part = await self.messaging.find_delivery_part(
            transport=inbound.transport,
            account=inbound.account,
            destination_id=inbound.destination_id,
            platform_message_id=reply_id,
        )
        if part is None and any(
            pending.conversation_id == conversation_id
            and pending.owner.record.state == "waiting_for_user"
            for pending in self._pending.values()
        ):
            # A fast reply can arrive between transport send and receipt commit.
            # Wait boundedly for trusted correlation; never guess from code text.
            for _ in range(20):
                await asyncio.sleep(0.05)
                part = await self.messaging.find_delivery_part(
                    transport=inbound.transport,
                    account=inbound.account,
                    destination_id=inbound.destination_id,
                    platform_message_id=reply_id,
                )
                if part is not None:
                    break
            if part is None:
                return (
                    "I could not confirm the verification request for this reply. "
                    "No code was submitted; reply directly to the verification message again."
                )
        if part is None:
            return None
        try:
            notification = await self.notifications.store.get_by_outbox(part.outbox_id, scope=scope)
        except NotificationNotFoundError:
            return None
        if notification.request.source_kind != "browser_challenge":
            return None
        if inbound.transport == "telegram" and inbound.destination_id.startswith("-"):
            return "Browser verification replies require the authorized private chat."
        pending = self._pending.get(notification.request.source_id)
        if pending is None:
            return (
                "This browser verification is no longer waiting for a code. No code was submitted."
            )
        principal = f"{inbound.transport}:{inbound.account}:{inbound.sender_id}"
        if principal != pending.principal_id or conversation_id != pending.conversation_id:
            return "This verification reply does not belong to this browser request."
        if inbound.images or not inbound.text.strip() or len(inbound.text.strip()) > 256:
            return "Reply directly to the verification request with the code only."
        owner = pending.owner
        if owner.record.kind == "manual" and inbound.text.strip().lower() != "done":
            return "Complete the requested action, then reply directly with done."
        source = ChallengeSource(
            principal_id=principal,
            conversation_id=conversation_id,
            prompt_message_id=reply_id,
        )
        try:
            if owner.record.source is None:
                await owner.bind_source(source)
            await owner.respond(
                ChallengeResponse(
                    code=SecretStr(inbound.text.strip()) if owner.record.kind == "otp" else None
                ),
                source=source,
            )
        except ChallengeError as exc:
            return str(exc)
        return (
            "Verification code received. The browser task will continue."
            if owner.record.kind == "otp"
            else "Response received. Ricky will check the browser before continuing."
        )
