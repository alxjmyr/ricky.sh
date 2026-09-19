"""Foreground conversation persistence, commands, routing, and auth tests."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import SecretStr

from authority_support import install_sandbox_runtime
from ricky.authority.store import AuthorityStore
from ricky.config import (
    BrowserSettings,
    GatewayRouteSettings,
    GatewaySettings,
    MessagingRouteSettings,
    MessagingSettings,
    MessagingTransportSettings,
    ProfileConfigSettings,
    RickySettings,
    TelegramAccountSettings,
    user_data_subpath,
)
from ricky.durable_tasks.scoped import ScopedDurableTaskStore
from ricky.executions.store import ExecutionStore
from ricky.gateway.conversations import ConversationCoordinator, GatewayConversationError
from ricky.gateway.store import GatewayStore
from ricky.gateway.types import ConversationKey
from ricky.llm import (
    CompletionRequest,
    Message,
    MessageDone,
    StreamEvent,
    TextPart,
    ToolCallPart,
)
from ricky.messaging.runtime import MessagingRuntime
from ricky.messaging.store import MessagingStore
from ricky.messaging.types import (
    DeliveryReceipt,
    InboundMessage,
    ReceiveBatch,
    ReceivedUpdate,
    TransportCursor,
    TransportMessage,
)
from ricky.profiles import ProfileScope
from ricky.sessions import SessionStore

_SCOPE = ProfileScope.create("personal")


@pytest.fixture(autouse=True)
def _test_only_guarded_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    install_sandbox_runtime(monkeypatch)


NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)


class HandoffTransport:
    """Record accepted messages while the real messaging runtime persists receipts."""

    def __init__(self) -> None:
        self.sent: list[TransportMessage] = []

    async def receive(self, cursor: TransportCursor | None) -> ReceiveBatch:
        raise AssertionError("handoff test must not poll a transport")

    async def send(self, message: TransportMessage) -> DeliveryReceipt:
        self.sent.append(message)
        return DeliveryReceipt(
            transport=message.transport,
            account=message.account,
            destination_id=message.destination_id,
            transport_message_id=message.id,
            platform_message_id=str(len(self.sent)),
            delivered_at=datetime.now(UTC),
        )

    async def aclose(self) -> None:
        return None


def _handoff_messaging(settings: RickySettings, transport: HandoffTransport) -> MessagingRuntime:
    from ricky.interfaces.messaging.telegram import split_telegram_text
    from ricky.notifications.routes import RoutePolicy

    return MessagingRuntime(
        settings,
        routes=RoutePolicy(settings, conversation_resolver=GatewayStore(settings)),
        transport_factory=lambda _: transport,
        text_splitter=split_telegram_text,
    )


class ScriptedProvider:
    name = "scripted"

    def __init__(self, scripts: list[list[StreamEvent]]) -> None:
        self.scripts = scripts
        self.requests: list[CompletionRequest] = []
        self.closed = False

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        for event in self.scripts.pop(0):
            yield event

    async def aclose(self) -> None:
        self.closed = True


class AdHocProvider:
    name = "adhoc"

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []
        self.step = 0

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        self.step += 1
        if self.step == 1:
            yield _tool(
                "create",
                "create_durable_task",
                {
                    "title": "Research",
                    "objective": "Research the topic",
                    "closure_criteria": "A cited answer is returned",
                    "execution_mode": "agent",
                },
            )
            return
        if self.step == 2:
            text = "\n".join(
                part.content
                for message in request.messages
                for part in message.content
                if part.kind == "tool_result"
            )
            task_id = re.search(r"task_[0-9a-f]{32}", text)
            assert task_id is not None
            yield _tool(
                "queue",
                "delegate_task",
                {
                    "action": "start",
                    "goal": "Research the topic",
                    "requested_capabilities": ["builtin.project.read"],
                    "task_id": task_id.group(),
                    "expected_task_revision": 1,
                },
            )
            return
        yield _answer("Queued the research task and execution.")

    async def aclose(self) -> None:
        return None


class GuardedDelegationProvider:
    name = "openrouter"

    def __init__(self, request_text: str) -> None:
        self.request_text = request_text
        self.requests: list[CompletionRequest] = []

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        step = len(self.requests)
        if step == 1:
            rendered = "\n".join(
                part.text
                for message in request.messages
                for part in message.content
                if part.kind == "text"
            )
            assert '"window_start"' in rendered
            assert '"format": "HH:MM"' in rendered
            yield _tool(
                "create-guarded",
                "create_durable_task",
                {
                    "title": "Sandbox reservation",
                    "objective": "Create the requested sandbox reservation",
                    "closure_criteria": "One bounded sandbox reservation is recorded",
                    "execution_mode": "agent",
                },
            )
            return
        if step == 2:
            rendered = "\n".join(
                part.content
                for message in request.messages
                for part in message.content
                if part.kind == "tool_result"
            )
            task_id = re.search(r"task_[0-9a-f]{32}", rendered)
            assert task_id is not None
            values: dict[str, object] = {
                "venue_id": "venue-a",
                "venue_name": "Restaurant A",
                "party_size": 2,
                "local_date": "2026-08-19",
                "window_start": "18:00",
                "window_end": "20:00",
                "timezone": "America/Chicago",
                "account_identity": "alex@example.com",
                "deposit_limit_minor": 0,
            }
            yield _tool(
                "start-guarded",
                "delegate_task",
                {
                    "action": "start",
                    "task_id": task_id.group(),
                    "expected_task_revision": 1,
                    "goal": "Create one sandbox reservation",
                    "requested_capabilities": ["builtin.sandbox.reservation"],
                    "guardrails": [
                        {
                            "capability_id": "builtin.sandbox.reservation",
                            "fields": [
                                {
                                    "field": field,
                                    "value": value,
                                }
                                for field, value in values.items()
                            ],
                        }
                    ],
                },
            )
            return
        if step == 3:
            rendered = "\n".join(
                part.content
                for message in request.messages
                for part in message.content
                if part.kind == "tool_result"
            )
            match = re.search(r"(draft_[0-9a-f]{32}) at revision (\d+)", rendered)
            assert match is not None
            yield _tool(
                "confirm-guarded",
                "delegate_task",
                {
                    "action": "confirm",
                    "draft_id": match.group(1),
                    "expected_draft_revision": int(match.group(2)),
                },
            )
            return
        if step == 4:
            rendered = "\n".join(
                part.content
                for message in request.messages
                for part in message.content
                if part.kind == "tool_result"
            )
            request_id = re.search(r"execution_[0-9a-f]{32}", rendered)
            assert request_id is not None
            yield _answer(f"Queued {request_id.group()} for the sandbox reservation.")
            return
        raise AssertionError("guarded delegation requested an unexpected model iteration")

    async def aclose(self) -> None:
        return None


class ClarifyingDelegationProvider:
    name = "openrouter"

    def __init__(self, request_text: str) -> None:
        self.request_text = request_text
        self.requests: list[CompletionRequest] = []

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        step = len(self.requests)
        if step == 1:
            yield _tool(
                "create-partial",
                "create_durable_task",
                {
                    "title": "Sandbox reservation",
                    "objective": "Create the requested sandbox reservation",
                    "closure_criteria": "One bounded sandbox reservation is recorded",
                    "execution_mode": "agent",
                },
            )
            return
        rendered = "\n".join(
            part.content
            for message in request.messages
            for part in message.content
            if part.kind == "tool_result"
        )
        if step == 2:
            task_id = re.search(r"task_[0-9a-f]{32}", rendered)
            assert task_id is not None
            values: dict[str, object] = {
                "venue_id": "venue-a",
                "venue_name": "Restaurant A",
                "local_date": "2026-08-19",
                "window_start": "18:00",
                "window_end": "20:00",
                "timezone": "America/Chicago",
                "account_identity": "alex@example.com",
                "deposit_limit_minor": 0,
            }
            yield _tool(
                "start-partial",
                "delegate_task",
                {
                    "action": "start",
                    "task_id": task_id.group(),
                    "expected_task_revision": 1,
                    "goal": "Create one sandbox reservation",
                    "requested_capabilities": ["builtin.sandbox.reservation"],
                    "guardrails": [
                        {
                            "capability_id": "builtin.sandbox.reservation",
                            "fields": [
                                {
                                    "field": field,
                                    "value": value,
                                    "source_quote": self.request_text,
                                }
                                for field, value in values.items()
                            ],
                        }
                    ],
                },
            )
            return
        drafts = re.findall(r"(draft_[0-9a-f]{32}) at revision (\d+)", rendered)
        assert drafts
        draft_id, draft_revision = drafts[-1]
        if step == 3:
            yield _tool(
                "supply-party",
                "delegate_task",
                {
                    "action": "supply_guardrails",
                    "draft_id": draft_id,
                    "expected_draft_revision": int(draft_revision),
                    "guardrails": [
                        {
                            "capability_id": "builtin.sandbox.reservation",
                            "fields": [
                                {
                                    "field": "party_size",
                                    "value": 2,
                                    "source_quote": "for 2 people",
                                }
                            ],
                        }
                    ],
                },
            )
            return
        if step == 4:
            yield _tool(
                "confirm-partial",
                "delegate_task",
                {
                    "action": "confirm",
                    "draft_id": draft_id,
                    "expected_draft_revision": int(draft_revision),
                },
            )
            return
        if step == 5:
            request_id = re.search(r"execution_[0-9a-f]{32}", rendered)
            assert request_id is not None
            yield _answer(f"Queued {request_id.group()} for the sandbox reservation.")
            return
        raise AssertionError("clarifying delegation requested an unexpected model iteration")

    async def aclose(self) -> None:
        return None


class RejectPendingDelegationProvider:
    name = "openrouter"

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        rendered_text = "\n".join(
            part.text
            for message in request.messages
            for part in message.content
            if part.kind == "text"
        )
        rendered_results = "\n".join(
            part.content
            for message in request.messages
            for part in message.content
            if part.kind == "tool_result"
        )
        if len(self.requests) == 1:
            assert "action=cancel" in rendered_text
            drafts = re.findall(r"(draft_[0-9a-f]{32}) at revision (\d+)", rendered_results)
            assert drafts
            draft_id, draft_revision = drafts[-1]
            yield _tool(
                "cancel-pending",
                "delegate_task",
                {
                    "action": "cancel",
                    "draft_id": draft_id,
                    "expected_draft_revision": int(draft_revision),
                },
            )
            return
        assert "No execution contract or background request was created" in rendered_results
        yield _answer("Cancelled the pending background execution request.")

    async def aclose(self) -> None:
        return None


def _answer(text: str) -> MessageDone:
    return MessageDone(message=Message.text("assistant", text), stop_reason="stop")


def _tool(call_id: str, name: str, args: dict[str, object]) -> MessageDone:
    return MessageDone(
        message=Message(
            role="assistant",
            content=[ToolCallPart(id=call_id, name=name, args=args)],
        ),
        stop_reason="tool_calls",
    )


def _settings(tmp_path: Path, *, destinations: tuple[str, ...] = ("200",)) -> RickySettings:
    routes: dict[str, MessagingRouteSettings] = {}
    gateway_routes: dict[str, GatewayRouteSettings] = {}
    for index, destination in enumerate(destinations):
        name = "owner" if index == 0 else f"owner-{index}"
        routes[name] = MessagingRouteSettings(
            transport="owner-telegram",
            destination=destination,
            owner_profile="personal",
            accepted_profiles=["shared", "personal"],
        )
        gateway_routes[name] = GatewayRouteSettings(
            provider="openrouter",
            model="test-model",
            primary_profile="personal",
            project_root=str(tmp_path),
        )
    return RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": ".ricky",
            "providers": {"openrouter": {"default_model": "test-model"}},
            "memory": {"enabled": False},
            "workflow": {"enabled": False},
            "google": {"accounts": {}},
            "messaging": MessagingSettings(
                telegram_accounts={
                    "personal/bot": TelegramAccountSettings(
                        bot_token=SecretStr("token"),
                        allowed_sender_ids=["100"],
                        allowed_destination_ids=list(destinations),
                    )
                },
                transports={
                    "owner-telegram": MessagingTransportSettings(
                        type="telegram", account="personal/bot"
                    )
                },
                routes=routes,
            ),
            "gateway": GatewaySettings(
                enabled=True,
                concurrency=4,
                routes=gateway_routes,
            ),
        }
    )


def _guarded_settings(tmp_path: Path) -> RickySettings:
    raw = _settings(tmp_path).model_dump()
    raw["agents"] = {
        "ad_hoc_background": {
            "confirmation_required_capabilities": ["builtin.sandbox.reservation"],
            "guardrail_required_capabilities": ["builtin.sandbox.reservation"],
            "execution": {
                "wall_clock_seconds": 30,
                "iterations": 3,
                "max_completion_tokens_per_request": 256,
                "effect_calls": 1,
            },
        }
    }
    raw["authority"] = {
        "enabled": True,
        "allowed_principals": ["telegram:personal/bot:100"],
        "max_effect_calls": 1,
        "capabilities": {
            "sandbox_reservation": {
                "enabled": True,
                "max_effect_calls": 1,
                "allowed_profiles": ["shared", "personal"],
            }
        },
    }
    return RickySettings.model_validate(raw)


async def _ingest(
    settings: RickySettings,
    *,
    suffix: str,
    text: str,
    destination: str = "200",
    account: str = "personal/bot",
    status: str = "pending",
    reply_to: str | None = None,
    images_resized: bool = False,
) -> InboundMessage:
    message = InboundMessage(
        id="inbound_" + suffix * 32,
        transport="telegram",
        account=account,
        update_id=suffix,
        destination_id=destination,
        sender_id="100",
        platform_message_id=suffix,
        reply_to_platform_message_id=reply_to,
        text=text if status != "rejected" else "[rejected update]",
        images_resized=images_resized,
        received_at=NOW,
        status=cast(Any, status),
    )
    store = MessagingStore(settings)
    await store.initialize()
    await store.ingest(
        ReceiveBatch(
            transport="telegram",
            account=account,
            updates=[
                ReceivedUpdate(
                    update_id=suffix,
                    message=message,
                    rejection_reason="sender is not allowed" if status == "rejected" else None,
                )
            ],
            next_cursor=TransportCursor(transport="telegram", account=account, value=suffix),
        )
    )
    return message


def _job(tmp_path: Path) -> None:
    bundle = tmp_path / "user" / "profiles" / "personal" / "jobs" / "brief"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "job.toml").write_text(
        """version = 3
