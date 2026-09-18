"""Coordinate committed foreground handoffs with confirmed message delivery."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

from ricky.agent.handoff import BackgroundHandoff
from ricky.config import RickySettings
from ricky.executions.store import ExecutionStore
from ricky.executions.types import ExecutionRequest
from ricky.gateway.handoff_text import render_handoff_acknowledgement
from ricky.gateway.store import GatewayStore
from ricky.gateway.types import Conversation
from ricky.messaging.store import MessagingStore
from ricky.messaging.types import InboundMessage
from ricky.notifications.store import NotificationStore
from ricky.notifications.types import CorrelationRef, NotificationRequest
from ricky.profiles import ProfileScope
from ricky.sessions.store import SessionStore

_LOG = logging.getLogger(__name__)


def _render_acknowledgement(
    inbound: InboundMessage,
    handoffs: Sequence[BackgroundHandoff],
    acknowledgement: str,
    max_chars: int,
) -> str:
    notice = (
        "Images were resized to fit the configured limits.\n\n" if inbound.images_resized else ""
    )
    return notice + render_handoff_acknowledgement(
        handoffs,
        acknowledgement,
        max_chars - len(notice),
    )


async def enqueue_foreground_response(
    settings: RickySettings,
    *,
    notifications: NotificationStore,
    executions: ExecutionStore,
    inbound: InboundMessage,
    conversation: Conversation,
    body: str,
    handoffs: Sequence[BackgroundHandoff] = (),
) -> str:
    """Enqueue once per source message, retaining correlations outside the prose."""
    body = (
        _render_acknowledgement(inbound, handoffs, body, settings.messaging.body_char_limit)
        if handoffs
        else body[: settings.messaging.body_char_limit].strip()
    )
    scope = conversation.profile_scope
    linked = await executions.list_by_source_message(inbound.id, scope=scope, limit=1_000)
    correlations = [
        CorrelationRef(
            kind="conversation",
            id=conversation.id,
            revision=conversation.revision,
            profile_label=scope.label(),
        )
    ]
    correlations.extend(
        CorrelationRef(kind="execution_request", id=request.id, profile_label=scope.label())
        for request in linked
    )
    correlations.extend(
        CorrelationRef(
            kind="task",
            id=request.task_id,
            revision=request.task_revision,
            profile_label=scope.label(),
        )
        for request in linked
        if request.task_id is not None
    )
    # The committed turn is the complete handoff manifest. Notification links
    # are bounded convenience references, not execution authority.
    correlations = correlations[:20]
    record = await notifications.enqueue(
        NotificationRequest(
            id=f"notification_{uuid4().hex}",
            route=f"inbox:{inbound.id}",
            body=body,
            body_format="portable_markdown_v1",
            urgency="normal",
            source_kind="gateway_turn",
            source_id=inbound.id,
            dedupe_key=inbound.id,
            profile_label=scope.label(),
            correlations=correlations,
            created_at=datetime.now(UTC),
        ),
        scope=scope,
    )
    # The result may already exist after a crash. Never attach a different message
    # returned under the same dedupe key as evidence of a successful handoff.
    if record.request.body == body:
        for request in linked:
            if any(item.request_id == request.id for item in handoffs):
                await executions.attach_acknowledgement(request.id, record.outbox.id, scope=scope)
    return record.outbox.id


async def reconcile_handoffs(
    settings: RickySettings,
    *,
    gateway: GatewayStore,
    messaging: MessagingStore,
    notifications: NotificationStore,
    sessions: SessionStore,
    executions: ExecutionStore,
    scope: ProfileScope,
    now: datetime | None = None,
) -> int:
    """Repair crash gaps without replaying a model or guessing transport success."""
    moment = now or datetime.now(UTC)
    pending = await executions.list_pending_acknowledgements(scope=scope)
    stale = {item.message.id for item in await messaging.stale_inbox_claims(now=moment)}
    released = 0
    for request in pending:
        try:
            released += await _reconcile_handoff(
                settings,
                request=request,
                gateway=gateway,
                messaging=messaging,
                notifications=notifications,
                sessions=sessions,
                executions=executions,
                stale=stale,
                moment=moment,
            )
        except Exception as exc:  # noqa: BLE001 - one broken dependency must not starve other work
            _LOG.warning("Background handoff %s remains held: %s", request.id, type(exc).__name__)
    return released


async def _reconcile_handoff(
    settings: RickySettings,
    *,
    request: ExecutionRequest,
    gateway: GatewayStore,
    messaging: MessagingStore,
    notifications: NotificationStore,
    sessions: SessionStore,
    executions: ExecutionStore,
    stale: set[str],
    moment: datetime,
) -> bool:
    if request.source_message_id is None or request.source_conversation_id is None:
        return False
    result = await gateway.get_result(request.source_message_id, scope=request.profile_scope)
    if result is None or result.conversation_id != request.source_conversation_id:
        return False
    if result.status not in {"running", "uncertain", "committed"}:
        return False
    conversation = await gateway.get(result.conversation_id, scope=request.profile_scope)
    if result.session_id != conversation.session_id:
        return False
    committed = await sessions.turn_for_inbound(
        result.session_id,
        result.message_id,
        scope=request.profile_scope,
    )
    if (
        committed is None
        or committed.status != "committed"
        or committed.handoff_acknowledgement is None
        or not any(item.request_id == request.id for item in committed.background_handoffs)
    ):
        return False
    inbound = await messaging.get_inbox(result.message_id)
    if result.status != "committed":
        # A live foreground owner is still finishing its own commit. Its
        # normal path must win; recovery cannot steal or advance that turn.
        if inbound.status == "claimed" and inbound.id not in stale:
            return False
        outbox_id = await enqueue_foreground_response(
            settings,
            notifications=notifications,
            executions=executions,
            inbound=inbound,
            conversation=conversation,
            body=committed.handoff_acknowledgement,
            handoffs=committed.background_handoffs,
        )
        result = await gateway.reconcile_committed_result(
            message_id=result.message_id,
            scope=request.profile_scope,
            session_revision=committed.base_revision + 1,
            response_outbox_id=outbox_id,
        )
        await messaging.settle_inbox_from_terminal_result(
            result.message_id,
            status="processed",
            now=moment,
        )
    if result.session_revision != committed.base_revision + 1 or result.response_outbox_id is None:
        return False
    record = await notifications.get_by_outbox(
        result.response_outbox_id, scope=request.profile_scope
    )
    # Existing acknowledgements can have been rendered under an earlier body
    # limit. Accept only a complete canonical or compact rendering, regardless
    # of later configuration changes.
    body = _render_acknowledgement(
        inbound,
        committed.background_handoffs,
        committed.handoff_acknowledgement,
        len(record.request.body),
    )
    if (
        record.request.source_kind != "gateway_turn"
        or record.request.source_id != result.message_id
        or record.request.route != f"inbox:{result.message_id}"
        or record.request.body != body
    ):
        return False
    await executions.attach_acknowledgement(
        request.id, record.outbox.id, scope=request.profile_scope
    )
    if record.outbox.status != "delivered":
        return False
    parts = [
        part
        for part in await messaging.delivery_parts(record.outbox.id)
        if part.fence == record.outbox.fence
    ]
    if not parts or any(
        part.status != "delivered" or not part.platform_message_id for part in parts
    ):
        return False
    expected_count = parts[0].message.part_count
    if (
        len(parts) != expected_count
        or {part.message.part_number for part in parts} != set(range(1, expected_count + 1))
        or any(
            part.message.part_count != expected_count
            or part.message.outbox_id != record.outbox.id
            or part.message.transport != conversation.key.transport
            or part.message.account != conversation.key.account
            or part.message.destination_id != conversation.key.destination_id
            for part in parts
        )
    ):
        return False
    updated = await executions.release_acknowledged(
        request.id,
        record.outbox.id,
        scope=request.profile_scope,
        now=moment,
    )
    return updated.status == "queued" and request.status == "awaiting_acknowledgement"
