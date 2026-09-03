"""Foreground conversation coordination and reduced runtime composition."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from pydantic import BaseModel

from ricky.agent.context_types import ContextReport
from ricky.agent.events import (
    AgentEvent,
    ContextCompactionFailedEvent,
    ContextCompactionFinishedEvent,
)
from ricky.agent.loop import AgentLoop
from ricky.agent.session import AgentSession
from ricky.authority.compiler import ContractAuthorityCompiler
from ricky.authority.store import AuthorityStore
from ricky.authority.tools import delegation_management_tools
from ricky.capabilities import (
    GuardrailIntakeSpec,
    GuardrailRegistry,
    build_capability_registry,
    capability_requires_project_root,
    registered_skill_owners,
    resolve_capability_policy,
    validate_capability_inventory,
    validate_capability_policy,
    validate_foreground_live_policy,
)
from ricky.capabilities.policy import policy_digest
from ricky.config import GatewayRouteSettings, RickySettings, find_project_root
from ricky.durable_tasks.scoped import ScopedDurableTaskStore
from ricky.durable_tasks.state_guard import DurableTaskStateGuard
from ricky.durable_tasks.tools import COORDINATION_TOOL_NAMES
from ricky.executions.compiler import CompileBinding, ExecutionContractCompiler
from ricky.executions.dispatcher import ExecutionDispatcher
from ricky.executions.store import ExecutionStoreError
from ricky.gateway.capability_use import (
    ForegroundAuthorizedTool,
    ForegroundCapabilityUseManager,
    PrepareCapabilityUseTool,
)
from ricky.gateway.context import (
    GatewayContextLoader,
    gateway_instructions,
    render_gateway_activity,
)
from ricky.gateway.store import GatewayStore
from ricky.gateway.tools import (
    gateway_capability_inventory_tools,
    gateway_control_descriptors,
    gateway_execution_tools,
)
from ricky.gateway.types import (
    Conversation,
    ConversationKey,
    GatewayActivity,
    GatewayCapabilityCatalog,
    GatewayCapabilityItem,
    GatewayProcessResult,
)
from ricky.jobs.registry import JobRegistry
from ricky.llm import Provider, TextPart, create_provider
from ricky.messaging.store import InboxLeaseError, MessagingStore
from ricky.messaging.types import InboundMessage, InboxClaim
from ricky.notifications import NotificationService
from ricky.notifications.routes import RoutePolicy
from ricky.notifications.store import NotificationStore
from ricky.notifications.types import (
    CorrelationRef,
    NotificationRecord,
    NotificationRequest,
)
from ricky.owned_operation import run_with_lease_heartbeat
from ricky.permissions import PermissionEngine, Policy, PolicyRule
from ricky.profiles import ProfileScope
from ricky.project_scope import ProjectScope
from ricky.protected_values import ResidentProtectedValueRegistry
from ricky.runtime.composition import (
    CAPABILITY_SPECS,
    CapabilityRuntime,
    SessionRuntime,
    build_capability_runtime,
)
from ricky.sessions import PersistentTurnService, SessionNotFoundError, SessionStore
from ricky.sessions.types import StoredSession, StoredTurn
from ricky.tools import StateGuardRegistry, Tool, ToolContext, ToolRegistry, ToolResult

_ALWAYS_EXCLUDED = {
    "run_shell",
    "notify_user",
    "start_workflow",
    "validate_workflow",
    # Delegable effects and generic execution controls are never exposed
    # directly. Source-bound gateway controls are composed separately.
    "start_named_job",
    "cancel_execution_request",
    "read_execution_request",
    "list_execution_requests",
    "delegate_task",
    "revoke_delegation",
    "list_delegations",
}


class GatewayConversationError(RuntimeError):
    """A bounded foreground message could not be handled safely."""


class GatewayNotificationService(NotificationService):
    """Add the trusted callback conversation to gateway execution results."""

    async def enqueue(
        self,
        request: NotificationRequest,
        *,
        scope: ProfileScope,
    ) -> NotificationRecord:
        if request.route.startswith("conversation:"):
            conversation_id = request.route.removeprefix("conversation:")
            if not any(
                item.kind == "conversation" and item.id == conversation_id
                for item in request.correlations
            ):
                request = request.model_copy(
                    update={
                        "correlations": [
                            *request.correlations,
                            CorrelationRef(
                                kind="conversation",
                                id=conversation_id,
                                profile_label=request.profile_label,
                            ),
                        ]
                    }
                )
        return await super().enqueue(request, scope=scope)


ProviderFactory = Callable[[str, RickySettings], Provider]
AgentEventSink = Callable[[AgentEvent], Awaitable[None] | None]


class _RevisionGuardedTool:
    """CAS guard for a task revision quoted by a notification reply."""

    def __init__(
        self,
        tool: Tool,
        *,
        store: Any,
        revisions: dict[str, int],
    ) -> None:
        self._tool = tool
        self._store = store
        self._revisions = revisions
        self.name = tool.name
        self.description = tool.description
        self.Params = tool.Params
        self.risk = tool.risk
        declared = cast(Any, tool)
        self.capability_id = declared.capability_id
        self.effect_kind = declared.effect_kind
        self.unattended = declared.unattended
        self.state_guard_id = declared.state_guard_id
        self.review_mode = getattr(declared, "review_mode", "policy")
        for name in (
            "Result",
            "workflow_only",
            "deferred_until_artifact",
            "result_is_bounded",
        ):
            if hasattr(tool, name):
                setattr(self, name, getattr(tool, name))

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        task_id = getattr(params, "task_id", None)
        if isinstance(task_id, str) and task_id in self._revisions:
            current = await self._store.get_task(task_id)
            expected = self._revisions[task_id]
            if current.revision != expected:
                return ToolResult(
                    content=(
                        f"stale task revision for {task_id}: expected {expected}, "
                        f"current {current.revision}; reload before applying the answer"
                    ),
                    is_error=True,
                )
        result = await self._tool.run(params, ctx)
        if not result.is_error and isinstance(task_id, str) and isinstance(result.data, dict):
            task = result.data.get("task")
            revision = task.get("revision") if isinstance(task, dict) else None
            if isinstance(revision, int):
                self._revisions[task_id] = revision
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tool, name)


@asynccontextmanager
async def build_gateway_runtime(
    settings: RickySettings,
    *,
    session: AgentSession,
    conversation: Conversation,
    inbound: InboundMessage,
    activity: GatewayActivity,
    dispatcher: ExecutionDispatcher | None = None,
    provider: Provider | None = None,
    provider_factory: ProviderFactory | None = None,
    protected_value_registry: ResidentProtectedValueRegistry | None = None,
) -> AsyncIterator[SessionRuntime]:
    """Build one reduced foreground runtime with no interactive prompt path."""

    route_settings = settings.resolve_profile_runtime_settings(conversation.profile_scope)
    route = route_settings.gateway.routes[conversation.route_name]
    project_scope = _project_scope(route)
    project_root = project_scope.root
    owned_provider = provider or (
        provider_factory(session.provider, route_settings)
        if provider_factory is not None
        else create_provider(session.provider, route_settings)
    )
    try:
        async with build_capability_runtime(
            settings,
            session=session,
            project_root=project_root,
            project_scope=project_scope,
            protected_value_registry=protected_value_registry,
        ) as capabilities:
            authority_store = (
                AuthorityStore(route_settings) if route_settings.authority.enabled else None
            )
            dispatcher = dispatcher or _gateway_dispatcher(
                route_settings,
                project_root=project_root,
                project_scope=project_scope,
                gateway=GatewayStore(settings),
                authority=authority_store,
                protected_value_registry=protected_value_registry,
            )
            compiler = ExecutionContractCompiler(
                route_settings,
                capabilities=capabilities.capability_registry,
                guardrails=capabilities.guardrail_registry,
                skills=capabilities.skill_registry,
                route=route,
                binding=CompileBinding(
                    principal_id=(f"{inbound.transport}:{inbound.account}:{inbound.sender_id}"),
                    conversation_id=conversation.id,
                    route_name=conversation.route_name,
                    notification_route=f"conversation:{conversation.id}",
                    profile_scope=conversation.profile_scope,
                    provider=route.provider,
                    model=route.model,
                    project_root_ref=str(project_root.resolve()) if project_root else None,
                ),
                store=dispatcher.store,
                protected_values=capabilities.protected_values,
            )
            control_tools = gateway_execution_tools(
                dispatcher,
                conversation=conversation,
                inbound=inbound,
                compiler=compiler,
                contract_authority=(
                    ContractAuthorityCompiler(route_settings, store=authority_store)
                    if authority_store is not None
                    else None
                ),
                capability_ids={
                    "builtin.automation.mutate",
                    "builtin.automation.read",
                },
                project_scope=project_scope,
            )
            management_tools: list[Tool] = []
            if authority_store is not None:
                management_tools = delegation_management_tools(
                    dispatcher,
                    authority_store,
                    conversation_id=conversation.id,
                )
            foreground_registry = build_capability_registry(
                gateway_capability_inventory_tools(
                    capabilities.tools,
                    control_tools,
                    management_tools,
                    gateway_control_descriptors(),
                ),
                capabilities.skill_registry,
                capability_specs=CAPABILITY_SPECS,
                skill_owners=registered_skill_owners(capabilities.capability_registry),
                state_guards=StateGuardRegistry(
                    [DurableTaskStateGuard(capabilities.durable_tasks)]
                ),
            )
            diagnostics = [
                *validate_capability_inventory(
                    foreground_registry,
                    capabilities.guardrail_registry,
                ),
                *validate_capability_policy(
                    foreground_registry,
                    capabilities.guardrail_registry,
                    route_settings.agents.gateway_foreground,
                    route=route,
                ),
                *validate_foreground_live_policy(
                    foreground_registry,
                    route_settings.agents.gateway_foreground,
                    route=route,
                ),
            ]
            errors = [item for item in diagnostics if item.severity == "error"]
            if errors:
                raise GatewayConversationError(
                    "; ".join(f"{item.capability_id}: {item.message}" for item in errors)
                )
            foreground_decisions = resolve_capability_policy(
                foreground_registry,
                route_settings.agents.gateway_foreground,
                route=route,
            )
            allowed_capability_ids = {
                item.capability_id for item in foreground_decisions if item.eligible
            }
            tools = _foreground_tools(capabilities, allowed_capability_ids, project_root)
            tools.extend(
                tool
                for tool in (*control_tools, *management_tools)
                if (
                    (definition := foreground_registry.for_resource("tool", tool.name)) is not None
                    and definition.id in allowed_capability_ids
                )
            )
            decisions_by_id = {item.capability_id: item for item in foreground_decisions}
            selected_by_name = {tool.name: tool for tool in tools}
            capability_use = ForegroundCapabilityUseManager(
                route_settings,
                registry=foreground_registry,
                guardrails=capabilities.guardrail_registry,
                decisions=decisions_by_id,
                tools=selected_by_name,
                route=route,
                conversation=conversation,
                inbound=inbound,
                store=dispatcher.store,
            )
            live_review_needed = any(
                decision.eligible
                and (decision.confirmation_required or decision.guardrail_required)
                and any(
                    resource.kind == "tool" and resource.id in selected_by_name
                    for resource in foreground_registry.require(decision.capability_id).resources
                )
                for decision in foreground_decisions
            )
            if live_review_needed and ("builtin.authorization.review" in allowed_capability_ids):
                tools.append(cast(Tool, PrepareCapabilityUseTool(capability_use)))
            tools = [
                cast(Tool, ForegroundAuthorizedTool(tool, capability_use))
                if tool.name != "prepare_capability_use"
                and (definition := foreground_registry.for_resource("tool", tool.name)) is not None
                and (
                    decisions_by_id[definition.id].confirmation_required
                    or decisions_by_id[definition.id].guardrail_required
                )
                else tool
                for tool in tools
            ]
            revisions = {
                record.id: record.revision
                for record in activity.records
                if record.kind == "task" and record.revision is not None
            }
            if revisions:
                tools = [
                    cast(
                        Tool,
                        _RevisionGuardedTool(
                            tool,
                            store=capabilities.durable_tasks,
                            revisions=revisions,
                        ),
                    )
                    if tool.name in COORDINATION_TOOL_NAMES
                    else tool
                    for tool in tools
                ]
            registry = ToolRegistry(tools)
            rules = [
                PolicyRule(
                    tool_name=tool.name,
                    decision="allow",
                    reason="configured foreground internal control capability",
                )
                for tool in tools
                if tool.risk == "mutating"
            ]
            permissions = PermissionEngine(
                Policy(
                    rules=rules,
                    read_only_default="allow",
                    mutating_default="deny",
                    destructive_default="deny",
                )
            )
            selected = replace(
                capabilities,
                tools=tuple(tools),
                chat_registry=registry,
                full_registry=registry,
                permission_engine=permissions,
                capability_registry=foreground_registry,
            )
            loop = AgentLoop(
                provider=owned_provider,
                registry=registry,
                settings=route_settings,
                permission_engine=permissions,
                cwd=project_root,
                skill_registry=(
                    capabilities.skill_registry
                    if "builtin.skill.use" in allowed_capability_ids
                    else None
                ),
                memory=capabilities.memory,
                workflow_registry=None,
                artifact_store=capabilities.session_artifacts,
            )
            yield SessionRuntime(
                provider=owned_provider,
                capabilities=selected,
                agent_loop=loop,
                workflow_runner=None,
            )
    finally:
        await owned_provider.aclose()


class ConversationCoordinator:
    """Claim one authenticated message and commit exactly one foreground outcome."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        gateway: GatewayStore | None = None,
        messaging: MessagingStore | None = None,
        notifications: NotificationStore | None = None,
        sessions: SessionStore | None = None,
        context_loader: GatewayContextLoader | None = None,
        provider_factory: ProviderFactory | None = None,
        event_sink: AgentEventSink | None = None,
        dispatcher: ExecutionDispatcher | None = None,
        protected_value_registry: ResidentProtectedValueRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.profile_scope = settings.resolve_profile_scope(
            settings.profiles.default,
            access_profiles=settings.profiles.enabled,
        )
        self.gateway = gateway or GatewayStore(settings)
        self.messaging = messaging or MessagingStore(settings)
        self.notifications = notifications or NotificationStore(settings)
        self.sessions = sessions or SessionStore(settings)
        self.context_loader = context_loader or GatewayContextLoader(
            settings,
            gateway=self.gateway,
            messaging=self.messaging,
            notifications=self.notifications,
        )
        self.provider_factory = provider_factory
        self.event_sink = event_sink
        self.protected_value_registry = protected_value_registry
        self._locks: dict[str, asyncio.Lock] = {}
        self._initialize_lock = asyncio.Lock()
        self._initialized = False
        self.dispatcher = dispatcher or _gateway_dispatcher(
            settings,
            project_root=None,
            gateway=self.gateway,
            protected_value_registry=protected_value_registry,
        )
        self.execution_store = self.dispatcher.store

    def bind_dispatcher(self, dispatcher: ExecutionDispatcher) -> None:
        """Bind every same-process execution control to the service-owned dispatcher."""

        self.dispatcher = dispatcher
        self.execution_store = dispatcher.store

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await asyncio.gather(
                self.gateway.initialize(),
                self.messaging.initialize(),
                self.notifications.initialize(),
                self.sessions.initialize(),
                self.execution_store.initialize(),
            )
            self._initialized = True

    async def process(self, message_id: str) -> GatewayProcessResult:
        """Process one pending accepted message; rejected input never reaches a provider."""

        # Authentication is decided by the transport adapter during ingestion. Read
        # only that durable verdict before initializing any gateway internals so a
        # rejected sender cannot reach sessions, tasks, executions, notifications,
        # runtime composition, or a provider, even through a direct API call.
        await self.messaging.initialize()
        inbound = await self.messaging.get_inbox(message_id)
        if inbound.status == "rejected":
            raise GatewayConversationError("rejected inbox messages cannot enter the gateway")
        await self.initialize()
        existing_result = await self.gateway.get_result(
            message_id,
            scope=self.profile_scope,
        )
        if existing_result is not None:
            if existing_result.status != "running":
                inbox_status = "uncertain" if existing_result.status == "uncertain" else "processed"
                # A live worker may still own the narrow finish-result ->
                # finish-inbox gap. Its fenced settlement remains authoritative.
                with suppress(InboxLeaseError):
                    await self.messaging.settle_inbox_from_terminal_result(
                        message_id,
                        status=inbox_status,
                    )
            return GatewayProcessResult(
                message_id=message_id,
                conversation_id=existing_result.conversation_id,
                session_id=existing_result.session_id,
                status=(
                    "uncertain"
                    if existing_result.status in {"running", "uncertain"}
                    else "processed"
                ),
                response_outbox_id=existing_result.response_outbox_id,
            )
        key = _key(inbound)
        lock = self._locks.setdefault(key.digest(), asyncio.Lock())
        async with lock:
            return await self._process_locked(inbound, key)

    async def _process_locked(
        self,
        inbound: InboundMessage,
        key: ConversationKey,
    ) -> GatewayProcessResult:
        claim = await self.messaging.claim_inbox(
            inbound.id,
            owner=f"gateway_{uuid4().hex}",
            lease_seconds=min(
                3_600,
                max(
                    self.settings.messaging.lease_seconds,
                    math.ceil(self.settings.sessions.turn_wall_seconds) + 30,
                ),
            ),
        )
        conversation: Conversation | None = None
        base_revision: int | None = None
        try:
            rotates = inbound.text.strip() == "/new"
            conversation = (
                await self._resolve_rotation(key, inbound.id)
                if rotates
                else await self._resolve_or_create(key, allow_route_drift=True)
            )
            if not rotates:
                try:
                    self._validate_route_snapshot(conversation)
                except GatewayConversationError as exc:
                    base_revision = conversation.revision
                    await self.gateway.begin_result(
                        message_id=inbound.id,
                        conversation_id=conversation.id,
                        session_id=conversation.session_id,
                        scope=conversation.profile_scope,
                    )
                    message = str(exc)
                    outbox_id = await self._enqueue_response(
                        inbound,
                        conversation,
                        f"I can't safely continue this conversation: {message}",
                    )
                    await self.gateway.finish_result(
                        scope=conversation.profile_scope,
                        message_id=inbound.id,
                        conversation_id=conversation.id,
                        expected_conversation_revision=base_revision,
                        status="failed",
                        session_revision=(
                            await self.sessions.get(
                                conversation.session_id,
                                scope=conversation.profile_scope,
                            )
                        ).revision,
                        response_outbox_id=outbox_id,
                        error=message,
                    )
                    await self.messaging.finish_inbox(claim, status="processed")
                    return GatewayProcessResult(
                        message_id=inbound.id,
                        conversation_id=conversation.id,
                        session_id=conversation.session_id,
                        status="processed",
                        response_outbox_id=outbox_id,
                    )
            base_revision = conversation.revision
            await self.gateway.begin_result(
                message_id=inbound.id,
                conversation_id=conversation.id,
                session_id=conversation.session_id,
                scope=conversation.profile_scope,
            )
            response, session_revision = await self._respond(inbound, conversation)
            outbox_id = await self._enqueue_response(inbound, conversation, response)
            await self.gateway.finish_result(
                scope=conversation.profile_scope,
                message_id=inbound.id,
                conversation_id=conversation.id,
                expected_conversation_revision=base_revision,
                status="committed",
                session_revision=session_revision,
                response_outbox_id=outbox_id,
                error=None,
            )
            await self.messaging.finish_inbox(claim, status="processed")
            return GatewayProcessResult(
                message_id=inbound.id,
                conversation_id=conversation.id,
                session_id=conversation.session_id,
                status="processed",
                response_outbox_id=outbox_id,
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self._settle_uncertain(
                    claim,
                    inbound,
                    conversation,
                    base_revision,
                    "gateway processing was cancelled",
                )
            )
            raise
        except BaseException as exc:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                await self._settle_uncertain(
                    claim,
                    inbound,
                    conversation,
                    base_revision,
                    "gateway processing was cancelled",
                )
                raise asyncio.CancelledError from exc
            if conversation is None or base_revision is None:
                await self._finish_claim_uncertain(claim)
                raise
            stored = await self.sessions.get(
                conversation.session_id,
                scope=conversation.profile_scope,
            )
            if stored.status == "uncertain":
                await self._settle_uncertain(
                    claim,
                    inbound,
                    conversation,
                    base_revision,
                    "foreground turn became uncertain",
                )
                return GatewayProcessResult(
                    message_id=inbound.id,
                    conversation_id=conversation.id,
                    session_id=conversation.session_id,
                    status="uncertain",
                )
            message = _bounded_error(exc)
            outbox_id = await self._enqueue_response(
                inbound,
                conversation,
                f"I couldn't complete that foreground turn safely: {message}",
            )
            await self.gateway.finish_result(
                scope=conversation.profile_scope,
                message_id=inbound.id,
                conversation_id=conversation.id,
                expected_conversation_revision=base_revision,
                status="failed",
                session_revision=stored.revision,
                response_outbox_id=outbox_id,
                error=message,
            )
            await self.messaging.finish_inbox(claim, status="processed")
            return GatewayProcessResult(
                message_id=inbound.id,
                conversation_id=conversation.id,
                session_id=conversation.session_id,
                status="processed",
                response_outbox_id=outbox_id,
            )

    async def _respond(
        self,
        inbound: InboundMessage,
        conversation: Conversation,
    ) -> tuple[str, int]:
        command = inbound.text.strip()
        command_name = command.split(maxsplit=1)[0] if command else ""
        if command == "/help":
            return (
                "Gateway commands: /new, /compact, /context, /status, /cancel ID, "
                "/approve APPROVAL CODE, /deny APPROVAL CODE, "
                "/reconcile TRANSACTION performed|not_performed NOTE, /help",
                (
                    await self.sessions.get(
                        conversation.session_id,
                        scope=conversation.profile_scope,
                    )
                ).revision,
            )
        if command == "/status":
            return await self._status(conversation), (
                await self.sessions.get(
                    conversation.session_id,
                    scope=conversation.profile_scope,
                )
            ).revision
        if command_name == "/cancel":
            return await self._cancel(command, conversation), (
                await self.sessions.get(
                    conversation.session_id,
                    scope=conversation.profile_scope,
                )
            ).revision
        if command_name == "/approve":
            return await self._browser_decision(command, inbound, conversation, approve=True), (
                await self.sessions.get(
                    conversation.session_id,
                    scope=conversation.profile_scope,
                )
            ).revision
        if command_name == "/deny":
            return await self._browser_decision(command, inbound, conversation, approve=False), (
                await self.sessions.get(
                    conversation.session_id,
                    scope=conversation.profile_scope,
                )
            ).revision
        if command_name == "/reconcile":
            return await self._reconcile_browser_transaction(command, inbound, conversation), (
                await self.sessions.get(
                    conversation.session_id,
                    scope=conversation.profile_scope,
                )
            ).revision
        if command == "/new":
            return (
                "Started a new conversation session. The previous session remains archived.",
                0,
            )
        if command == "/compact":
            return await self._compact(inbound, conversation)
        if command == "/context":
            return await self._context(inbound, conversation)
        if command.startswith("/"):
            return (
                "Unknown gateway command. Use /help for the exact command list.",
                (
                    await self.sessions.get(
                        conversation.session_id,
                        scope=conversation.profile_scope,
                    )
                ).revision,
            )
        if _is_bare_browser_decision(command) and await self._has_pending_browser_approval(
            conversation
        ):
            return (
                "Browser approvals require the exact source-bound command "
                "/approve APPROVAL CODE or /deny APPROVAL CODE.",
                (
                    await self.sessions.get(
                        conversation.session_id,
                        scope=conversation.profile_scope,
                    )
                ).revision,
            )

        activity = await self.context_loader.load(inbound, conversation)
        route = self.settings.gateway.routes[conversation.route_name]
        route_settings = self.settings.resolve_profile_runtime_settings(conversation.profile_scope)
        project_root = _project_root(route)
        preview = (
            await self.sessions.get(
                conversation.session_id,
                scope=conversation.profile_scope,
            )
        ).session.model_copy(deep=True)
        async with build_gateway_runtime(
            self.settings,
            session=preview,
            conversation=conversation,
            inbound=inbound,
            activity=activity,
            dispatcher=self.dispatcher,
            provider_factory=self.provider_factory,
            protected_value_registry=getattr(self, "protected_value_registry", None),
        ) as runtime:

            @asynccontextmanager
            async def runtime_builder(
                settings: RickySettings, **kwargs: Any
            ) -> AsyncIterator[SessionRuntime]:
                del settings, kwargs
                yield runtime

            catalog = await _gateway_capability_catalog(
                route_settings,
                runtime=runtime.capabilities,
                project_root=project_root,
                route=route,
            )
            service = PersistentTurnService(
                route_settings,
                self.sessions,
                profile_scope=conversation.profile_scope,
                runtime_builder=runtime_builder,
                project_root=project_root,
            )
            stored = await service.run_turn(
                conversation.session_id,
                inbound.text,
                owner=f"gateway-turn:{inbound.id}",
                inbound_ref=inbound.id,
                extra_system_sections={
                    "gateway": gateway_instructions(
                        conversation,
                        inbound,
                        catalog=catalog,
                        capabilities=[item.name for item in catalog.ad_hoc_capabilities],
                    ),
                    "gateway_activity": render_gateway_activity(
                        activity,
                        char_limit=self.settings.gateway.activity_char_limit,
                    ),
                },
                event_sink=self._forward_event,
            )
        response = _final_message(stored.session)
        if response is None:
            raise GatewayConversationError("foreground turn produced no assistant response")
        return response, stored.revision

    async def _has_pending_browser_approval(self, conversation: Conversation) -> bool:
        linked = await self.dispatcher.store.list_by_conversation(
            conversation.id,
            scope=conversation.profile_scope,
            statuses=(
                "awaiting_protected_approval",
                "awaiting_transaction_approval",
            ),
            limit=100,
        )
        for request in linked:
            approvals = await self.dispatcher.store.browser_approvals_for_request(
                request.id,
                scope=conversation.profile_scope,
            )
            if any(approval.state == "pending" for approval in approvals):
                return True
        return False

    async def _context(
        self,
        inbound: InboundMessage,
        conversation: Conversation,
    ) -> tuple[str, int]:
        """Inspect the exact foreground session context without a model request."""

        stored = await self.sessions.get(
            conversation.session_id,
            scope=conversation.profile_scope,
        )
        activity = await self.context_loader.load(inbound, conversation)
        route = self.settings.gateway.routes[conversation.route_name]
        route_settings = self.settings.resolve_profile_runtime_settings(conversation.profile_scope)
        project_root = _project_root(route)
        session = stored.session.model_copy(deep=True)
        async with build_gateway_runtime(
            self.settings,
            session=session,
            conversation=conversation,
            inbound=inbound,
            activity=activity,
            dispatcher=self.dispatcher,
            provider_factory=self.provider_factory,
            protected_value_registry=getattr(self, "protected_value_registry", None),
        ) as runtime:
            catalog = await _gateway_capability_catalog(
                route_settings,
                runtime=runtime.capabilities,
                project_root=project_root,
                route=route,
            )
            report = runtime.agent_loop.inspect_context(
                session,
                extra_system_sections={
                    "gateway": gateway_instructions(
                        conversation,
                        inbound,
                        catalog=catalog,
                        capabilities=[item.name for item in catalog.ad_hoc_capabilities],
                    ),
                    "gateway_activity": render_gateway_activity(
                        activity,
                        char_limit=self.settings.gateway.activity_char_limit,
                    ),
                },
            )
        return _render_context_report(session, stored.revision, report), stored.revision

    async def _compact(
        self,
        inbound: InboundMessage,
        conversation: Conversation,
    ) -> tuple[str, int]:
        """Persist one manual compaction under the session fence."""

        lease = await self.sessions.acquire(
            conversation.session_id,
            owner=f"gateway-compact:{inbound.id}",
            scope=conversation.profile_scope,
            lease_seconds=self.settings.sessions.lease_seconds,
        )
        turn: StoredTurn | None = None
        try:
            stored = await self.sessions.get(
                conversation.session_id,
                scope=conversation.profile_scope,
            )
            session = stored.session.model_copy(deep=True)
            session.permission_grants.clear()
            turn = StoredTurn(
                id=f"turn_{uuid4().hex}",
                session_id=session.id,
                profile_label=conversation.profile_scope.label(),
                inbound_ref=inbound.id,
                base_revision=stored.revision,
                status="running",
                started_at=datetime.now(UTC),
            )
            await self.sessions.begin_turn(lease, turn)

            async def compact_owned() -> tuple[str, int]:
                async with asyncio.timeout(self.settings.sessions.turn_wall_seconds):
                    async with build_gateway_runtime(
                        self.settings,
                        session=session,
                        conversation=conversation,
                        inbound=inbound,
                        activity=GatewayActivity(profile_label=conversation.profile_scope.label()),
                        dispatcher=self.dispatcher,
                        provider_factory=self.provider_factory,
                        protected_value_registry=getattr(self, "protected_value_registry", None),
                    ) as runtime:
                        session.active_task_leases.clear()
                        finished: ContextCompactionFinishedEvent | None = None
                        failed: ContextCompactionFailedEvent | None = None
                        async for event in runtime.agent_loop.compact_context(session):
                            await self._forward_event(event)
                            if isinstance(event, ContextCompactionFinishedEvent):
                                finished = event
                            elif isinstance(event, ContextCompactionFailedEvent):
                                failed = event
                        if finished is None:
                            message = (
                                failed.message
                                if failed is not None
                                else "compaction ended without a terminal event"
                            )
                            await self.sessions.fail_turn(
                                lease,
                                turn.id,
                                message,
                                uncertain=False,
                            )
                            return (
                                f"Context compaction did not change the session: {message}",
                                stored.revision,
                            )
                        committed = await self.sessions.commit(
                            lease,
                            stored.revision,
                            session,
                            turn,
                        )
                        # Publish the cleared session snapshot before releasing
                        # its durable task rows. A failed compaction therefore
                        # cannot leave stored lease claims that were already removed.
                        await runtime.durable_tasks.release_session_leases(session.id)
                        return (
                            f"Context compacted into checkpoint {finished.checkpoint_id}.",
                            committed.revision,
                        )

            return await run_with_lease_heartbeat(
                compact_owned(),
                lease=lease,
                renew=self.sessions.renew,
                interval_seconds=max(0.1, self.settings.sessions.lease_seconds / 3),
            )
        except BaseException as exc:
            if turn is not None:
                with suppress(Exception):
                    await self.sessions.fail_turn(
                        lease,
                        turn.id,
                        _bounded_error(exc),
                        uncertain=False,
                    )
            raise
        finally:
            with suppress(Exception):
                await self.sessions.release(lease)

    async def _forward_event(self, event: AgentEvent) -> None:
        """Forward observer events without letting observation affect a turn."""

        if self.event_sink is None:
            return
        try:
            result = self.event_sink(event)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            # Preserve cancellation of the foreground operation itself, while a
            # sink that independently cancels remains an observational failure.
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
        except Exception:
            return

    async def _resolve_or_create(
        self,
        key: ConversationKey,
        *,
        allow_route_drift: bool = False,
        created_for_inbound_message_id: str | None = None,
    ) -> Conversation:
        existing = await self.gateway.get_active(key, scope=self.profile_scope)
        if existing is not None:
            if not allow_route_drift:
                self._validate_route_snapshot(existing)
            return existing
        route_name, route = _route_for(self.settings, key)
        route_settings = self.settings.resolve_profile_runtime_settings(route.profile_scope())
        project_root = _project_root(route)
        session = AgentSession.create(
            self.settings,
            profile_scope=route.profile_scope(),
            provider=route.provider,
            model=route.model,
        )
        await self.sessions.create(session, scope=session.profile_scope)
        return await self.gateway.create(
            key=key,
            session_id=session.id,
            route_name=route_name,
            provider=session.provider,
            model=session.model,
            profile_scope=session.profile_scope,
            project_root=str(project_root) if project_root is not None else None,
            route_policy_digest=policy_digest(
                route_settings.agents.gateway_foreground,
                route,
            ),
            created_for_inbound_message_id=created_for_inbound_message_id,
        )

    def _validate_route_snapshot(self, conversation: Conversation) -> None:
        route = self.settings.gateway.routes.get(conversation.route_name)
        if route is None:
            raise GatewayConversationError(
                f"gateway route {conversation.route_name!r} was removed; send /new to continue"
            )
        project_root = _project_root(route)
        current_root = str(project_root) if project_root is not None else None
        route_settings = self.settings.resolve_profile_runtime_settings(route.profile_scope())
        current_digest = policy_digest(route_settings.agents.gateway_foreground, route)
        drifted = (
            conversation.provider != route.provider
            or conversation.model != route.model
            or conversation.profile_scope != route.profile_scope()
            or conversation.project_root != current_root
            or conversation.route_policy_digest is None
            or conversation.route_policy_digest != current_digest
        )
        if drifted:
            raise GatewayConversationError(
                "gateway route or capability policy changed for this conversation; "
                "send /new to start under the current configuration"
            )

    async def _rotate(
        self,
        conversation: Conversation,
        key: ConversationKey,
        inbound_message_id: str,
    ) -> Conversation:
        stored = await self.sessions.get(
            conversation.session_id,
            scope=conversation.profile_scope,
        )
        await self.gateway.archive(
            conversation.id,
            scope=conversation.profile_scope,
            expected_revision=conversation.revision,
            for_inbound_message_id=inbound_message_id,
        )
        return await self._resume_rotation(conversation, stored, key, inbound_message_id)

    async def _resolve_rotation(
        self,
        key: ConversationKey,
        inbound_message_id: str,
    ) -> Conversation:
        active = await self.gateway.get_active(key, scope=self.profile_scope)
        if active is not None:
            if active.created_for_inbound_message_id == inbound_message_id:
                return active
            return await self._rotate(active, key, inbound_message_id)
        source = await self.gateway.find_rotation_source(
            key,
            inbound_message_id,
            scope=self.profile_scope,
        )
        if source is None:
            return await self._resolve_or_create(
                key,
                created_for_inbound_message_id=inbound_message_id,
            )
        stored = await self.sessions.get(source.session_id, scope=source.profile_scope)
        return await self._resume_rotation(source, stored, key, inbound_message_id)

    async def _resume_rotation(
        self,
        source: Conversation,
        source_session: StoredSession,
        key: ConversationKey,
        inbound_message_id: str,
    ) -> Conversation:
        if source_session.status == "active":
            await self.sessions.archive(
                source.session_id,
                source_session.revision,
                scope=source.profile_scope,
            )
        elif source_session.status != "archived":
            raise GatewayConversationError(
                "cannot rotate a conversation whose prior session is uncertain"
            )

        existing = await self.gateway.get_active(key, scope=self.profile_scope)
        if existing is not None:
            if existing.created_for_inbound_message_id != inbound_message_id:
                raise GatewayConversationError("another conversation became active during /new")
            return existing

        route_name, route = _route_for(self.settings, key)
        route_settings = self.settings.resolve_profile_runtime_settings(route.profile_scope())
        project_root = _project_root(route)
        session_id = _rotation_session_id(key, inbound_message_id)
        session = AgentSession.create(
            self.settings,
            profile_scope=route.profile_scope(),
            provider=route.provider,
            model=route.model,
        ).model_copy(update={"id": session_id})
        try:
            replacement = await self.sessions.get(session_id, scope=session.profile_scope)
        except SessionNotFoundError:
            await self.sessions.create(session, scope=session.profile_scope)
        else:
            if (
                replacement.status != "active"
                or replacement.session.provider != session.provider
                or replacement.session.model != session.model
                or replacement.session.profile_scope != session.profile_scope
                or replacement.session.history
            ):
                raise GatewayConversationError("stored /new replacement session is inconsistent")
            session = replacement.session
        return await self.gateway.create(
            key=key,
            session_id=session.id,
            route_name=route_name,
            provider=session.provider,
            model=session.model,
            profile_scope=session.profile_scope,
            project_root=str(project_root) if project_root is not None else None,
            route_policy_digest=policy_digest(
                route_settings.agents.gateway_foreground,
                route,
            ),
            created_for_inbound_message_id=inbound_message_id,
        )

    async def _status(self, conversation: Conversation) -> str:
        dispatcher = self.dispatcher
        await dispatcher.store.initialize()
        linked = await dispatcher.store.list_by_conversation(
            conversation.id,
            scope=conversation.profile_scope,
            limit=100,
        )
        lines: list[str] = []
        seen_tasks: set[str] = set()
        for item in linked:
            if item.task_id is None or item.task_id in seen_tasks:
                continue
            seen_tasks.add(item.task_id)
            task_store = await ScopedDurableTaskStore.create(
                self.settings, scope=item.profile_scope
            )
            task = await task_store.get_task(item.task_id)
            if task.status in {"open", "in_progress", "waiting", "blocked"}:
                lines.append(f"task {task.id} {task.status} rev={task.revision}: {task.title}")
        active = [
            item
            for item in linked
            if item.status
            in {
                "queued",
                "claimed",
                "running",
                "awaiting_protected_approval",
                "awaiting_transaction_approval",
                "cancel_requested",
                "blocked",
            }
        ]
        for item in active:
            lines.append(f"execution {item.id} {item.status} task={item.task_id or '-'}")
            if item.status in {
                "awaiting_protected_approval",
                "awaiting_transaction_approval",
            }:
                approvals = await dispatcher.store.browser_approvals_for_request(
                    item.id,
                    scope=conversation.profile_scope,
                )
                if approvals:
                    approval = approvals[-1]
                    lines.append(
                        f"  approval {approval.id} {approval.state} "
                        f"expires={approval.expires_at.isoformat()} "
                        f"review={approval.review_digest[:12]}"
                    )
        if not lines:
            return "No active tasks or executions are linked to this conversation."
        return "\n".join(lines)

    async def _cancel(self, command: str, conversation: Conversation) -> str:
        pieces = command.split()
        if len(pieces) != 2:
            return "Usage: /cancel execution_<id>"
        dispatcher = self.dispatcher
        current = await dispatcher.read_execution_request(
            pieces[1], scope=conversation.profile_scope
        )
        if current.source_conversation_id != conversation.id:
            return "That execution is not controlled by this conversation."
        result = await dispatcher.cancel_execution_request(
            current.id, scope=conversation.profile_scope
        )
        return f"{result.id}: {result.status}"

    async def _browser_decision(
        self,
        command: str,
        inbound: InboundMessage,
        conversation: Conversation,
        *,
        approve: bool,
    ) -> str:
        pieces = command.split()
        verb = "approve" if approve else "deny"
        if len(pieces) != 3 or pieces[0] != f"/{verb}":
            return f"Usage: /{verb} browser_<approval-id> <one-time-code>"
        principal = f"{inbound.transport}:{inbound.account}:{inbound.sender_id}"
        try:
            approval = await self.dispatcher.decide_browser_approval(
                pieces[1],
                scope=conversation.profile_scope,
                approve=approve,
                principal_id=principal,
                conversation_id=conversation.id,
                source_message_id=inbound.id,
                code=pieces[2],
            )
        except ExecutionStoreError:
            return (
                "That browser approval could not be applied. Check the exact approval ID "
                "and one-time code from this conversation, then try again."
            )
        outcome = (
            "will revalidate before commit"
            if approval.state == "approved"
            else "will resume without committing that occurrence"
        )
        return f"{approval.id}: {approval.state}; execution {approval.request_id} {outcome}"

    async def _reconcile_browser_transaction(
        self,
        command: str,
        inbound: InboundMessage,
        conversation: Conversation,
    ) -> str:
        pieces = command.split(maxsplit=3)
        if len(pieces) != 4 or pieces[2] not in {"performed", "not_performed"}:
            return (
                "Usage: /reconcile browser_transaction_<id> performed|not_performed <operator note>"
            )
        disposition = (
            "confirmed_completed" if pieces[2] == "performed" else "confirmed_not_completed"
        )
        principal = f"{inbound.transport}:{inbound.account}:{inbound.sender_id}"
        try:
            request = await self.dispatcher.reconcile_browser_transaction(
                pieces[1],
                scope=conversation.profile_scope,
                disposition=disposition,
                actor_principal_id=principal,
                source_conversation_id=conversation.id,
                source_message_id=inbound.id,
                note=pieces[3],
            )
        except ExecutionStoreError:
            return (
                "That browser transaction could not be reconciled. Check the exact transaction "
                "ID from this conversation and confirm it is awaiting reconciliation."
            )
        return (
            f"{pieces[1]} attested as {pieces[2]}; execution {request.id} is {request.status}. "
            "Original browser evidence was retained unchanged."
        )

    async def _enqueue_response(
        self,
        inbound: InboundMessage,
        conversation: Conversation,
        body: str,
    ) -> str:
        correlations = [
            CorrelationRef(
                kind="conversation",
                id=conversation.id,
                revision=conversation.revision,
                profile_label=conversation.profile_scope.label(),
            )
        ]

        execution_store = self.execution_store
        linked_requests = []
        for request in await execution_store.list_by_source_message(
            inbound.id,
            scope=conversation.profile_scope,
            limit=100,
        ):
            linked_requests.append(request)
            correlations.append(
                CorrelationRef(
                    kind="execution_request",
                    id=request.id,
                    profile_label=request.profile_scope.label(),
                )
            )
            if request.task_id is not None:
                correlations.append(
                    CorrelationRef(
                        kind="task",
                        id=request.task_id,
                        revision=request.task_revision,
                        profile_label=request.profile_scope.label(),
                    )
                )
        if linked_requests:
            footer_lines = ["Background work:"]
            for linked in linked_requests:
                line = f"- request {linked.id} ({linked.status})"
                if linked.task_id is not None:
                    line += f"; task {linked.task_id}"
                footer_lines.append(line)
            footer = "\n".join(footer_lines)
            available = max(1, self.settings.messaging.body_char_limit - len(footer) - 2)
            body = f"{body[:available]}\n\n{footer}"
        request = NotificationRequest(
            id=f"notification_{uuid4().hex}",
            route=f"inbox:{inbound.id}",
            body=body[: self.settings.messaging.body_char_limit],
            body_format="portable_markdown_v1",
            urgency="normal",
            source_kind="gateway_turn",
            profile_label=conversation.profile_scope.label(),
            source_id=inbound.id,
            dedupe_key=inbound.id,
            correlations=correlations,
            created_at=datetime.now(UTC),
        )
        record = await self.notifications.enqueue(
            request,
            scope=conversation.profile_scope,
        )
        return record.outbox.id

    async def _settle_uncertain(
        self,
        claim: InboxClaim,
        inbound: InboundMessage,
        conversation: Conversation | None,
        base_revision: int | None,
        error: str,
    ) -> None:
        if conversation is not None and base_revision is not None:
            with suppress(Exception):
                stored = await self.sessions.get(
                    conversation.session_id,
                    scope=conversation.profile_scope,
                )
                await self.gateway.finish_result(
                    scope=conversation.profile_scope,
                    message_id=inbound.id,
                    conversation_id=conversation.id,
                    expected_conversation_revision=base_revision,
                    status="uncertain",
                    session_revision=stored.revision,
                    response_outbox_id=None,
                    error=error,
                )
            await self._operator_alert(inbound, conversation, error)
        await self._finish_claim_uncertain(claim)

    async def _finish_claim_uncertain(self, claim: InboxClaim) -> None:
        with suppress(Exception):
            await self.messaging.finish_inbox(claim, status="uncertain")

    async def _operator_alert(
        self,
        inbound: InboundMessage,
        conversation: Conversation,
        error: str,
    ) -> None:
        route = self.settings.gateway.operator_route
        if route is None:
            return
        request = NotificationRequest(
            id=f"notification_{uuid4().hex}",
            route=route,
            title="Ricky gateway needs attention",
            body=(
                f"Conversation {conversation.id} became uncertain while processing "
                f"{inbound.id}: {error}"
            )[: self.settings.messaging.body_char_limit],
            urgency="attention",
            source_kind="gateway_uncertain",
            profile_label=conversation.profile_scope.label(),
            source_id=inbound.id,
            dedupe_key=inbound.id,
            correlations=[
                CorrelationRef(
                    kind="conversation",
                    id=conversation.id,
                    revision=conversation.revision,
                    profile_label=conversation.profile_scope.label(),
                )
            ],
            created_at=datetime.now(UTC),
        )
        with suppress(Exception):
            await self.notifications.enqueue(request, scope=conversation.profile_scope)


