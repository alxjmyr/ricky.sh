"""Gateway high-cardinality, route-pinning, and composition boundaries."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gateway_ops_support import (
    ACCOUNT,
    PROFILE_SCOPE,
    make_conversation,
    store_inbound,
)
from gateway_ops_support import inbound as build_inbound
from gateway_ops_support import (
    settings as gateway_settings,
)
from ricky.capabilities.policy import policy_digest
from ricky.executions import store as execution_store_module
from ricky.executions.dispatcher import ExecutionDispatcher
from ricky.executions.store import ExecutionStore
from ricky.executions.types import ExecutionRequest
from ricky.gateway.conversations import ConversationCoordinator, GatewayConversationError
from ricky.gateway.retention import GatewayRetention
from ricky.gateway.store import GatewayStore
from ricky.gateway.types import Conversation, ConversationKey
from ricky.llm import (
    CompletionRequest,
    Message,
    MessageDone,
    StreamEvent,
    TextPart,
    ToolCallPart,
)
from ricky.messaging.store import MessagingStore
from ricky.messaging.types import ReceiveBatch, ReceivedUpdate, TransportCursor
from ricky.notifications import NotificationService
from ricky.notifications import store as notification_store_module
from ricky.notifications.store import NotificationStore
from ricky.notifications.types import CorrelationRef, NotificationRequest
from ricky.project_scope import ProjectScope
from ricky.sessions.store import SessionStore

NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


class _ScriptedProvider:
    name = "scripted"

    def __init__(self, *, try_project_job: bool = False) -> None:
        self.try_project_job = try_project_job
        self.requests: list[CompletionRequest] = []

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        if self.try_project_job and len(self.requests) == 1:
            yield MessageDone(
                message=Message(
                    role="assistant",
                    content=[
                        ToolCallPart(
                            id="call_project_job",
                            name="start_named_job",
                            args={"name": "checkout-only"},
                        )
                    ],
                ),
                stop_reason="tool_calls",
            )
            return
        yield MessageDone(message=Message.text("assistant", "done"), stop_reason="stop")

    async def aclose(self) -> None:
        return None


def _named_request(
    index: int,
    *,
    created_at: datetime,
    conversation_id: str | None = None,
    message_id: str | None = None,
) -> ExecutionRequest:
    return ExecutionRequest(
        id=f"execution_{index:032x}",
        kind="named_job",
        status="queued",
        named_job="personal/brief",
        job_digest="a" * 64,
        profile_scope=PROFILE_SCOPE,
        source_conversation_id=conversation_id,
        source_message_id=message_id,
        notification_route="owner",
        request_key=f"backlog:{index}",
        created_at=created_at,
    )


def _notification(index: int, *, created_at: datetime, source_id: str) -> NotificationRequest:
    return NotificationRequest(
        id=f"notification_{index:032x}",
        route="owner",
        body=f"notification {index}",
        source_kind="execution",
        profile_label=PROFILE_SCOPE.label(),
        source_id=source_id,
        dedupe_key="result:cancelled",
        correlations=[],
        created_at=created_at,
    )


def _seed_execution_requests(
    store: ExecutionStore,
    requests: list[ExecutionRequest],
) -> None:
    """Insert already-validated execution fixtures in one test-only transaction."""

    connection = store._connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany(
            f"INSERT INTO execution_requests "
            f"({','.join(execution_store_module._COLUMNS)}) "
            f"VALUES ({','.join('?' for _ in execution_store_module._COLUMNS)})",
            [execution_store_module._values(request) for request in requests],
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _seed_notifications(
    store: NotificationStore,
    requests: list[NotificationRequest],
) -> list[str]:
    """Insert canonical notification/outbox fixture rows in one transaction."""

    outbox_ids = [f"outbox_{index:032x}" for index in range(1, len(requests) + 1)]
    notification_rows = [
        (
            request.id,
            notification_store_module.SCHEMA_VERSION,
            request.model_dump_json(),
            request.source_kind,
            request.source_id,
            request.dedupe_key,
            request.route,
            request.created_at.astimezone(UTC).isoformat(),
        )
        for request in requests
    ]
    outbox_rows = [
        (
            outbox_id,
            request.id,
            request.route,
            request.source_kind,
            request.source_id,
            request.dedupe_key,
            request.created_at.astimezone(UTC).isoformat(),
            request.created_at.astimezone(UTC).isoformat(),
        )
        for outbox_id, request in zip(outbox_ids, requests, strict=True)
    ]
    with store._connect() as connection, store._transaction(connection):
        connection.executemany(
            """
            INSERT INTO notifications(
                id, schema_version, request_json, source_kind, source_id,
                dedupe_key, route, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            notification_rows,
        )
        connection.executemany(
            """
            INSERT INTO outbox(
                id, notification_id, route, source_kind, source_id, dedupe_key,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            outbox_rows,
        )
    return outbox_ids


@pytest.mark.asyncio
async def test_outbox_selects_oldest_before_limit_under_continuous_new_arrivals(
    tmp_path: Path,
) -> None:
    config = gateway_settings(tmp_path)
    store = NotificationStore(config)
    await store.initialize()
    expected: list[str] = []
    for index in range(5):
        record = await store.enqueue(
            _notification(
                index + 1, created_at=NOW + timedelta(seconds=index), source_id=str(index)
            ),
            scope=PROFILE_SCOPE,
        )
        expected.append(record.outbox.id)

    for round_index in range(3):
        [oldest] = await store.list_pending_oldest(scope=PROFILE_SCOPE, limit=1)
        assert oldest.outbox.id == expected[round_index]
        await store.cancel(oldest.outbox.id, scope=PROFILE_SCOPE)
        await store.enqueue(
            _notification(
                100 + round_index,
                created_at=NOW + timedelta(hours=1, seconds=round_index),
                source_id=f"new-{round_index}",
            ),
            scope=PROFILE_SCOPE,
        )

    assert [
        item.outbox.id for item in await store.list_pending_oldest(scope=PROFILE_SCOPE, limit=2)
    ] == expected[3:]


@pytest.mark.parametrize(
    "drift",
    ["provider", "model", "profile_scope", "project_root", "policy", "removed_route"],
)
def test_every_pinned_route_boundary_fails_snapshot_validation(
    tmp_path: Path,
    drift: str,
) -> None:
    config = gateway_settings(tmp_path)
    route = config.gateway.routes["owner"]
    route_settings = config.resolve_profile_runtime_settings(route.profile_scope())
    conversation = Conversation(
        id="conversation_" + "a" * 32,
        key=ConversationKey(
            transport="telegram",
            account=ACCOUNT,
            destination_id="200",
        ),
        session_id="session_" + "b" * 32,
        route_name="owner",
        provider=route.provider,
        model=route.model,
        profile_scope=route.profile_scope(),
        project_root=None,
        route_policy_digest=policy_digest(
            route_settings.agents.gateway_foreground,
            route,
        ),
        status="active",
        revision=0,
        created_at=NOW,
        updated_at=NOW,
    )

    if drift == "provider":
        route.provider = "anthropic"
    elif drift == "model":
        route.model = "changed-model"
    elif drift == "profile_scope":
        route.primary_profile = "work"
    elif drift == "project_root":
        route.project_root = str(tmp_path / "another-project")
    elif drift == "policy":
        route.exclude_capabilities.append("builtin.project.read")
    else:
        del config.gateway.routes["owner"]

    with pytest.raises(GatewayConversationError, match="send /new"):
        ConversationCoordinator(config)._validate_route_snapshot(conversation)


@pytest.mark.asyncio
async def test_projectless_foreground_builds_one_runtime_and_cannot_discover_checkout_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ricky.gateway import conversations

    checkout = tmp_path / "checkout"
    bundle = checkout / ".ricky" / "jobs" / "checkout-only"
    bundle.mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("[project]\nname='checkout'\n", encoding="utf-8")
    (bundle / "job.toml").write_text(
        """version = 3
name = "checkout-only"
description = "Must not leak into a projectless route."
provider = "openrouter"
model = "test-model"
goal = "Do not run."
[budget]
wall_clock_seconds = 10
iterations = 1
max_completion_tokens_per_request = 100
effect_calls = 0
[tools]
allow = []
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(checkout)
    config = gateway_settings(tmp_path)
    provider = _ScriptedProvider(try_project_job=True)
    provider_builds = 0
    runtime_builds = 0
    original = conversations.build_capability_runtime

    def provider_factory(_name: str, _settings: Any) -> _ScriptedProvider:
        nonlocal provider_builds
        provider_builds += 1
        return provider

    @asynccontextmanager
    async def counted_runtime(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        nonlocal runtime_builds
        runtime_builds += 1
        async with original(*args, **kwargs) as runtime:
            yield runtime

    monkeypatch.setattr(conversations, "build_capability_runtime", counted_runtime)
    messaging = MessagingStore(config)
    await messaging.initialize()
    message = await store_inbound(
        messaging,
        build_inbound(update_id="projectless"),
    )
    result = await ConversationCoordinator(
        config,
        provider_factory=provider_factory,
    ).process(message.id)

    request_text = "\n".join(
        part.text
        for request in provider.requests
        for message in request.messages
        for part in message.content
        if isinstance(part, TextPart)
    )
    stored = await SessionStore(config).get(result.session_id, scope=PROFILE_SCOPE)
    tool_error = next(
        part.content
        for message in stored.session.history
        for part in message.content
        if part.kind == "tool_result"
    )

    assert runtime_builds == 1
    assert provider_builds == 1
    assert '"name": "checkout-only"' not in request_text
    assert "no job bundle named 'checkout-only'" in tool_error
    assert await ExecutionStore(config).list(scope=PROFILE_SCOPE, limit=10) == []


@pytest.mark.asyncio
async def test_terminal_projection_is_set_difference_and_cancelled_dedupe_is_terminal(
    tmp_path: Path,
) -> None:
    config = gateway_settings(tmp_path)
    executions = ExecutionStore(config)
    notifications = NotificationStore(config)
    await executions.initialize()
    await notifications.initialize()
    history_size = 1_002
    terminal_requests = [
        ExecutionRequest.model_validate(
            _named_request(
                index + 1,
                created_at=NOW + timedelta(microseconds=index),
            ).model_dump()
            | {"status": "cancelled", "error": "cancelled by user"}
        )
        for index in range(history_size)
    ]
    _seed_execution_requests(executions, terminal_requests[:-1])
    final_request = _named_request(
        history_size,
        created_at=NOW + timedelta(microseconds=history_size - 1),
    )
    await executions.submit(final_request, scope=PROFILE_SCOPE)
    assert (await executions.cancel(final_request.id, scope=PROFILE_SCOPE)).status == "cancelled"
    notification_requests = [
        _notification(
            index + 1,
            created_at=NOW + timedelta(microseconds=index),
            source_id=terminal_requests[index].id,
        )
        for index in range(history_size - 1)
    ]
    cancelled_outbox_id = _seed_notifications(notifications, notification_requests)[0]

    await notifications.cancel(cancelled_outbox_id, scope=PROFILE_SCOPE)
    dispatcher = ExecutionDispatcher(
        config,
        project_scope=ProjectScope.disabled(),
        store=executions,
        notifications=NotificationService(config, store=notifications),
    )

    assert await dispatcher.project_notifications(scope=PROFILE_SCOPE) == 1
    assert await dispatcher.project_notifications(scope=PROFILE_SCOPE) == 0
    assert (
        await notifications.get_outbox(cancelled_outbox_id, scope=PROFILE_SCOPE)
    ).status == "cancelled"
    assert (
        len(await notifications.source_ids(source_kind="execution", scope=PROFILE_SCOPE))
        == history_size
    )
    assert len(await notifications.unresolved_outbox_ids(scope=PROFILE_SCOPE)) == history_size - 1


@pytest.mark.asyncio
async def test_conversation_and_source_filters_apply_before_execution_limits(
    tmp_path: Path,
) -> None:
    config = gateway_settings(tmp_path)
    gateway = GatewayStore(config)
    sessions = SessionStore(config)
    messaging = MessagingStore(config)
    executions = ExecutionStore(config)
    await gateway.initialize()
    await sessions.initialize()
    await messaging.initialize()
    await executions.initialize()
    conversation_id, _ = await make_conversation(gateway, sessions)
    conversation = await gateway.get(conversation_id, scope=PROFILE_SCOPE)
    inbound = await store_inbound(messaging, build_inbound(update_id="target"))
    target = _named_request(
        1,
        created_at=NOW,
        conversation_id=conversation.id,
        message_id=inbound.id,
    )
    await executions.submit(target, scope=PROFILE_SCOPE)
    _seed_execution_requests(
        executions,
        [
            _named_request(
                index,
                created_at=NOW + timedelta(seconds=index),
                conversation_id=f"conversation_{index:032x}",
                message_id=f"inbound_{index:032x}",
            )
            for index in range(2, 125)
        ],
    )

    coordinator = ConversationCoordinator(config, gateway=gateway, sessions=sessions)
    await coordinator.initialize()
    status = await coordinator._status(conversation)
    outbox_id = await coordinator._enqueue_response(inbound, conversation, "queued")
    response = await NotificationStore(config).get_by_outbox(outbox_id, scope=PROFILE_SCOPE)

    assert f"execution {target.id} queued" in status
    assert f"request {target.id} (queued)" in response.request.body
    assert any(
        item.kind == "execution_request" and item.id == target.id
        for item in response.request.correlations
    )


@pytest.mark.parametrize("outbox_status", ["pending", "claimed", "failed", "in_doubt"])
@pytest.mark.asyncio
async def test_unresolved_notification_protects_archived_conversation_until_resolution(
    tmp_path: Path,
    outbox_status: str,
) -> None:
    from ricky.config import GatewayRetentionSettings

    config = gateway_settings(
        tmp_path,
        retention=GatewayRetentionSettings(
            enabled=True,
            min_age_seconds=0,
            archived_conversations=0,
        ),
    )
    gateway = GatewayStore(config, clock=lambda: NOW)
    sessions = SessionStore(config)
    notifications = NotificationStore(config)
    await gateway.initialize()
    await sessions.initialize()
    await notifications.initialize()
    conversation_id, _ = await make_conversation(gateway, sessions)
    conversation = await gateway.get(conversation_id, scope=PROFILE_SCOPE)
    await gateway.archive(
        conversation.id,
        expected_revision=conversation.revision,
        scope=PROFILE_SCOPE,
    )
    record = await notifications.enqueue(
        NotificationRequest(
            id="notification_" + "f" * 32,
            route=f"conversation:{conversation.id}",
            body="unresolved",
            source_kind="test",
            profile_label=PROFILE_SCOPE.label(),
            source_id="source",
            dedupe_key="one",
            correlations=[
                CorrelationRef(
                    kind="conversation",
                    id=conversation.id,
                    profile_label=PROFILE_SCOPE.label(),
                )
            ],
            created_at=NOW,
        ),
        scope=PROFILE_SCOPE,
    )
    claimed = None
    if outbox_status in {"claimed", "in_doubt"}:
        claimed = await notifications.claim(
            record.outbox.id,
            scope=PROFILE_SCOPE,
            worker="worker",
            transport="telegram",
            destination_ref="200",
        )
    if outbox_status == "failed":
        await notifications.fail_pending(
            record.outbox.id, scope=PROFILE_SCOPE, error="pre-send failure"
        )
    elif outbox_status == "in_doubt":
        assert claimed is not None
        await notifications.mark_in_doubt(claimed, scope=PROFILE_SCOPE, error="ambiguous send")

    retention = GatewayRetention(
        config,
        scope=PROFILE_SCOPE,
        notifications=notifications,
        gateway=gateway,
    )
    protected = await retention.plan(now=NOW + timedelta(days=1))
    group = protected.group("archived_conversations")
    assert group is not None
    assert conversation.id in group.protected_ids
    assert conversation.id not in group.removable_ids

    if outbox_status == "claimed":
        assert claimed is not None
        await notifications.release(claimed, scope=PROFILE_SCOPE)
        await notifications.cancel(record.outbox.id, scope=PROFILE_SCOPE)
    elif outbox_status == "in_doubt":
        await notifications.resolve(
            record.outbox.id,
            scope=PROFILE_SCOPE,
            disposition="delivered",
            actor="operator",
            note="confirmed",
        )
    else:
        await notifications.cancel(record.outbox.id, scope=PROFILE_SCOPE)

    disposable = await retention.plan(now=NOW + timedelta(days=1))
    resolved_group = disposable.group("archived_conversations")
    assert resolved_group is not None
    assert conversation.id in resolved_group.removable_ids


@pytest.mark.asyncio
async def test_protection_queries_return_dependencies_beyond_former_thousand_row_caps(
    tmp_path: Path,
) -> None:
    config = gateway_settings(tmp_path)
    messaging = MessagingStore(config)
    executions = ExecutionStore(config)
    await messaging.initialize()
    await executions.initialize()
    updates = []
    execution_message_ids: list[str] = []
    requests: list[ExecutionRequest] = []
    for index in range(1_001):
        message = build_inbound(update_id=f"bulk-{index}")
        updates.append(ReceivedUpdate(update_id=message.update_id, message=message))
        execution_message_ids.append(f"inbound_{index + 10_000:032x}")
        requests.append(
            _named_request(
                index + 10_000,
                created_at=NOW + timedelta(microseconds=index),
                message_id=execution_message_ids[-1],
            )
        )
    _seed_execution_requests(executions, requests)
    await messaging.ingest(
        ReceiveBatch(
            transport="telegram",
            account=ACCOUNT,
            updates=updates,
            next_cursor=TransportCursor(
                transport="telegram",
                account=ACCOUNT,
                value="bulk-1000",
            ),
        )
    )

    inbox_ids = await messaging.protected_inbox_ids()
    protected_execution_messages = await executions.protected_message_ids(scope=PROFILE_SCOPE)

    assert len(inbox_ids) == 1_001
    assert len(protected_execution_messages) == 1_001
    assert execution_message_ids[0] in protected_execution_messages
    assert execution_message_ids[-1] in protected_execution_messages
