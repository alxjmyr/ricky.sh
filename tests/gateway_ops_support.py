"""Shared offline fixtures for the gateway operations suite.

Every helper here builds real durable records through the real stores. No test
in this suite touches a network, a model provider, or a live service manager.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from pydantic import SecretStr

from ricky.agent.session import AgentSession
from ricky.config import (
    ExecutionSettings,
    GatewayRetentionSettings,
    GatewayRouteSettings,
    GatewayServiceSettings,
    GatewaySettings,
    MessagingRouteSettings,
    MessagingSettings,
    MessagingTransportSettings,
    RickySettings,
    TelegramAccountSettings,
)
from ricky.executions.store import ExecutionStore
from ricky.executions.types import ExecutionRequest
from ricky.gateway.service_unit import CommandResult
from ricky.gateway.store import GatewayStore
from ricky.gateway.types import ConversationKey
from ricky.jobs.store import JobRunStore
from ricky.jobs.types import JobRun
from ricky.messaging.store import MessagingStore
from ricky.messaging.types import (
    InboundMessage,
    ReceiveBatch,
    ReceivedUpdate,
    TransportCursor,
    TransportMessage,
)
from ricky.notifications.store import NotificationStore
from ricky.notifications.types import NotificationRequest, OutboxEntry
from ricky.profiles import ProfileScope
from ricky.sessions.store import SessionStore
from ricky.tools.base import EffectIdentity

ACCOUNT = "personal/bot"
SENDER = "100"
DESTINATION = "200"
PROFILE_SCOPE = ProfileScope.create("personal")


def settings(
    tmp_path: Path,
    *,
    enabled: bool = True,
    retention: GatewayRetentionSettings | None = None,
    startup_recovery: bool = True,
    log_dir: str = "logs",
) -> RickySettings:
    """Build one fully isolated user data root with a usable owner route."""

    return RickySettings(
        user_data_dir=str(tmp_path / "user"),
        project_data_dir=str(tmp_path / "project"),
        messaging=MessagingSettings(
            telegram_accounts={
                ACCOUNT: TelegramAccountSettings(
                    bot_token=SecretStr("test-token"),
                    allowed_sender_ids=[SENDER],
                    allowed_destination_ids=[DESTINATION],
                )
            },
            transports={
                "telegram-personal": MessagingTransportSettings(type="telegram", account=ACCOUNT)
            },
            routes={
                "owner": MessagingRouteSettings(
                    transport="telegram-personal",
                    destination=DESTINATION,
                    owner_profile="personal",
                    accepted_profiles=["shared", "personal"],
                )
            },
        ),
        gateway=GatewaySettings(
            enabled=enabled,
            concurrency=1,
            inbox_poll_seconds=0.01,
            maintenance_seconds=0.01,
            operator_route="owner",
            startup_recovery=startup_recovery,
            routes={
                "owner": GatewayRouteSettings(
                    provider="openrouter",
                    model="test-model",
                    primary_profile="personal",
                )
            },
            retention=retention or GatewayRetentionSettings(),
            service=GatewayServiceSettings(log_dir=log_dir),
        ),
        executions=ExecutionSettings(claim_seconds=60.0),
    )


def inbound(
    *,
    update_id: str = "1",
    received_at: datetime | None = None,
    reply_to: str | None = None,
) -> InboundMessage:
    """Build one accepted inbound message with a unique canonical id."""

    return InboundMessage(
        id=f"inbound_{uuid4().hex}",
        transport="telegram",
        account=ACCOUNT,
        update_id=update_id,
        destination_id=DESTINATION,
        sender_id=SENDER,
        platform_message_id=f"platform-{update_id}",
        reply_to_platform_message_id=reply_to,
        text="please handle this",
        received_at=received_at or datetime.now(UTC),
        status="pending",
    )


async def store_inbound(
    messaging: MessagingStore,
    message: InboundMessage,
) -> InboundMessage:
    """Persist one accepted inbound message through the real ingest path."""

    batch = ReceiveBatch(
        transport="telegram",
        account=ACCOUNT,
        updates=[ReceivedUpdate(update_id=message.update_id, message=message)],
        next_cursor=TransportCursor(transport="telegram", account=ACCOUNT, value=message.update_id),
    )
    stored = await messaging.ingest(batch)
    return stored[0]


async def make_session(sessions: SessionStore, *, session_id: str | None = None) -> str:
    """Create one active stored session and return its id."""

    identity = session_id or f"session_{uuid4().hex}"
    await sessions.create(
        AgentSession(
            id=identity,
            provider="openrouter",
            model="test-model",
            profile_scope=PROFILE_SCOPE,
        ),
        scope=PROFILE_SCOPE,
    )
    return identity


async def make_conversation(
    gateway: GatewayStore,
    sessions: SessionStore,
    *,
    thread_id: str | None = None,
) -> tuple[str, str]:
    """Create one active conversation bound to a fresh session."""

    session_id = await make_session(sessions)
    conversation = await gateway.create(
        key=ConversationKey(
            transport="telegram",
            account=ACCOUNT,
            destination_id=DESTINATION,
            thread_id=thread_id,
        ),
        session_id=session_id,
        route_name="owner",
        provider="openrouter",
        model="test-model",
        profile_scope=PROFILE_SCOPE,
        project_root=None,
    )
    return conversation.id, session_id


async def enqueue_notification(
    notifications: NotificationStore,
    *,
    route: str = "owner",
    source_id: str = "source-1",
    correlations: list | None = None,
) -> OutboxEntry:
    """Enqueue one pending notification and return its outbox entry."""

    record = await notifications.enqueue(
        NotificationRequest(
            id=f"notification_{uuid4().hex}",
            route=route,
            body="a bounded notification body",
            source_kind="test",
            profile_label=PROFILE_SCOPE.label(),
            source_id=source_id,
            dedupe_key=uuid4().hex,
            correlations=correlations or [],
            created_at=datetime.now(UTC),
        ),
        scope=PROFILE_SCOPE,
    )
    return record.outbox


def transport_message(entry: OutboxEntry) -> TransportMessage:
    """Build the single transport part for one claimed outbox entry."""

    return TransportMessage(
        id=f"transport_message_{uuid4().hex}",
        transport="telegram",
        account=ACCOUNT,
        destination_id=DESTINATION,
        text="a bounded notification body",
        outbox_id=entry.id,
        part_number=1,
        part_count=1,
    )


def execution_request(
    *,
    status: str = "queued",
    task_id: str | None = None,
    created_at: datetime | None = None,
    source_message_id: str | None = None,
    source_conversation_id: str | None = None,
) -> ExecutionRequest:
    """Build one ad hoc execution request in its submitted state."""

    del status  # submit() requires a queued, unclaimed request.
    # An ad hoc request is always bound to one profile-scoped durable task.
    task = task_id or f"task_{uuid4().hex}"
    return ExecutionRequest(
        id=f"execution_{uuid4().hex}",
        kind="ad_hoc",
        status="queued",
        goal="do the bounded background work",
        contract_id=f"contract_{uuid4().hex}",
        contract_digest="a" * 64,
        task_id=task,
        task_revision=1,
        profile_scope=PROFILE_SCOPE,
        source_conversation_id=source_conversation_id,
        source_message_id=source_message_id,
        notification_route="owner",
        request_key=uuid4().hex,
        created_at=created_at or datetime.now(UTC),
    )


async def claimed_execution(
    executions: ExecutionStore,
    *,
    started: bool,
    now: datetime,
    claim_seconds: float = 60.0,
) -> ExecutionRequest:
    """Submit, claim, and optionally start one request, then let its lease expire."""

    request = execution_request()
    await executions.submit(request, scope=PROFILE_SCOPE)
    claimed = await executions.claim(scope=PROFILE_SCOPE, worker_id="worker", limit=1, now=now)
    assert claimed, "the queued request should have been claimable"
    entry = claimed[0]
    if started:
        assert entry.claim_token is not None
        entry = await executions.start(
            entry.id,
            scope=PROFILE_SCOPE,
            token=entry.claim_token,
            fence=entry.claim_fence,
            run_id=f"jobrun_{uuid4().hex}",
        )
    del claim_seconds
    return entry


def job_run(*, run_id: str | None = None, session_id: str = "session_test") -> JobRun:
    """Build one persisted job run record."""

    return JobRun(
        id=run_id or f"jobrun_{uuid4().hex}",
        job_name="delegate",
        spec_digest="a" * 64,
        provider="openrouter",
        model="test-model",
        profile_scope=PROFILE_SCOPE,
        session_id=session_id,
        started_at=datetime.now(UTC),
        runtime_policy_digest="b" * 64,
    )


async def reserve_effect(
    jobs: JobRunStore,
    run: JobRun,
    *,
    action_key: str | None = None,
) -> str:
    """Reserve one external effect and leave it unresolved."""

    identity = EffectIdentity(
        operation="sandbox.reserve",
        target="venue:acme",
        occurrence="task_1@1",
        summary="Reserve a table",
        action_key=action_key or uuid4().hex + uuid4().hex,
    )
    action = await jobs.reserve_action(
        job_name="delegate",
        run_id=run.id,
        identity=identity,
        effect_budget=5,
        scope=PROFILE_SCOPE,
    )
    return action.id


class FakeRunner:
    """Record every supervisor command instead of running one."""

    def __init__(self, *, returncode: int = 0, stdout: str = "active") -> None:
        self.calls: list[tuple[str, ...]] = []
        self.returncode = returncode
        self.stdout = stdout

    def __call__(self, args) -> CommandResult:  # type: ignore[no-untyped-def]
        recorded = tuple(args)
        self.calls.append(recorded)
        return CommandResult(
            args=recorded, returncode=self.returncode, stdout=self.stdout, stderr=""
        )


def past(seconds: float, *, now: datetime | None = None) -> datetime:
    """Return an aware UTC moment this many seconds in the past."""

    return (now or datetime.now(UTC)) - timedelta(seconds=seconds)
