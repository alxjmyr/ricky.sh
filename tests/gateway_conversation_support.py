"""Shared gateway conversation fixtures and scripted providers."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pydantic import SecretStr

from ricky.config import (
    GatewayRouteSettings,
    GatewaySettings,
    MessagingRouteSettings,
    MessagingSettings,
    MessagingTransportSettings,
    RickySettings,
    TelegramAccountSettings,
)
from ricky.gateway.store import GatewayStore
from ricky.llm import (
    CompletionRequest,
    Message,
    MessageDone,
    StreamEvent,
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

_SCOPE = ProfileScope.create("personal")


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