def _foreground_tools(
    capabilities: CapabilityRuntime,
    allowed: Collection[str],
    project_root: Path | None,
) -> list[Tool]:
    selected: list[Tool] = []
    for tool in capabilities.tools:
        if tool.name in _ALWAYS_EXCLUDED:
            continue
        definition = capabilities.capability_registry.for_resource("tool", tool.name)
        if definition is None or definition.id not in allowed:
            continue
        if tool.risk == "read_only" or definition.id.startswith("builtin.task."):
            selected.append(tool)
        # Direct foreground external mutation remains hard-excluded. The
        # allowed task controls are source/task scoped; other mutations need a
        # dedicated effect evaluator before they can be enabled here.
    return selected


async def _gateway_capability_catalog(
    settings: RickySettings,
    *,
    runtime: CapabilityRuntime,
    project_root: Path | None,
    route: GatewayRouteSettings,
) -> GatewayCapabilityCatalog:
    jobs: list[GatewayCapabilityItem] = []
    background_decisions = resolve_capability_policy(
        runtime.capability_registry,
        settings.agents.ad_hoc_background,
        route=route,
        require_unattended=True,
    )
    ad_hoc = [
        GatewayCapabilityItem(
            name=definition.id,
            description=definition.description,
            resources=[resource.id for resource in definition.resources],
            confirmation_required=decision.confirmation_required,
            guardrail_required=(
                decision.guardrail_required or definition.authority_capability is not None
            ),
            guardrail_intake=_guardrail_intake(
                runtime.guardrail_registry,
                definition.id,
                required=(
                    decision.guardrail_required or definition.authority_capability is not None
                ),
            ),
            delegable_capabilities=(
                [definition.authority_capability]
                if definition.authority_capability is not None
                else []
            ),
        )
        for decision in background_decisions
        if decision.eligible
        for definition in [runtime.capability_registry.require(decision.capability_id)]
        if project_root is not None or not capability_requires_project_root(definition)
    ][:100]
    excluded_controls = {
        *settings.agents.gateway_foreground.exclude_capabilities,
        *route.exclude_capabilities,
    }
    if "builtin.automation.mutate" not in excluded_controls:
        loaded_jobs, _ = JobRegistry(
            settings,
            profile_scope=route.profile_scope(),
        ).discover()
        jobs = [
            GatewayCapabilityItem(
                name=item.resource.qualified,
                description=item.spec.description,
            )
            for item in loaded_jobs[:100]
        ]
    return GatewayCapabilityCatalog(named_jobs=jobs, ad_hoc_capabilities=ad_hoc)


