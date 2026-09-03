"""Shared offline fixtures for task-scoped delegated authority tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from ricky.authority.registry import AuthorityRegistry
from ricky.authority.types import AuthorityScope, DelegationGrant, GrantSource, source_text_digest
from ricky.capabilities import CapabilitySpec
from ricky.config import ExecutionSettings, MessagingSettings, RickySettings
from ricky.durable_tasks.scoped import ScopedDurableTaskStore
from ricky.durable_tasks.types import DurableTask
from ricky.executions.types import ExecutionRequest
from ricky.llm import CompletionRequest, Message, MessageDone, StreamEvent, ToolCallPart
from ricky.messaging.types import InboundMessage
from ricky.profiles import ProfileName, ProfileScope
from sandbox_support import (
    SandboxGuardrailEvaluator,
    SandboxOutcome,
    SandboxReservationEvaluator,
    SandboxReservationTool,
    SandboxReserveParams,
)

PRINCIPAL = "telegram:personal/owner-bot:sender-1"
CONVERSATION_ID = f"conversation_{uuid4().hex}"


def authority_registry() -> AuthorityRegistry:
    return AuthorityRegistry([SandboxReservationEvaluator()])


def install_sandbox_runtime(monkeypatch: Any, *, dispatch: Any = None) -> None:
    """Install the synthetic effect capability only inside an isolated test."""

    monkeypatch.setattr(
        "ricky.runtime.composition.delegable_effect_tools",
        lambda config: (
            [SandboxReservationTool(config, dispatch=dispatch)]
            if config.authority.enabled and "sandbox_reservation" in config.authority.capabilities
            else []
        ),
    )
    monkeypatch.setattr(
        "ricky.runtime.composition.built_in_guardrail_evaluators",
        lambda: (SandboxGuardrailEvaluator(),),
    )
    monkeypatch.setattr("ricky.authority.registry.default_authority_registry", authority_registry)
    monkeypatch.setattr("ricky.authority.compiler.default_authority_registry", authority_registry)
    from ricky.runtime import composition

    specs = (
        *(
            spec
            for spec in composition.CAPABILITY_SPECS
            if spec.id != "builtin.sandbox.reservation"
        ),
        CapabilitySpec(
            id="builtin.sandbox.reservation",
            owner="builtin",
            description="Test-only reversible reservation effect.",
            guardrail_schema_id="sandbox.reservation",
            authority_capability="sandbox_reservation",
        ),
    )
    monkeypatch.setattr(composition, "CAPABILITY_SPECS", specs)
    monkeypatch.setattr("ricky.gateway.conversations.CAPABILITY_SPECS", specs)
    original_owners = composition._external_tool_owners
    monkeypatch.setattr(
        composition,
        "_external_tool_owners",
        lambda tools: {
            name: owner
            for name, owner in original_owners(tools).items()
            if name != "sandbox_reserve"
        },
    )


class ScriptedProvider:
    """Emits one scripted assistant message per request."""

    name = "scripted"

    def __init__(self, responder: Any) -> None:
        self.responder = responder
        self.requests: list[CompletionRequest] = []
        self.closed = False

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        yield self.responder(request, len(self.requests) - 1)

    async def aclose(self) -> None:
        self.closed = True


def tool_call(name: str, args: dict[str, object], stage: int) -> MessageDone:
    return MessageDone(
        message=Message(
            role="assistant",
            content=[ToolCallPart(id=f"call_{stage}_{name}", name=name, args=args)],
        )
    )


def text(body: str) -> MessageDone:
    return MessageDone(message=Message.text("assistant", body))


def settings(
    tmp_path: Path,
    *,
    enabled: bool = True,
    max_effect_calls: int = 1,
    capability_effect_calls: int = 1,
    max_ttl_seconds: float = 86_400.0,
    capability_ttl_seconds: float = 86_400.0,
    max_financial_limit_minor: int = 0,
    currency: str | None = None,
    allowed_profiles: list[str] | None = None,
    principals: list[str] | None = None,
    capabilities: dict[str, Any] | None = None,
) -> RickySettings:
    capability_table: dict[str, Any] = (
        capabilities
        if capabilities is not None
        else {
            "sandbox_reservation": {
                "enabled": True,
                "max_effect_calls": capability_effect_calls,
                "max_ttl_seconds": capability_ttl_seconds,
                "max_financial_limit_minor": max_financial_limit_minor,
                "currency": currency,
                "allowed_profiles": allowed_profiles or ["shared", "personal"],
            }
        }
    )
    return RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": ".ricky",
            "providers": {"openrouter": {"default_model": "test-model"}},
            "google": {"accounts": {}},
            "memory": {"enabled": False},
            "workflow": {"enabled": False},
            "authority": {
                "enabled": enabled,
                "allowed_principals": principals if principals is not None else [PRINCIPAL],
                "default_ttl_seconds": min(3_600.0, max_ttl_seconds),
                "max_ttl_seconds": max_ttl_seconds,
                "max_effect_calls": max_effect_calls,
                "capabilities": capability_table,
            },
            "executions": ExecutionSettings(
                claim_seconds=10,
                heartbeat_seconds=0.05,
                concurrency=1,
                poll_seconds=0.01,
            ),
            "messaging": MessagingSettings.model_validate(
                {
                    "telegram_accounts": {
                        "personal/owner-bot": {"bot_token": "personal-token"},
                        "work/work-bot": {"bot_token": "work-token"},
                    },
                    "transports": {
                        "main": {"type": "telegram", "account": "personal/owner-bot"},
                        "work": {"type": "telegram", "account": "work/work-bot"},
                    },
                    "routes": {
                        "owner": {
                            "transport": "main",
                            "destination": "chat-owner",
                            "owner_profile": "personal",
                            "accepted_profiles": ["shared", "personal"],
                        },
                        "work-alerts": {
                            "transport": "work",
                            "destination": "chat-work",
                            "owner_profile": "work",
                            "accepted_profiles": ["shared", "work"],
                        },
                    },
                    "agent_routes": ["owner"],
                }
            ),
            "gateway": {
                "enabled": True,
                "routes": {
                    "owner": {
                        "provider": "openrouter",
                        "model": "test-model",
                        "primary_profile": "personal",
                    }
                },
            },
        }
    )


async def gateway_conversation(config: RickySettings) -> Any:
    """One real gateway conversation so `conversation:` routes resolve as in production."""

    from ricky.gateway.store import GatewayStore
    from ricky.gateway.types import ConversationKey

    store = GatewayStore(config)
    await store.initialize()
    key = ConversationKey(
        transport="telegram",
        account="personal/owner-bot",
        destination_id="chat-owner",
    )
    profile_scope = config.resolve_profile_scope("personal")
    existing = await store.get_active(key, scope=profile_scope)
    if existing is not None:
        return existing
    return await store.create(
        key=key,
        session_id=f"session_{uuid4().hex}",
        route_name="owner",
        provider="openrouter",
        model="test-model",
        profile_scope=profile_scope,
        project_root=None,
    )


def gateway_dispatcher(
    config: RickySettings, project_root: Path, *, provider_factory: Any = None
) -> Any:
    """The same conversation-aware dispatcher the gateway composes."""

    from ricky.authority.store import AuthorityStore
    from ricky.executions.dispatcher import ExecutionDispatcher
    from ricky.gateway.conversations import GatewayNotificationService
    from ricky.gateway.store import GatewayStore
    from ricky.notifications.routes import RoutePolicy

    routes = RoutePolicy(config, conversation_resolver=GatewayStore(config))
    return ExecutionDispatcher(
        config,
        project_root=project_root,
        routes=routes,
        notifications=GatewayNotificationService(config, routes=routes),
        authority=AuthorityStore(config),
        authority_registry=authority_registry(),
        provider_factory=provider_factory,
    )


async def durable_task(
    config: RickySettings, *, profile: ProfileName = "personal", mode: str = "agent"
) -> DurableTask:
    store = await ScopedDurableTaskStore.create(config, scope=ProfileScope.create(profile))
    return await store.create_task(
        title="Dinner reservation",
        objective="Reserve a table",
        closure_criteria="A confirmed reservation exists",
        execution_mode=mode,  # type: ignore[arg-type]
        authority="deterministic_user_command",
        executor_id="test",
    )


def inbound(
    body: str = "Book us a table at Restaurant A on the 27th between 6 and 8 PM.",
    *,
    status: str = "pending",
    sender_id: str = "sender-1",
) -> InboundMessage:
    return InboundMessage(
        id=f"inbound_{uuid4().hex}",
        transport="telegram",
        account="personal/owner-bot",
        update_id=uuid4().hex[:8],
        destination_id="chat-owner",
        sender_id=sender_id,
        platform_message_id="42",
        text=body,
        received_at=datetime.now(UTC),
        status=status,  # type: ignore[arg-type]
    )


def grant_source(message: InboundMessage | None = None) -> GrantSource:
    message = message or inbound()
    return GrantSource(
        principal_id=f"{message.transport}:{message.account}:{message.sender_id}",
        transport=message.transport,
        account=message.account,
        sender_id=message.sender_id,
        destination_id=message.destination_id,
        platform_message_id=message.platform_message_id,
        inbound_message_id=message.id,
        conversation_id=CONVERSATION_ID,
        text_digest=source_text_digest(message.text),
        text_snapshot=message.text,
        received_at=message.received_at,
    )


COMPLETE_CONSTRAINTS: dict[str, Any] = {
    "venue_id": "venue-a",
    "venue_name": "Restaurant A",
    "party_size": 2,
    "local_date": "2026-08-27",
    "window_start": "18:00",
    "window_end": "20:00",
    "timezone": "America/Chicago",
    "account_identity": "alex@example.com",
    "max_reservations": 1,
    "deposit_limit_minor": 0,
}

VALID_CALL: dict[str, Any] = {
    "venue_id": "venue-a",
    "party_size": 2,
    "local_date": "2026-08-27",
    "arrival_time": "18:30",
    "timezone": "America/Chicago",
    "account_identity": "alex@example.com",
    "deposit_minor": 0,
}


async def queue_contract_delegation(
    config: RickySettings,
    project_root: Path,
    dispatcher: Any,
    *,
    task: DurableTask,
    message: InboundMessage | None = None,
) -> tuple[DelegationGrant, ExecutionRequest]:
    """Persist one contract-bound grant and its linked request for management tests."""

    from ricky.authority.store import AuthorityStore

    del project_root
    conversation = await gateway_conversation(config)
    now = datetime.now(UTC)
    request_id = f"execution_{uuid4().hex}"
    contract_id = f"contract_{uuid4().hex}"
    contract_digest = uuid4().hex + uuid4().hex
    grant = DelegationGrant(
        id=f"grant_{uuid4().hex}",
        source=grant_source(message).model_copy(update={"conversation_id": conversation.id}),
        task_id=task.id,
        task_revision=task.revision,
        profile_scope=config.resolve_profile_scope("personal"),
        execution_request_id=None,
        contract_id=contract_id,
        contract_digest=contract_digest,
        scopes=(
            AuthorityScope(
                capability="sandbox_reservation",
                schema_id="sandbox.reservation",
                schema_version=1,
                constraints=dict(COMPLETE_CONSTRAINTS),
            ),
        ),
        summary="Bounded restaurant reservation.",
        effect_call_limit=1,
        issued_at=now,
        expires_at=now + timedelta(seconds=3_600),
        status="active",
        policy_digest=config.authority.digest(),
    )
    authority = AuthorityStore(config)
    await authority.initialize()
    profile_scope = config.resolve_profile_scope("personal")
    await authority.issue(grant, scope=profile_scope)
    request = ExecutionRequest(
        id=request_id,
        kind="ad_hoc",
        status="queued",
        goal="Book the requested table and report the confirmation.",
        contract_id=contract_id,
        contract_digest=contract_digest,
        task_id=task.id,
        task_revision=task.revision,
        profile_scope=profile_scope,
        source_conversation_id=conversation.id,
        source_message_id=grant.source.inbound_message_id,
        grant_id=grant.id,
        notification_route=f"conversation:{conversation.id}",
        request_key=f"contract-test:{uuid4().hex}",
        created_at=now,
    )
    await dispatcher.store.initialize()
    await dispatcher.store.submit(request, scope=profile_scope)
    attached = await authority.attach_execution(grant.id, request.id, scope=profile_scope)
    return attached, request


def ambiguous_dispatch(params: SandboxReserveParams) -> SandboxOutcome:
    del params
    return SandboxOutcome(
        disposition="in_doubt",
        reference=None,
        detail="the sandbox transport timed out after sending the booking",
    )