name = "brief"
description = "Prepare a brief."
provider = "openrouter"
model = "test-model"
goal = "Prepare the brief."
[budget]
wall_clock_seconds = 10
iterations = 2
max_completion_tokens_per_request = 100
effect_calls = 0
[tools]
allow = []
""",
        encoding="utf-8",
    )


async def test_conversation_resumes_same_session_after_process_restart(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    first_message = await _ingest(settings, suffix="a", text="first")
    first_provider = ScriptedProvider([[_answer("first answer")]])
    first = ConversationCoordinator(
        settings,
        provider_factory=lambda _name, _settings: first_provider,
    )
    one = await first.process(first_message.id)

    second_message = await _ingest(settings, suffix="b", text="second")
    second_provider = ScriptedProvider([[_answer("second answer")]])
    restarted = ConversationCoordinator(
        settings,
        provider_factory=lambda _name, _settings: second_provider,
    )
    two = await restarted.process(second_message.id)

    assert one.conversation_id == two.conversation_id
    assert one.session_id == two.session_id
    stored = await SessionStore(settings).get(one.session_id, scope=_SCOPE)
    assert [
        part.text
        for message in stored.session.history
        for part in message.content
        if isinstance(part, TextPart)
    ] == ["first", "first answer", "second", "second answer"]
    assert any(
        "first answer" in part.text
        for message in second_provider.requests[0].messages
        for part in message.content
        if isinstance(part, TextPart)
    )


async def test_new_rotates_and_archives_without_provider_or_history_deletion(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    message = await _ingest(settings, suffix="a", text="hello")
    provider = ScriptedProvider([[_answer("remember me")]])
    coordinator = ConversationCoordinator(
        settings, provider_factory=lambda _name, _settings: provider
    )
    old = await coordinator.process(message.id)

    command = await _ingest(settings, suffix="b", text="/new")
    new = await coordinator.process(command.id)

    assert new.session_id != old.session_id
    assert (
        await GatewayStore(settings).get(old.conversation_id, scope=_SCOPE)
    ).status == "archived"
    assert (await SessionStore(settings).get(old.session_id, scope=_SCOPE)).status == "archived"
    assert (await SessionStore(settings).get(new.session_id, scope=_SCOPE)).session.history == []
    assert len(provider.requests) == 1


async def test_context_reports_exact_foreground_session_without_model_request(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    turn_provider = ScriptedProvider([[_answer("remember me")]])
    inspection_provider = ScriptedProvider([])
    providers = [turn_provider, inspection_provider]

    def factory(_name: str, _settings: RickySettings) -> ScriptedProvider:
        return providers.pop(0)

    coordinator = ConversationCoordinator(settings, provider_factory=factory)
    message = await _ingest(settings, suffix="a", text="hello")
    first = await coordinator.process(message.id)
    command = await _ingest(settings, suffix="b", text="/context")
    inspected = await coordinator.process(command.id)

    from ricky.notifications.store import NotificationStore

    reply = await NotificationStore(settings).get_by_outbox(
        inspected.response_outbox_id,  # type: ignore[arg-type]
        scope=_SCOPE,
    )
    stored = await SessionStore(settings).get(first.session_id, scope=_SCOPE)

    assert inspected.session_id == first.session_id
    assert f"Session: {first.session_id} (revision 1)" in reply.request.body
    assert "Model: openrouter · test-model" in reply.request.body
    assert "pending user input: no" in reply.request.body
    assert "- conversation_text:" in reply.request.body
    assert "- gateway:" in reply.request.body
    assert "- gateway_activity:" in reply.request.body
    assert "- advertised_tool_definitions:" in reply.request.body
    assert stored.revision == 1
    assert [
        part.text
        for item in stored.session.history
        for part in item.content
        if isinstance(part, TextPart)
    ] == ["hello", "remember me"]
    assert len(turn_provider.requests) == 1
    assert inspection_provider.requests == []
    assert inspection_provider.closed
    assert providers == []


async def test_help_exposes_context_gateway_command(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    provider = ScriptedProvider([])
    command = await _ingest(settings, suffix="a", text="/help")
    result = await ConversationCoordinator(
        settings,
        provider_factory=lambda _name, _settings: provider,
    ).process(command.id)

    from ricky.notifications.store import NotificationStore

    reply = await NotificationStore(settings).get_by_outbox(
        result.response_outbox_id,  # type: ignore[arg-type]
        scope=_SCOPE,
    )
    assert "/context" in reply.request.body
    assert provider.requests == []


async def test_route_policy_drift_fails_closed_and_new_rebinds_current_route(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    first = await _ingest(settings, suffix="a", text="hello")
    provider = ScriptedProvider([[_answer("remember me")]])
    coordinator = ConversationCoordinator(
        settings, provider_factory=lambda _name, _settings: provider
    )
    old = await coordinator.process(first.id)

    settings.gateway.routes["owner"].model = "changed-model"
    drifted = await _ingest(settings, suffix="b", text="continue")
    result = await coordinator.process(drifted.id)

    from ricky.notifications.store import NotificationStore

    assert result.response_outbox_id is not None
    reply = await NotificationStore(settings).get_by_outbox(result.response_outbox_id, scope=_SCOPE)
    assert "send /new" in reply.request.body
    assert len(provider.requests) == 1
    assert (await GatewayStore(settings).get(old.conversation_id, scope=_SCOPE)).status == "active"

    rotate = await _ingest(settings, suffix="c", text="/new")
    rebound = await coordinator.process(rotate.id)
    active = await GatewayStore(settings).get(rebound.conversation_id, scope=_SCOPE)
    assert rebound.conversation_id != old.conversation_id
    assert active.model == "changed-model"
    assert active.route_policy_digest is not None
    assert active.created_for_inbound_message_id == rotate.id


async def test_replayed_new_command_reuses_its_recorded_replacement(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    first = await _ingest(settings, suffix="a", text="hello")
    provider = ScriptedProvider([[_answer("remember me")]])
    coordinator = ConversationCoordinator(
        settings, provider_factory=lambda _name, _settings: provider
    )
    old = await coordinator.process(first.id)
    command = await _ingest(settings, suffix="b", text="/new")
    old_conversation = await GatewayStore(settings).get(old.conversation_id, scope=_SCOPE)
    key = ConversationKey(
        transport=command.transport,
        account=command.account,
        destination_id=command.destination_id,
        thread_id=command.thread_id,
    )

    replacement = await coordinator._rotate(old_conversation, key, command.id)
    replayed = await coordinator.process(command.id)

    assert replayed.conversation_id == replacement.id
    gateway = GatewayStore(settings)
    assert len(await gateway.list(scope=_SCOPE, status="archived", limit=10)) == 1
    assert len(await gateway.list(scope=_SCOPE, status="active", limit=10)) == 1
    assert len(provider.requests) == 1


@pytest.mark.parametrize(
    "completed_phase",
    [
        "conversation_archive",
        "session_archive",
        "replacement_session",
        "replacement_conversation",
    ],
)
async def test_new_replay_resumes_each_rotation_phase_without_orphans(
    tmp_path: Path,
    completed_phase: str,
) -> None:
    from ricky.agent.session import AgentSession
    from ricky.capabilities.policy import policy_digest
    from ricky.gateway.conversations import _rotation_session_id

    settings = _settings(tmp_path)
    first = await _ingest(settings, suffix="a", text="hello")
    provider = ScriptedProvider([[_answer("remember me")]])
    coordinator = ConversationCoordinator(
        settings,
        provider_factory=lambda _name, _settings: provider,
    )
    old_result = await coordinator.process(first.id)
    command = await _ingest(settings, suffix="b", text="/new")
    key = ConversationKey(
        transport=command.transport,
        account=command.account,
        destination_id=command.destination_id,
        thread_id=command.thread_id,
    )
    gateway = GatewayStore(settings)
    sessions = SessionStore(settings)
    old = await gateway.get(old_result.conversation_id, scope=_SCOPE)
    archived = await gateway.archive(
        old.id,
        scope=_SCOPE,
        expected_revision=old.revision,
        for_inbound_message_id=command.id,
    )

    if completed_phase in {
        "session_archive",
        "replacement_session",
        "replacement_conversation",
    }:
        stored = await sessions.get(old.session_id, scope=_SCOPE)
        await sessions.archive(old.session_id, stored.revision, scope=_SCOPE)

    replacement_session: AgentSession | None = None
    if completed_phase in {"replacement_session", "replacement_conversation"}:
        route = settings.gateway.routes[archived.route_name]
        replacement_session = AgentSession.create(
            settings,
            profile_scope=route.profile_scope(),
            provider=route.provider,
            model=route.model,
        ).model_copy(update={"id": _rotation_session_id(key, command.id)})
        await sessions.create(replacement_session, scope=route.profile_scope())

    if completed_phase == "replacement_conversation":
        assert replacement_session is not None
        route = settings.gateway.routes[archived.route_name]
        await gateway.create(
            key=key,
            session_id=replacement_session.id,
            route_name=archived.route_name,
            provider=replacement_session.provider,
            model=replacement_session.model,
            profile_scope=replacement_session.profile_scope,
            project_root=str(tmp_path.resolve()),
            route_policy_digest=policy_digest(
                settings.agents.gateway_foreground,
                route,
            ),
            created_for_inbound_message_id=command.id,
        )

    replayed = await coordinator.process(command.id)

    active = await gateway.get(replayed.conversation_id, scope=_SCOPE)
    assert active.created_for_inbound_message_id == command.id
    assert len(await gateway.list(scope=_SCOPE, status="archived", limit=10)) == 1
    assert len(await gateway.list(scope=_SCOPE, status="active", limit=10)) == 1
    assert len(await sessions.list(scope=_SCOPE, limit=10)) == 2
    assert (await sessions.get(old.session_id, scope=_SCOPE)).status == "archived"
    assert (await sessions.get(active.session_id, scope=_SCOPE)).status == "active"
    assert len(provider.requests) == 1


async def test_rejected_sender_reaches_no_provider_task_execution_or_reply(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    rejected = await _ingest(
        settings,
        suffix="a",
        text="ignored",
        status="rejected",
    )
    provider = ScriptedProvider([[_answer("must not happen")]])
    coordinator = ConversationCoordinator(
        settings, provider_factory=lambda _name, _settings: provider
    )
    with pytest.raises(GatewayConversationError, match="rejected"):
        await coordinator.process(rejected.id)

    assert provider.requests == []
    assert not user_data_subpath(settings, settings.gateway.store_path).exists()
    assert not user_data_subpath(settings, settings.sessions.store_path).exists()
    assert not user_data_subpath(settings, settings.executions.store_path).exists()
    assert not (tmp_path / ".ricky" / "tasks").exists()

    from ricky.notifications.store import NotificationStore

    notifications = NotificationStore(settings)
    await notifications.initialize()
    assert await notifications.list(scope=_SCOPE, limit=10) == []


@pytest.mark.parametrize("images_resized", [False, True])
async def test_named_job_is_queued_once_and_foreground_returns_without_running_it(
    tmp_path: Path,
    images_resized: bool,
) -> None:
    settings = _settings(tmp_path)
    _job(tmp_path)
    inbound = await _ingest(
        settings, suffix="a", text="Run my brief job", images_resized=images_resized
    )
    provider = ScriptedProvider(
        [
            [_tool("job", "start_named_job", {"name": "brief"})],
            [_answer("Queued the brief job.")],
        ]
    )
    coordinator = ConversationCoordinator(
        settings, provider_factory=lambda _name, _settings: provider
    )
    result = await asyncio.wait_for(coordinator.process(inbound.id), timeout=3)
    replay = await coordinator.process(inbound.id)

    store = ExecutionStore(settings)
    requests = await store.list(scope=_SCOPE, limit=10)
    assert result == replay
    assert len(requests) == 1
    assert requests[0].kind == "named_job"
    assert requests[0].status == "awaiting_acknowledgement"
    assert requests[0].source_conversation_id == result.conversation_id
    assert requests[0].source_message_id == inbound.id
    assert len(provider.requests) == 1
    from ricky.notifications.store import NotificationStore

    assert result.response_outbox_id is not None
    reply = await NotificationStore(settings).get_by_outbox(
        result.response_outbox_id,
        scope=_SCOPE,
    )
    assert requests[0].id not in reply.request.body
    assert "Background work:" not in reply.request.body
    assert ("resized" in reply.request.body.lower()) is images_resized
    assert requests[0].acknowledgement_outbox_id == result.response_outbox_id
    assert any(ref.id == requests[0].id for ref in reply.request.correlations)
    assert await store.claim(scope=_SCOPE, worker_id="early", limit=1) == []
    assert await coordinator.reconcile_handoffs() == 0
    transport = HandoffTransport()
    messaging = _handoff_messaging(settings, transport)
    assert await messaging.deliver_once() == 1
    assert await coordinator.reconcile_handoffs() == 1
    assert await coordinator.reconcile_handoffs() == 0
    assert (await store.get(requests[0].id, scope=_SCOPE)).status == "queued"
    assert len(provider.requests) == 1
    from ricky.executions.dispatcher import ExecutionDispatcher
    from ricky.notifications.routes import RoutePolicy
    from ricky.notifications.service import NotificationService

    worker = ScriptedProvider([[_answer("Here is your brief.")]])
    routes = RoutePolicy(settings, conversation_resolver=GatewayStore(settings))
    dispatcher = ExecutionDispatcher(
        settings,
        project_root=tmp_path,
        store=store,
        provider_factory=lambda _: worker,
        routes=routes,
        notifications=NotificationService(settings, routes=routes),
    )
    completed = await dispatcher.worker_once(scope=_SCOPE)
    assert len(completed) == 1
    assert completed[0].status == "succeeded", completed[0].error
    assert await messaging.deliver_once() == 1
    assert len(transport.sent) == 2
    assert "Here is your brief." not in transport.sent[0].text
    assert "Here is your brief." in transport.sent[1].text
    assert len(provider.requests) == 1
    assert len(worker.requests) == 1
    assert await dispatcher.worker_once(scope=_SCOPE) == []
    assert await messaging.deliver_once() == 0


async def test_ad_hoc_instruction_creates_task_before_execution_request(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    inbound = await _ingest(settings, suffix="a", text="Research the topic in the background")
    provider = AdHocProvider()
    result = await ConversationCoordinator(
        settings,
        provider_factory=lambda _name, _settings: provider,
    ).process(inbound.id)

    requests = await ExecutionStore(settings).list(scope=_SCOPE, limit=10)
    assert len(requests) == 1
    request = requests[0]
    assert request.kind == "ad_hoc"
    assert request.contract_id is not None
    assert request.contract_digest is not None
    assert request.task_id is not None
    assert request.source_conversation_id == result.conversation_id
    from ricky.durable_tasks.store import DurableTaskStore

    task = await (await DurableTaskStore.create(settings, profile="personal")).get_task(
        request.task_id
    )
    assert task.created_at <= request.created_at
    from ricky.notifications.store import NotificationStore

    assert result.response_outbox_id is not None
    reply = await NotificationStore(settings).get_by_outbox(
        result.response_outbox_id,
        scope=_SCOPE,
    )
    assert request.status == "awaiting_acknowledgement"
    assert request.id not in reply.request.body
    assert request.task_id not in reply.request.body
    assert {request.id, request.task_id} <= {ref.id for ref in reply.request.correlations}
    assert len(provider.requests) == 2


async def test_multiple_handoffs_share_one_delivered_acknowledgement(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _job(tmp_path)
    jobs = tmp_path / "user" / "profiles" / "personal" / "jobs"
    second = jobs / "second"
    second.mkdir()
    (second / "job.toml").write_text(
        (jobs / "brief" / "job.toml").read_text().replace('name = "brief"', 'name = "second"')
    )
    provider = ScriptedProvider(
        [
            [
                MessageDone(
                    message=Message(
                        role="assistant",
                        content=[
                            ToolCallPart(
                                id="first", name="start_named_job", args={"name": "brief"}
                            ),
                            ToolCallPart(
                                id="second", name="start_named_job", args={"name": "second"}
                            ),
                        ],
                    ),
                    stop_reason="tool_calls",
                )
            ]
        ]
    )
    inbound = await _ingest(settings, suffix="a", text="Run both jobs")
    coordinator = ConversationCoordinator(settings, provider_factory=lambda *_: provider)
    result = await coordinator.process(inbound.id)
    store = ExecutionStore(settings)
    requests = await store.list(scope=_SCOPE, limit=10)
    assert len(requests) == 2
    assert all(request.status == "awaiting_acknowledgement" for request in requests)
    assert {request.acknowledgement_outbox_id for request in requests} == {
        result.response_outbox_id
    }
    assert await store.claim(scope=_SCOPE, worker_id="early", limit=1) == []
    assert len(provider.requests) == 1

    transport = HandoffTransport()
    assert await _handoff_messaging(settings, transport).deliver_once() == 1
    assert len(transport.sent) == 1
    assert await coordinator.reconcile_handoffs() == 2
    assert await coordinator.reconcile_handoffs() == 0
    assert len(await store.claim(scope=_SCOPE, worker_id="ready", limit=2)) == 2


@pytest.mark.parametrize("failure_boundary", ["enqueue", "finish_result"])
async def test_committed_handoff_recovers_coordinator_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_boundary: str,
) -> None:
    settings = _settings(tmp_path)
    _job(tmp_path)
    provider = ScriptedProvider([[_tool("job", "start_named_job", {"name": "brief"})]])
    coordinator = ConversationCoordinator(settings, provider_factory=lambda *_: provider)
    owner = coordinator.notifications if failure_boundary == "enqueue" else coordinator.gateway
    method_name = "enqueue" if failure_boundary == "enqueue" else "finish_result"
    original = getattr(owner, method_name)
    failed = False

    async def fail_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("simulated durable write failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(owner, method_name, fail_once)
    inbound = await _ingest(settings, suffix="a", text="Run my brief")
    result = await coordinator.process(inbound.id)
    assert result.status == "uncertain"
    records = await coordinator.notifications.list(scope=_SCOPE)
    assert len(records) == (0 if failure_boundary == "enqueue" else 1)
    assert await coordinator.reconcile_handoffs() == 0
    records = await coordinator.notifications.list(scope=_SCOPE)
    assert len(records) == 1
    assert "background" in records[0].request.body
    assert "simulated durable write failure" not in records[0].request.body
    assert (await coordinator.messaging.get_inbox(inbound.id)).status == "processed"
    transport = HandoffTransport()
    assert await _handoff_messaging(settings, transport).deliver_once() == 1
    assert await coordinator.reconcile_handoffs() == 1
    assert len(provider.requests) == 1
    assert (
        len(await coordinator.execution_store.claim(scope=_SCOPE, worker_id="ready", limit=1)) == 1
    )


async def test_guarded_delegation_extracts_proactive_fields_stops_and_confirms_once(
    tmp_path: Path,
) -> None:
    settings = _guarded_settings(tmp_path)
    request_text = (
        "In the background, reserve Restaurant A (venue ID venue-a) for 2 people on "
        "2026-08-19 between 6 PM and 8 PM Chicago time using alex@example.com. "
        "Do not permit any deposit."
    )
    inbound = await _ingest(settings, suffix="a", text=request_text)
    provider = GuardedDelegationProvider(request_text)
    coordinator = ConversationCoordinator(
        settings,
        provider_factory=lambda _name, _settings: provider,
    )

    first = await coordinator.process(inbound.id)

    assert len(provider.requests) == 2
    from ricky.notifications.store import NotificationStore

    assert first.response_outbox_id is not None
    first_reply = await NotificationStore(settings).get_by_outbox(
        first.response_outbox_id, scope=_SCOPE
    )
    assert "Restaurant A (venue-a) for 2" in first_reply.request.body
    assert "Approval authorizes the background worker" in first_reply.request.body
    assert "Reply Yes to approve" in first_reply.request.body
    drafts = await ExecutionStore(settings).list_drafts(scope=_SCOPE, limit=10)
    assert len(drafts) == 1
    assert drafts[0].status == "awaiting_confirmation"
    assert drafts[0].pending_questions == ()
    assert len(drafts[0].collected_guardrail_fields) == 9
    assert all(field.source_quote is None for field in drafts[0].collected_guardrail_fields)
    assert await ExecutionStore(settings).list(scope=_SCOPE, limit=10) == []

    yes = await _ingest(settings, suffix="b", text="Yes.")
    second = await coordinator.process(yes.id)

    assert len(provider.requests) == 3
    requests = await ExecutionStore(settings).list(scope=_SCOPE, limit=10)
    assert len(requests) == 1
    assert requests[0].status == "awaiting_acknowledgement"
    queued_draft = (await ExecutionStore(settings).list_drafts(scope=_SCOPE, limit=10))[0]
    assert queued_draft.status == "queued"
    assert queued_draft.confirmation is not None
    grants = await AuthorityStore(settings).list(scope=_SCOPE, limit=10)
    assert len(grants) == 1
    assert grants[0].execution_request_id == requests[0].id
    assert second.response_outbox_id is not None
    second_reply = await NotificationStore(settings).get_by_outbox(
        second.response_outbox_id, scope=_SCOPE
    )
    assert requests[0].id not in second_reply.request.body
    assert any(ref.id == requests[0].id for ref in second_reply.request.correlations)


async def test_rejection_after_coordinator_restart_cancels_draft_without_queueing(
    tmp_path: Path,
) -> None:
    settings = _guarded_settings(tmp_path)
    request_text = (
        "In the background, reserve Restaurant A (venue ID venue-a) for 2 people on "
        "2026-08-19 between 6 PM and 8 PM Chicago time using alex@example.com. "
        "Do not permit any deposit."
    )
    inbound = await _ingest(settings, suffix="a", text=request_text)
    first_coordinator = ConversationCoordinator(
        settings,
        provider_factory=lambda _name, _settings: GuardedDelegationProvider(request_text),
    )

    await first_coordinator.process(inbound.id)
    awaiting = (await ExecutionStore(settings).list_drafts(scope=_SCOPE, limit=10))[0]
    assert awaiting.status == "awaiting_confirmation"

    rejection = await _ingest(settings, suffix="b", text="nah bro... request denied")
    restarted_provider = RejectPendingDelegationProvider()
    restarted_coordinator = ConversationCoordinator(
        settings,
        provider_factory=lambda _name, _settings: restarted_provider,
    )
    result = await restarted_coordinator.process(rejection.id)

    cancelled = await ExecutionStore(settings).get_draft(awaiting.id, scope=_SCOPE)
    assert cancelled.status == "cancelled"
    assert cancelled.revision == awaiting.revision + 1
    assert cancelled.contract_id is None and cancelled.request_id is None
    assert await ExecutionStore(settings).list(scope=_SCOPE, limit=10) == []
    from ricky.durable_tasks.store import DurableTaskStore

    assert cancelled.task_id is not None
    task = await (await DurableTaskStore.create(settings, profile="personal")).get_task(
        cancelled.task_id
    )
    assert task.status == "open"
    from ricky.notifications.store import NotificationStore

    assert result.response_outbox_id is not None
    reply = await NotificationStore(settings).get_by_outbox(result.response_outbox_id, scope=_SCOPE)
    assert "Cancelled the pending background execution request" in reply.request.body


async def test_guardrail_clarification_preserves_prior_fields_and_stops_each_turn(
    tmp_path: Path,
) -> None:
    settings = _guarded_settings(tmp_path)
    request_text = (
        "In the background, reserve Restaurant A (venue ID venue-a) on 2026-08-19 "
        "between 18:00 and 20:00 America/Chicago using alex@example.com. "
        "No deposit is allowed."
    )
    first_inbound = await _ingest(settings, suffix="a", text=request_text)
    provider = ClarifyingDelegationProvider(request_text)
    coordinator = ConversationCoordinator(
        settings,
        provider_factory=lambda _name, _settings: provider,
    )

    first = await coordinator.process(first_inbound.id)

    assert len(provider.requests) == 2
    from ricky.notifications.store import NotificationStore

    assert first.response_outbox_id is not None
    first_reply = await NotificationStore(settings).get_by_outbox(
        first.response_outbox_id, scope=_SCOPE
    )
    assert first_reply.request.body == "- How many people is the reservation for?"
    collecting = (await ExecutionStore(settings).list_drafts(scope=_SCOPE, limit=10))[0]
    assert collecting.status == "collecting_guardrails"
    assert len(collecting.collected_guardrail_fields) == 8

    party = await _ingest(settings, suffix="b", text="The reservation is for 2 people.")
    second = await coordinator.process(party.id)

    assert len(provider.requests) == 3
    assert second.response_outbox_id is not None
    second_reply = await NotificationStore(settings).get_by_outbox(
        second.response_outbox_id, scope=_SCOPE
    )
    assert "Restaurant A (venue-a) for 2" in second_reply.request.body
    assert "Reply Yes to approve" in second_reply.request.body
    awaiting = (await ExecutionStore(settings).list_drafts(scope=_SCOPE, limit=10))[0]
    assert awaiting.status == "awaiting_confirmation"
    assert len(awaiting.collected_guardrail_fields) == 9
    evidence = {item.field: item for item in awaiting.collected_guardrail_fields}
    assert evidence["window_start"].source_message_id == first_inbound.id
    assert evidence["party_size"].source_message_id == party.id

    yes = await _ingest(settings, suffix="c", text="Yes.")
    await coordinator.process(yes.id)

    assert len(provider.requests) == 4
    requests = await ExecutionStore(settings).list(scope=_SCOPE, limit=10)
    assert len(requests) == 1
    assert requests[0].status == "awaiting_acknowledgement"


async def test_status_reports_linked_task_and_execution_and_cancel_is_scoped(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    inbound = await _ingest(settings, suffix="a", text="Research in the background")
    provider = AdHocProvider()
    coordinator = ConversationCoordinator(
        settings,
        provider_factory=lambda _name, _settings: provider,
    )
    first = await coordinator.process(inbound.id)
    request = (await ExecutionStore(settings).list(scope=_SCOPE, limit=10))[0]
    assert request.task_id is not None

    status_message = await _ingest(settings, suffix="b", text="/status")
    status_result = await coordinator.process(status_message.id)
    from ricky.notifications.store import NotificationStore

    status_reply = await NotificationStore(settings).get_by_outbox(
        status_result.response_outbox_id,  # type: ignore[arg-type]
        scope=_SCOPE,
    )
    assert f"task {request.task_id} open rev=1" in status_reply.request.body
    assert (
        f"execution {request.id} awaiting_acknowledgement task={request.task_id}"
        in status_reply.request.body
    )

    cancel_message = await _ingest(
        settings,
        suffix="c",
        text=f"/cancel {request.id}",
    )
    cancel_result = await coordinator.process(cancel_message.id)
    cancelled = await ExecutionStore(settings).get(request.id, scope=_SCOPE)
    cancel_reply = await NotificationStore(settings).get_by_outbox(
        cancel_result.response_outbox_id,  # type: ignore[arg-type]
        scope=_SCOPE,
    )

    assert first.conversation_id == status_result.conversation_id
    assert status_result.conversation_id == cancel_result.conversation_id
    assert cancelled.status == "cancelled"
    assert f"{request.id}: cancelled" in cancel_reply.request.body
    assert len(provider.requests) == 2


async def test_background_turn_receives_named_jobs_and_delegable_capability_catalog(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _job(tmp_path)
    inbound = await _ingest(
        settings,
        suffix="a",
        text="Research this in the background and report back",
    )
    provider = AdHocProvider()

    await ConversationCoordinator(
        settings, provider_factory=lambda _name, _settings: provider
    ).process(inbound.id)

    first_request_text = "\n".join(
        part.text
        for message in provider.requests[0].messages
        for part in message.content
        if isinstance(part, TextPart)
    )
    assert "must use fire-and-report control-plane tools" in first_request_text
    assert '"name": "personal/brief"' in first_request_text
    assert '"name": "builtin.project.read"' in first_request_text
    assert "execution_profiles" not in first_request_text


@pytest.mark.parametrize("background_enabled", [False, True])
async def test_actual_gateway_request_preserves_background_browser_catalog_and_intake(
    tmp_path: Path, background_enabled: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ricky.browser.guardrails import browser_guardrail_evaluators

    # This regression needs the production browser evaluators, not the module's
    # synthetic reservation-only evaluator fixture.
    monkeypatch.setattr(
        "ricky.runtime.composition.built_in_guardrail_evaluators", browser_guardrail_evaluators
    )
    settings = _settings(tmp_path).model_copy(
        update={
            "browser": BrowserSettings.model_validate(
                {
                    "enabled": True,
                    "background": {
                        "enabled": background_enabled,
                        "read_enabled": background_enabled,
                        "interaction_enabled": background_enabled,
                        "commit_enabled": background_enabled,
                    },
                }
            )
        }
    )
    inbound = await _ingest(
        settings, suffix="a", text="Use my personal browser profile to check my account balance."
    )
    provider = ScriptedProvider([[MessageDone(message=Message.text("assistant", "Acknowledged."))]])

    await ConversationCoordinator(
        settings, provider_factory=lambda _name, _settings: provider
    ).process(inbound.id)

    request = provider.requests[0]
    text = "\n".join(
        part.text
        for message in request.messages
        for part in message.content
        if isinstance(part, TextPart)
    )
    catalog = json.loads(
        text.split("Valid ad hoc capabilities: ", 1)[1].split(
            ". Exact guarded capability intake specifications:", 1
        )[0]
    )
    by_name = {item["name"]: item for item in catalog}
    browser_ids = {"builtin.browser.read", "builtin.browser.interact", "builtin.browser.commit"}
    if background_enabled:
        assert browser_ids <= by_name.keys()
        # Browser read requires a guardrail even with empty owner policy lists.
        for capability_id in browser_ids:
            assert by_name[capability_id]["guardrail_required"]
            assert by_name[capability_id]["guardrail_intake"]["fields"]
        assert "browser_session_open_resource" in by_name["builtin.browser.interact"]["resources"]
    else:
        assert browser_ids.isdisjoint(by_name)
    tools = {tool.name for tool in request.tools}
    assert "delegate_task" in tools
    assert not any(name.startswith("browser_") for name in tools)
    assert "builtin.protected_value.use" not in by_name


class FailingProvider:
    name = "failing"

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        if False:
            yield cast(Any, None)
        raise RuntimeError("provider unavailable with a deliberately bounded diagnostic")

    async def aclose(self) -> None:
        return None


class BlockingAnswerProvider:
    name = "blocking"

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        del request
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        yield _answer(self.answer)

    async def aclose(self) -> None:
        return None


async def test_correlation_store_failure_creates_bounded_user_message_without_provider(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    inbound = await _ingest(
        settings,
        suffix="a",
        text="Discuss this",
        reply_to="unknown-platform-message",
    )
    provider = ScriptedProvider([[_answer("must not run")]])
    result = await ConversationCoordinator(
        settings,
        provider_factory=lambda _name, _settings: provider,
    ).process(inbound.id)

    stored_result = await GatewayStore(settings).get_result(inbound.id, scope=_SCOPE)
    assert stored_result is not None and stored_result.status == "failed"
    assert provider.requests == []
    from ricky.notifications.store import NotificationStore

    reply = await NotificationStore(settings).get_by_outbox(
        result.response_outbox_id,  # type: ignore[arg-type]
        scope=_SCOPE,
    )
    assert "trusted Ricky delivery receipt" in reply.request.body


async def test_provider_failure_creates_bounded_user_message_and_terminal_result(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    inbound = await _ingest(settings, suffix="a", text="hello")
    provider = FailingProvider()
    result = await ConversationCoordinator(
        settings,
        provider_factory=lambda _name, _settings: provider,
    ).process(inbound.id)

    stored_result = await GatewayStore(settings).get_result(inbound.id, scope=_SCOPE)
    assert result.status == "processed"
    assert stored_result is not None and stored_result.status == "failed"
    assert stored_result.response_outbox_id is not None
    from ricky.notifications.store import NotificationStore

    response = await NotificationStore(settings).get_by_outbox(
        stored_result.response_outbox_id, scope=_SCOPE
    )
    assert "couldn't complete" in response.request.body
    assert response.request.body_format == "portable_markdown_v1"
    assert len(response.request.body) <= settings.messaging.body_char_limit


async def test_cancellation_during_provider_leaves_explicit_uncertain_states(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    inbound = await _ingest(settings, suffix="a", text="wait")
    provider = BlockingAnswerProvider("never")
    coordinator = ConversationCoordinator(
        settings,
        provider_factory=lambda _name, _settings: provider,
    )
    task = asyncio.create_task(coordinator.process(inbound.id))
    await provider.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (await MessagingStore(settings).get_inbox(inbound.id)).status == "uncertain"
    result = await GatewayStore(settings).get_result(inbound.id, scope=_SCOPE)
    assert result is not None and result.status == "uncertain"
    assert (
        await GatewayStore(settings).get(result.conversation_id, scope=_SCOPE)
    ).status == "uncertain"
    assert provider.cancelled


async def test_agent_events_are_forwarded_without_affecting_a_turn(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    inbound = await _ingest(settings, suffix="e", text="inspect task")
    provider = ScriptedProvider(
        [
            [_tool("call_x", "read_durable_task", {"task_id": "task_missing"})],
            [_answer("No task found.")],
        ]
    )
    events = []

    def factory(_name: str, _settings: RickySettings) -> ScriptedProvider:
        return provider

    coordinator = ConversationCoordinator(
        settings,
        provider_factory=factory,
        event_sink=events.append,
    )
    result = await coordinator.process(inbound.id)

    assert result.status == "processed"
    assert {event.kind for event in events} >= {
        "session_started",
        "turn_started",
        "tool_call_requested",
        "tool_call_started",
        "tool_call_finished",
        "turn_finished",
    }


async def test_raising_agent_event_sink_does_not_change_durable_turn(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    inbound = await _ingest(settings, suffix="f", text="hello")

    def raising_sink(_event: object) -> None:
        raise RuntimeError("observer unavailable")

    coordinator = ConversationCoordinator(
        settings,
        provider_factory=lambda _name, _settings: ScriptedProvider([[_answer("hello")]]),
        event_sink=raising_sink,
    )
    result = await coordinator.process(inbound.id)

    stored = await SessionStore(settings).get(result.session_id, scope=_SCOPE)
    assert result.status == "processed"
    assert stored.status == "active"
    assert stored.revision == 1


async def test_two_messages_in_one_conversation_run_strictly_in_order(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    first = await _ingest(settings, suffix="a", text="first")
    second = await _ingest(settings, suffix="b", text="second")
    first_provider = BlockingAnswerProvider("first answer")
    second_provider = BlockingAnswerProvider("second answer")
    providers = [first_provider, second_provider]

    def factory(_name: str, _settings: RickySettings) -> BlockingAnswerProvider:
        return providers.pop(0)

    coordinator = ConversationCoordinator(settings, provider_factory=factory)
    first_task = asyncio.create_task(coordinator.process(first.id))
    await first_provider.started.wait()
    second_task = asyncio.create_task(coordinator.process(second.id))
    await asyncio.sleep(0)
    assert not second_provider.started.is_set()

    first_provider.release.set()
    await second_provider.started.wait()
    second_provider.release.set()
    first_result, second_result = await asyncio.gather(first_task, second_task)
    assert first_result.session_id == second_result.session_id
    stored = await SessionStore(settings).get(first_result.session_id, scope=_SCOPE)
    text = [
        part.text
        for message in stored.session.history
        for part in message.content
        if isinstance(part, TextPart)
    ]
    assert text == ["first", "first answer", "second", "second answer"]


async def test_two_conversations_can_run_provider_calls_concurrently(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, destinations=("200", "201"))
    first = await _ingest(settings, suffix="a", text="first", destination="200")
    second = await _ingest(settings, suffix="b", text="second", destination="201")
    first_provider = BlockingAnswerProvider("first answer")
    second_provider = BlockingAnswerProvider("second answer")
    providers = [first_provider, second_provider]

    def factory(_name: str, _settings: RickySettings) -> BlockingAnswerProvider:
        return providers.pop(0)

    coordinator = ConversationCoordinator(settings, provider_factory=factory)
    first_task = asyncio.create_task(coordinator.process(first.id))
    second_task = asyncio.create_task(coordinator.process(second.id))
    await asyncio.wait_for(
        asyncio.gather(
            first_provider.started.wait(),
            second_provider.started.wait(),
        ),
        timeout=2,
    )
    first_provider.release.set()
    second_provider.release.set()
    first_result, second_result = await asyncio.gather(first_task, second_task)
    assert first_result.conversation_id != second_result.conversation_id


async def test_gateway_resolves_provider_and_policy_from_each_route_scope(
    tmp_path: Path,
) -> None:
    settings = RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / "project-data"),
            "profiles": {
                "definitions": {
                    "personal": {
                        "default_provider": "openrouter",
                        "allowed_providers": ["openrouter"],
                    },
                    "work": {
                        "default_provider": "anthropic",
                        "allowed_providers": ["anthropic"],
                    },
                }
            },
            "profile_configs": {
                "personal": ProfileConfigSettings.model_validate(
                    {
                        "openrouter_api_key": "personal-key",
                        "workflow": {"max_parallel_steps": 4},
                        "messaging": {
                            "telegram_accounts": {"bot": {"bot_token": "personal-token"}}
                        },
                    }
                ),
                "work": ProfileConfigSettings.model_validate(
                    {
                        "anthropic_api_key": "work-key",
                        "workflow": {"max_parallel_steps": 1},
                        "messaging": {"telegram_accounts": {"bot": {"bot_token": "work-token"}}},
                        "agents": {
                            "gateway_foreground": {"exclude_capabilities": ["builtin.project.read"]}
                        },
                    }
                ),
            },
            "memory": {"enabled": False},
            "workflow": {"enabled": False, "max_parallel_steps": 8},
            "messaging": {
                "transports": {
                    "personal-telegram": {"type": "telegram", "account": "personal/bot"},
                    "work-telegram": {"type": "telegram", "account": "work/bot"},
                },
                "routes": {
                    "personal": {
                        "transport": "personal-telegram",
                        "destination": "200",
                        "owner_profile": "personal",
                        "accepted_profiles": ["shared", "personal"],
                    },
                    "work": {
                        "transport": "work-telegram",
                        "destination": "201",
                        "owner_profile": "work",
                        "accepted_profiles": ["shared", "work"],
                    },
                },
            },
            "gateway": {
                "enabled": True,
                "routes": {
                    "personal": {
                        "provider": "openrouter",
                        "model": "anthropic/claude-sonnet-4",
                        "primary_profile": "personal",
                        "project_root": str(tmp_path),
                    },
                    "work": {
                        "provider": "anthropic",
                        "model": "claude-sonnet-5",
                        "primary_profile": "work",
                        "project_root": str(tmp_path),
                    },
                },
            },
        }
    )
    personal = await _ingest(
        settings,
        suffix="a",
        text="personal",
        destination="200",
        account="personal/bot",
    )
    work = await _ingest(
        settings,
        suffix="b",
        text="work",
        destination="201",
        account="work/bot",
    )
    observed: dict[str, tuple[bool, bool, int, list[str]]] = {}

    def factory(name: str, runtime_settings: RickySettings) -> ScriptedProvider:
        observed[name] = (
            runtime_settings.openrouter_api_key is not None,
            runtime_settings.anthropic_api_key is not None,
            runtime_settings.workflow.max_parallel_steps,
            runtime_settings.agents.gateway_foreground.exclude_capabilities,
        )
        return ScriptedProvider([[_answer(f"{name} reply")]])

    coordinator = ConversationCoordinator(settings, provider_factory=factory)
    personal_result = await coordinator.process(personal.id)
    work_result = await coordinator.process(work.id)

    assert personal_result.status == "processed"
    assert work_result.status == "processed"
    assert observed == {
        "openrouter": (True, False, 4, []),
        "anthropic": (False, True, 1, ["builtin.project.read"]),
    }
    assert not Path(settings.project_data_dir).exists()


COMPACTION_SUMMARY = """## Current goal and user intent
Continue the gateway conversation.