def _guardrail_intake(
    registry: GuardrailRegistry, capability_id: str, *, required: bool
) -> GuardrailIntakeSpec | None:
    if not required:
        return None
    evaluator = registry.get(capability_id)
    return evaluator.intake_spec if evaluator is not None else None


def _gateway_dispatcher(
    settings: RickySettings,
    *,
    project_root: Path | None,
    gateway: GatewayStore,
    project_scope: ProjectScope | None = None,
    authority: AuthorityStore | None = None,
    protected_value_registry: ResidentProtectedValueRegistry | None = None,
) -> ExecutionDispatcher:
    routes = RoutePolicy(settings, conversation_resolver=gateway)
    return ExecutionDispatcher(
        settings,
        project_root=project_root,
        project_scope=(
            project_scope
            or (
                ProjectScope.disabled()
                if project_root is None
                else ProjectScope.bound(project_root)
            )
        ),
        routes=routes,
        notifications=GatewayNotificationService(settings, routes=routes),
        authority=authority,
        protected_value_registry=protected_value_registry,
    )


def _route_for(
    settings: RickySettings,
    key: ConversationKey,
) -> tuple[str, GatewayRouteSettings]:
    matches: list[tuple[str, GatewayRouteSettings]] = []
    for name, gateway_route in settings.gateway.routes.items():
        messaging_route = settings.messaging.routes[name]
        transport = settings.messaging.transports[messaging_route.transport]
        if (
            transport.type == key.transport
            and transport.account == key.account
            and messaging_route.destination == key.destination_id
        ):
            matches.append((name, gateway_route))
    if len(matches) != 1:
        raise GatewayConversationError(
            "inbound conversation must match exactly one configured gateway route"
        )
    return matches[0]