## Constraints and preferences
Preserve exact durable state.

## Completed work
Earlier discussion was retained.

## In-progress or blocked work
None.

## Decisions and rationale
Use the existing checkpoint contract.

## Corrections and rejected approaches
None.

## Unresolved questions and next actions
Continue.

## Material references
No external artifacts."""


async def test_compact_uses_existing_contract_and_persists_checkpoint(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.context.compaction.keep_recent_tokens = 1
    providers = [
        ScriptedProvider([[_answer("A" * 2_000)]]),
        ScriptedProvider([[_answer("B" * 2_000)]]),
        ScriptedProvider([[_answer("recent answer")]]),
        ScriptedProvider([[_answer(COMPACTION_SUMMARY)]]),
        ScriptedProvider([]),
    ]

    def factory(_name: str, _settings: RickySettings) -> ScriptedProvider:
        return providers.pop(0)

    events = []
    coordinator = ConversationCoordinator(
        settings,
        provider_factory=factory,
        event_sink=events.append,
    )
    result = None
    for suffix, text in (("a", "old one"), ("b", "old two"), ("c", "recent")):
        inbound = await _ingest(settings, suffix=suffix, text=text)
        result = await coordinator.process(inbound.id)
    assert result is not None
    command = await _ingest(settings, suffix="d", text="/compact")
    compacted = await coordinator.process(command.id)

    stored = await SessionStore(settings).get(compacted.session_id, scope=_SCOPE)
    assert stored.revision == 4
    assert stored.session.active_checkpoint_id is not None
    assert len(stored.session.checkpoints) == 1
    assert {event.kind for event in events} >= {
        "context_compaction_started",
        "context_compaction_finished",
    }

    context_command = await _ingest(settings, suffix="e", text="/context")
    inspected = await coordinator.process(context_command.id)
    from ricky.notifications.store import NotificationStore

    reply = await NotificationStore(settings).get_by_outbox(
        inspected.response_outbox_id,  # type: ignore[arg-type]
        scope=_SCOPE,
    )
    assert "Active checkpoint:" in reply.request.body
    assert stored.session.active_checkpoint_id in reply.request.body
    assert "available in original history" in reply.request.body
    assert (await SessionStore(settings).get(compacted.session_id, scope=_SCOPE)).revision == 4


@pytest.mark.parametrize(
    "selection",
    [
        "short",
        "default",
        "sole",
        "ambiguous",
        "encoded",
        "purchase",
        "purchase_denied",
        "purchase_budget_denied",
        "purchase_popup_budget_denied",
        "recover_id",
    ],
)
async def test_browser_delegation_repairs_model_arguments_and_pins_scoped_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str
) -> None:
    from ricky.authority.registry import AuthorityRegistry
    from ricky.browser.authority import browser_authority_evaluators
    from ricky.browser.guardrails import browser_guardrail_evaluators
    from ricky.executions.contracts import load_contract_snapshot

    purchase = selection.startswith("purchase")
    popup_denied = selection == "purchase_popup_budget_denied"
    budget_denied = selection in {"purchase_budget_denied", "purchase_popup_budget_denied"}
    denied = selection == "purchase_denied" or budget_denied
    final_report = (
        "Starting balance: $6.65. No purchase made; browser budget exhausted."
        if budget_denied
        else "Starting balance: $6.65. No purchase made; approval denied."
        if denied
        else "Starting balance: $6.65. Ending balance: $26.65."
        if purchase
        else "$12.34"
    )
    monkeypatch.setattr(
        "ricky.runtime.composition.built_in_guardrail_evaluators", browser_guardrail_evaluators
    )
    monkeypatch.setattr(
        "ricky.authority.compiler.default_authority_registry",
        lambda: AuthorityRegistry(list(browser_authority_evaluators())),
    )
    raw = _settings(tmp_path).model_dump(mode="python")
    raw["browser"] = {
        "enabled": True,
        "background": {
            "enabled": True,
            "read_enabled": True,
            "interaction_enabled": True,
            "commit_enabled": purchase,
            "budget": {"transaction_commits": 1},
        },
    }
    if budget_denied:
        raw["browser"]["background"]["budget"].update(
            {"created_pages": 0} if popup_denied else {"navigations": 2}
        )
    resource = {"kind": "persistent", "headless": True, "description": "Personal browser"}
    browsers = {"ricky-personal": resource}
    if selection in {"default", "ambiguous"}:
        browsers["second"] = resource
    raw["profile_configs"] = {
        "personal": {
            "browser": {
                "resources": browsers,
                "default_resource": "ricky-personal" if selection == "default" else None,
            }
        },
        "work": {"browser": {"resources": {"hidden-browser": resource}}},
        "shared": {
            "browser": {
                "resources": {
                    "shared-browser": resource,
                    "headed-browser": {**resource, "headless": False},
                    "cdp-browser": {
                        "kind": "cdp",
                        "endpoint": "http://127.0.0.1:9222",
                        "description": "External browser",
                    },
                }
            }
        },
    }
    raw["authority"] = {
        "enabled": True,
        "allowed_principals": ["telegram:personal/bot:100"],
        "max_effect_calls": 20 if purchase else 1,
        "capabilities": {
            "browser_interact": {
                "enabled": True,
                "allowed_profiles": ["shared", "personal"],
                "max_effect_calls": 20 if purchase else 1,
            },
            "browser_commit": {
                "enabled": purchase,
                "allowed_profiles": ["shared", "personal"],
                "max_effect_calls": 20 if purchase else 1,
                "max_financial_limit_minor": 3000,
                "currency": "USD",
            },
        },
    }
    raw["agents"] = {
        "ad_hoc_background": {
            "confirmation_required_capabilities": [],
            "guardrail_required_capabilities": [],
            "execution": {"effect_calls": 20 if purchase else 1},
        }
    }
    settings = RickySettings.model_validate(raw)

    class BrowserProvider(AdHocProvider):
        async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
            if self.step == 0:
                rendered = "\n".join(
                    p.text for m in request.messages for p in m.content if isinstance(p, TextPart)
                )
                catalog, _ = json.JSONDecoder().raw_decode(
                    rendered.split("Scoped browser resources: ", 1)[1]
                )
                names = {item["name"] for item in catalog}
                assert "personal/ricky-personal" in names
                assert "shared/shared-browser" in names
                assert not any(
                    "hidden-browser" in name or "headed-browser" in name or "cdp-browser" in name
                    for name in names
                )
                assert "127.0.0.1:9222" not in rendered
                defaults = [item["name"] for item in catalog if item["default"]]
                assert defaults == ([] if selection == "ambiguous" else ["personal/ricky-personal"])
                async for event in super().stream(request):
                    yield event
                return
            self.requests.append(request)
            self.step += 1
            results = "\n".join(
                p.content for m in request.messages for p in m.content if p.kind == "tool_result"
            )
            if self.step in {2, 3} or (selection == "short" and self.step == 4):
                if self.step == 3:
                    assert "Correct the browser contract arguments" in results
                if self.step == 4:
                    assert "masked browser screenshots are not allowed" in results
                task = re.search(r"task_[0-9a-f]{32}", results)
                assert task is not None
                alias = "ricky-personal" if selection == "short" else ""
                guardrails = []
                capability_tools = [
                    ("builtin.browser.read", "browser_navigate,browser_snapshot"),
                    (
                        "builtin.browser.interact",
                        "browser_session_open_resource,browser_click"
                        if purchase
                        else "browser_session_open_resource",
                    ),
                ]
                if purchase:
                    capability_tools.append(("builtin.browser.commit", "browser_commit"))
                for capability, tools in capability_tools:
                    if self.step == 2 and capability == "builtin.browser.read":
                        tools += ",browser_session_open_resource"
                    visual = (
                        selection == "short"
                        and self.step == 3
                        and capability == "builtin.browser.read"
                    )
                    if visual:
                        tools += ",browser_visual_snapshot"
                    fields: dict[str, str | bool] = {
                        "mode": "transaction" if purchase else "read_only",
                        "allowed_tools": tools,
                        "authenticated_origins": f"{alias}#https://openrouter.ai",
                    }
                    if visual:
                        fields["allow_masked_visual_observations"] = True
                    if alias:
                        fields["resources"] = alias
                    guardrails.append(
                        {
                            "capability_id": capability,
                            "fields": [
                                {"field": key, "value": value} for key, value in fields.items()
                            ],
                        }
                    )
                capabilities = ["builtin.browser.read", "builtin.browser.interact"]
                if purchase:
                    capabilities.append("builtin.browser.commit")
                if selection == "encoded":
                    for guardrail in guardrails:
                        guardrail["fields"] = json.dumps(guardrail["fields"])
                yield _tool(
                    f"browser-{self.step}",
                    "delegate_task",
                    {
                        "action": "start",
                        "task_id": task.group(),
                        "expected_task_revision": 1,
                        "goal": "Check balance and purchase $20 credits if below $10."
                        if purchase
                        else "Read my OpenRouter balance only.",
                        "requested_capabilities": json.dumps(capabilities)
                        if selection == "encoded"
                        else capabilities,
                        "guardrails": json.dumps(guardrails)
                        if selection == "encoded"
                        else guardrails,
                    },
                )
                return
            if selection == "ambiguous" and self.step == 4:
                yield _tool(
                    "choose-browser",
                    "delegate_task",
                    {
                        "action": "supply_guardrails",
                        "draft_id": drafts[0].id,
                        "expected_draft_revision": drafts[0].revision,
                        "guardrails": [
                            {
                                "capability_id": capability,
                                "fields": [
                                    {"field": "resources", "value": "ricky-personal"},
                                    {
                                        "field": "authenticated_origins",
                                        "value": "ricky-personal#https://openrouter.ai",
                                    },
                                ],
                            }
                            for capability in ("builtin.browser.read", "builtin.browser.interact")
                        ],
                    },
                )
                return
            yield _answer("Queued the balance check.")

    provider = BrowserProvider()
    inbound = await _ingest(
        settings,
        suffix="a",
        text="Use ricky-personal to check my OpenRouter balance."
        if selection == "short"
        else "Check my OpenRouter balance.",
    )
    coordinator = ConversationCoordinator(settings, provider_factory=lambda *_: provider)
    await coordinator.process(inbound.id)
    store = ExecutionStore(settings)
    requests = await store.list(scope=_SCOPE, limit=10)
    drafts = await store.list_drafts(scope=_SCOPE, limit=10)
    assert len(drafts) == 1
    if selection == "ambiguous":
        assert not requests
        assert drafts[0].status == "collecting_guardrails"
        assert all("Which browser" in question for question in drafts[0].pending_questions)
        answer = await _ingest(settings, suffix="b", text="Use ricky-personal.")
        await coordinator.process(answer.id)
        requests = await store.list(scope=_SCOPE, limit=10)
    assert len(requests) == 1
    assert requests[0].status == "awaiting_acknowledgement"
    assert requests[0].contract_digest is not None
    contract = load_contract_snapshot(settings, requests[0].contract_digest)
    assert contract.browser is not None
    assert contract.browser.mode == ("transaction" if purchase else "read_only")
    tasks = await ScopedDurableTaskStore.create(settings, scope=_SCOPE)
    task = await tasks.get_task(contract.task_id)
    assert requests[0].handoff_title == task.title
    assert requests[0].handoff_title != contract.goal
    assert [item.resource.qualified for item in contract.browser.resources] == [
        "personal/ricky-personal"
    ]
    assert contract.browser.resources[0].authenticated_origin_ceiling == ("https://openrouter.ai",)
    assert not contract.browser.attachments
    assert not contract.browser.protected_resources
    assert ("browser_commit" in contract.browser.allowed_tools) == purchase
    foreground_count = 4 if selection in {"short", "ambiguous"} else 3
    assert len(provider.requests) == foreground_count

    # Exercise the actual worker handoff, not just contract creation and queueing.
    from contextlib import asynccontextmanager

    from browser_support import (
        FakeBrowserBackend,
        FakeBrowserPage,
        FakeBrowserSession,
        fake_executable,
    )
    from ricky.browser.backend import BackendTargetDescriptor
    from ricky.browser.policy import DestinationPolicy
    from ricky.browser.service import BrowserService
    from ricky.executions.dispatcher import ExecutionDispatcher
    from ricky.jobs.store import JobRunStore
    from ricky.notifications.routes import RoutePolicy
    from ricky.notifications.service import NotificationService
    from ricky.runtime import build_session_runtime

    class PurchasePage(FakeBrowserPage):
        async def perform_action(self, request):
            outcome = await super().perform_action(request)
            if request.target.ref == "e2":
                self.snapshot_text = '- document\n  - text "Credit balance: $26.65"'
            return outcome

    page = (
        PurchasePage(
            snapshot=(
                '- document\n  - text "Credit balance: $6.65"\n'
                '  - button "Add Credits" [ref=e1]\n'
                '  - button "Pay $20" [ref=e2]'
            ),
            targets=(
                BackendTargetDescriptor(
                    ref="e1",
                    role="button",
                    name="Add Credits",
                    frame_origin="https://openrouter.ai",
                ),
                BackendTargetDescriptor(
                    ref="e2",
                    role="button",
                    name="Pay $20",
                    consequential=True,
                    frame_origin="https://openrouter.ai",
                ),
            ),
        )
        if purchase
        else FakeBrowserPage(snapshot='- document\n  - text "Credit balance: $12.34"')
    )
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(FakeBrowserSession([page]))

    async def resolve(host: str, port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    monkeypatch.setattr(
        "ricky.browser.service.DestinationPolicy",
        lambda **kwargs: DestinationPolicy(resolver=resolve, **kwargs),
    )

    async def browser_factory(settings: RickySettings, **kwargs: Any) -> BrowserService:
        return BrowserService(
            settings, backend=backend, executable_path=fake_executable(tmp_path), **kwargs
        )

    @asynccontextmanager
    async def runtime_factory(*args: Any, **kwargs: Any):
        async with build_session_runtime(
            *args, background_browser_factory=browser_factory, **kwargs
        ) as runtime:
            yield runtime

    monkeypatch.setattr("ricky.jobs.runner.build_session_runtime", runtime_factory)

    class WorkerProvider(AdHocProvider):
        async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
            expected_tools = {
                "browser_session_open_resource",
                "browser_navigate",
                "browser_snapshot",
            }
            if purchase:
                expected_tools.update({"browser_click", "browser_commit"})
            assert {
                tool.name for tool in request.tools if tool.name.startswith("browser_")
            } == expected_tools
            self.requests.append(request)
            self.step += 1
            results = "\n".join(
                p.content for m in request.messages for p in m.content if p.kind == "tool_result"
            )
            if self.step == 1:
                rendered = "\n".join(
                    p.text for m in request.messages for p in m.content if isinstance(p, TextPart)
                )
                resources, _ = json.JSONDecoder().raw_decode(
                    rendered.split("Authorized browser resources: ", 1)[1]
                )
                assert resources == [
                    {
                        "resource": "personal/ricky-personal",
                        "authenticated_origins": ["https://openrouter.ai"],
                    }
                ]
                assert "shared/shared-browser" not in rendered
                yield _tool(
                    "open", "browser_session_open_resource", {"resource": resources[0]["resource"]}
                )
                return
            session = re.search(r"browser_session_[0-9a-f]{32}", results)
            assert session is not None, results
            stage = self.step - int(selection == "recover_id" and self.step >= 3)
            session_id = session.group()
            if selection == "recover_id":
                if self.step == 2:
                    session_id = "browser_session_" + "f" * 32
                elif self.step == 3:
                    assert not page.navigations
                    feedback = [
                        p.content
                        for m in request.messages
                        for p in m.content
                        if p.kind == "tool_result"
                    ][-1]
                    row = json.loads(
                        feedback.split("Current runtime browser references:\n")[1].splitlines()[0]
                    )
                    session_id = row["session_id"]
            if stage == 2:
                yield _tool(
                    "navigate",
                    "browser_navigate",
                    {
                        "session_id": session_id,
                        "url": "https://openrouter.ai/settings/credits",
                    },
                )
            elif stage == 3 or (purchase and self.step in {5, 7}):
                yield _tool("snapshot", "browser_snapshot", {"session_id": session.group()})
            elif purchase and self.step in {4, 6}:
                latest = [
                    p.content
                    for m in request.messages
                    for p in m.content
                    if p.kind == "tool_result"
                ][-1]
                snapshot = re.search(r"browser_snapshot_[0-9a-f]{32}", latest)
                page_id = re.search(r"browser_page_[0-9a-f]{32}", latest)
                assert snapshot is not None and page_id is not None, latest
                args: dict[str, Any] = {
                    "target": {
                        "session_id": session.group(),
                        "page_id": page_id.group(),
                        "snapshot_id": snapshot.group(),
                        "ref": "e1" if self.step == 4 else "e2",
                    }
                }
                if self.step == 6:
                    assert len(page.actions) == (0 if popup_denied else 1), results
                    args["envelope"] = {
                        "kind": "financial",
                        "intent": "Purchase $20 in credits",
                        "payee": "OpenRouter",
                        "total": {"amount": "20.00", "currency": "USD"},
                        "fees": [],
                        "timing": "one_time",
                        "source": {"kind": "site", "label": "Saved payment method"},
                        "consequences": ["Charges $20 once"],
                        "expected_result": "Balance increases by $20",
                    }
                yield _tool(
                    f"action-{self.step}",
                    "browser_click" if self.step == 4 else "browser_commit",
                    args,
                )
            elif purchase:
                assert ("$6.65" if denied else "$26.65") in results
                if budget_denied:
                    assert (
                        "browser budget exhausted: "
                        + ("created_pages" if popup_denied else "navigations")
                    ) in results
                yield _answer(final_report)
            else:
                assert "$12.34" in results
                yield _answer("$12.34")

    worker = WorkerProvider()
    routes = RoutePolicy(settings, conversation_resolver=GatewayStore(settings))
    dispatcher = ExecutionDispatcher(
        settings,
        project_root=tmp_path,
        store=store,
        provider_factory=lambda _: worker,
        authority_registry=AuthorityRegistry(list(browser_authority_evaluators())),
        routes=routes,
        notifications=NotificationService(settings, routes=routes),
    )
    assert await dispatcher.worker_once(scope=_SCOPE) == []
    assert worker.requests == []
    assert page.navigations == []
    assert await coordinator.reconcile_handoffs() == 0
    transport = HandoffTransport()
    messaging = _handoff_messaging(settings, transport)
    approvals = []
    if purchase:
        coordinator.bind_dispatcher(dispatcher)
        notify_approval = dispatcher.notify_browser_approval

        async def approve_transaction(challenge, *, scope):
            assert not budget_denied, "exhausted browser capacity must not prompt for approval"
            assert len(page.actions) == 1  # Preparation must not dispatch the purchase.
            assert challenge.approval.envelope.total.amount == "20.00"
            await notify_approval(challenge, scope=scope)
            assert await messaging.deliver_once() >= 1
            assert any("/approve" in message.text for message in transport.sent)
            approvals.append(challenge.approval.id)
            command = await _ingest(
                settings,
                suffix="c",
                text=f"/{'deny' if denied else 'approve'} {challenge.approval.id} {challenge.code}",
            )
            await coordinator.process(command.id)

        monkeypatch.setattr(dispatcher, "notify_browser_approval", approve_transaction)
    assert await messaging.deliver_once() == (2 if selection == "ambiguous" else 1)
    assert "$12.34" not in transport.sent[-1].text
    assert task.title in transport.sent[-1].text
    assert await coordinator.reconcile_handoffs() == 1
    completed = await dispatcher.worker_once(scope=_SCOPE)
    assert len(completed) == 1
    assert completed[0].status == ("failed" if denied else "succeeded"), completed[0].error
    assert completed[0].run_id is not None
    run = await JobRunStore(settings).get(completed[0].run_id, scope=_SCOPE)
    assert run.final_message == final_report
    assert len(worker.requests) == (8 if purchase else 5 if selection == "recover_id" else 4)
    if purchase:
        assert len(approvals) == (0 if budget_denied else 1)
        assert [action.target.ref for action in page.actions] == (
            [] if popup_denied else ["e1"] if denied else ["e1", "e2"]
        )
        receipts = await JobRunStore(settings).actions_for_run(run.id, scope=_SCOPE)
        assert [receipt.status for receipt in receipts] == (
            ["not_performed"]
            if popup_denied
            else ["performed"]
            if denied
            else ["performed", "performed"]
        )
        stored_approvals = await store.browser_approvals_for_request(completed[0].id, scope=_SCOPE)
        assert len(stored_approvals) == (0 if budget_denied else 1)
        if not budget_denied:
            assert stored_approvals[0].state == ("denied" if denied else "consumed")
        else:
            assert completed[0].grant_id is not None
            grant = await AuthorityStore(settings).get(completed[0].grant_id, scope=_SCOPE)
            assert grant.status == "active"
        if not denied:
            assert completed[0].grant_id is not None
            grant = await AuthorityStore(settings).get(completed[0].grant_id, scope=_SCOPE)
            assert grant.status == "consumed"
    assert page.navigations == ["https://openrouter.ai/settings/credits"]
    assert page.snapshot_depths
    assert backend.closed
    assert page.closed
    assert await messaging.deliver_once() == (2 if purchase and not budget_denied else 1)
    assert final_report in transport.sent[-1].text
    if denied:
        assert "Execution failed" in transport.sent[-1].text
        assert "Agent report (unverified)" in transport.sent[-1].text
    assert task.title in transport.sent[-1].text
    if not purchase:
        assert len(transport.sent) == (3 if selection == "ambiguous" else 2)
    assert len(provider.requests) == foreground_count
    assert await dispatcher.worker_once(scope=_SCOPE) == []
    assert await messaging.deliver_once() == 0