def _key(inbound: InboundMessage) -> ConversationKey:
    return ConversationKey(
        transport=inbound.transport,
        account=inbound.account,
        destination_id=inbound.destination_id,
        thread_id=inbound.thread_id,
    )


def _rotation_session_id(key: ConversationKey, inbound_message_id: str) -> str:
    identity = f"gateway-rotation\0{key.digest()}\0{inbound_message_id}"
    return "session_" + hashlib.sha256(identity.encode()).hexdigest()[:32]


def _project_root(route: GatewayRouteSettings) -> Path | None:
    if route.project_root is None:
        return None
    configured = Path(route.project_root).expanduser()
    if not configured.is_absolute():
        configured = find_project_root() / configured
    return configured.resolve()


def _project_scope(route: GatewayRouteSettings) -> ProjectScope:
    root = _project_root(route)
    return ProjectScope.disabled() if root is None else ProjectScope.bound(root)


def _final_message(session: AgentSession) -> str | None:
    for message in reversed(session.history):
        if message.role != "assistant":
            continue
        text = "".join(part.text for part in message.content if isinstance(part, TextPart)).strip()
        if text:
            return text
    return None


def _render_context_report(
    session: AgentSession,
    revision: int,
    report: ContextReport,
) -> str:
    """Render a transport-neutral, bounded foreground context inspection."""

    budget = report.budget
    hard_input = (
        str(budget.hard_input_tokens) if budget.hard_input_tokens is not None else "unknown"
    )
    headroom = str(budget.remaining_tokens) if budget.remaining_tokens is not None else "unknown"
    lines = [
        "Foreground context",
        f"Session: {session.id} (revision {revision})",
        f"Model: {session.provider} · {session.model}",
        (f"Input: ~{report.estimated_input_tokens} tokens / {report.serialized_chars} chars"),
        (
            f"Capacity: hard input {hard_input}; headroom {headroom}; "
            f"response reserve {budget.output_reserve_tokens}; "
            f"safety margin {budget.safety_margin_tokens}; source {budget.capacity_source}"
        ),
        (
            f"Request: {report.message_count} messages; {report.tool_count} advertised tools; "
            f"pending user input: {'yes' if report.pending_user_input_included else 'no'}"
        ),
        (
            f"Artifacts: {report.artifact_count} references; "
            f"{report.stored_artifact_chars} stored chars excluded from input"
        ),
        "Sections (chars / est. tokens / count):",
        *(
            f"- {section.name}: {section.chars} / {section.estimated_tokens} / {section.item_count}"
            for section in report.sections
        ),
    ]
    if report.checkpoint is not None:
        checkpoint = report.checkpoint
        lines.extend(
            [
                "Active checkpoint:",
                f"- id: {checkpoint.id}",
                f"- created: {checkpoint.created_at.isoformat()}",
                (
                    f"- raw messages: {checkpoint.covered_raw_messages} covered; "
                    f"{checkpoint.retained_raw_messages} retained; "
                    f"{checkpoint.original_history_messages} available in original history"
                ),
                (
                    f"- summary: {checkpoint.summary_chars} chars; estimated tokens "
                    f"{checkpoint.estimated_tokens_before} -> "
                    f"{checkpoint.estimated_tokens_after} "
                    f"(reduction {checkpoint.estimated_token_reduction})"
                ),
                f"- original artifact references: {checkpoint.artifact_reference_count}",
            ]
        )
    return "\n".join(lines)


def _is_bare_browser_decision(value: str) -> bool:
    """Keep ambiguous chat replies out of both the model and approval channel."""

    return value.casefold().strip().rstrip(".!?").strip() in {"yes", "no"}


def _bounded_error(exc: BaseException) -> str:
    value = str(exc).strip() or type(exc).__name__
    return value[:2_000]
