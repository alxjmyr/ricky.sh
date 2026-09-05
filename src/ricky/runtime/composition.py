"""Neutral composition root shared by interactive chat and agent jobs."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ricky.agent.artifacts import SessionArtifactStore
from ricky.agent.loop import AgentLoop
from ricky.agent.session import AgentSession
from ricky.agent.tool_dispatch import PermissionResponder
from ricky.agent.workflow import ApprovalResponder, WorkflowService
from ricky.browser import (
    BrowserExecutionGuard,
    BrowserService,
    background_browser_tools,
    browser_tool_descriptors,
    browser_tools,
)
from ricky.browser.guardrails import (
    BROWSER_COMMIT_TOOLS,
    BROWSER_INTERACT_TOOLS,
    BROWSER_PROTECTED_TOOLS,
    BROWSER_READ_TOOLS,
    browser_guardrail_evaluators,
)
from ricky.browser.tools import BrowserAttachmentResolver
from ricky.capabilities import (
    CapabilityRegistry,
    CapabilitySpec,
    GuardrailRegistry,
    build_capability_registry,
    derive_skill_owners,
    validate_capability_inventory,
)
from ricky.config import RickySettings
from ricky.durable_tasks.scoped import ScopedDurableTaskStore, ScopedTaskArtifactStore
from ricky.durable_tasks.state_guard import DurableTaskStateGuard
from ricky.durable_tasks.tools import durable_task_policy, durable_task_tools
from ricky.executions.dispatcher import ExecutionDispatcher
from ricky.executions.tools import execution_tools
from ricky.llm import Provider, SupportsMediaResolver, create_provider
from ricky.media import SessionMediaStore
from ricky.memory import MemoryStore, memory_tools
from ricky.notifications import NotificationService
from ricky.notifications.tools import NotifyUserTool
from ricky.permissions import PermissionEngine
from ricky.project_scope import ProjectScope
from ricky.protected_values import (
    DestinationApprovalResponder,
    ProfileVaultStore,
    ProtectedValueBackendFactory,
    ProtectedValueBroker,
    ProtectedValuesCatalogTool,
    ResidentProtectedValueRegistry,
    SecureValueResponder,
    UnlockResponder,
    deny_destination,
    deny_secure_value,
    deny_unlock,
)
from ricky.skills.registry import SkillRegistry, discover_skills
from ricky.skills.tool import ReadSkillResourceTool, UseSkillTool
from ricky.tools import Tool, ToolRegistry
from ricky.tools.base import StateGuardRegistry
from ricky.tools.builtin import builtin_tools
from ricky.tools.builtin.artifacts import ReadToolArtifactTool
from ricky.tools.integrations.gcal import gcal_toolset
from ricky.tools.integrations.gmail import gmail_toolset
from ricky.tools.integrations.google import (
    ALL_SERVICE_SCOPES,
    GoogleAuth,
    google_accounts_available,
)
from ricky.tools.integrations.slack import slack_toolset
from ricky.tools.integrations.web_search import web_search_toolset
from ricky.workflows.registry import WorkflowRegistry, discover_workflows
from ricky.workflows.tool import StartWorkflowTool, ValidateWorkflowTool


@dataclass(frozen=True)
class CapabilityRuntime:
    """Provider-independent capabilities and owned resources for one session."""

    tools: tuple[Tool, ...]
    chat_registry: ToolRegistry
    full_registry: ToolRegistry
    skill_registry: SkillRegistry
    memory: MemoryStore | None
    durable_tasks: ScopedDurableTaskStore
    workflow_registry: WorkflowRegistry | None
    permission_engine: PermissionEngine
    job_stream_adapters: tuple[Any, ...]
    session_artifacts: SessionArtifactStore
    session_media: SessionMediaStore
    capability_registry: CapabilityRegistry
    guardrail_registry: GuardrailRegistry
    protected_values: ProtectedValueBroker | None


@dataclass(frozen=True)
class BackgroundBrowserRuntime:
    """Exact execution-owned browser composition inputs for one job attempt."""

    mode: Literal["read_only", "transaction"]
    allowed_tools: frozenset[str]
    guard: BrowserExecutionGuard
    tool_wrapper: Callable[[Tool], Tool] | None = None
    attachment_resolver: BrowserAttachmentResolver | None = None


@dataclass(frozen=True)
class SessionRuntime:
    """Complete runtime used to drive one agent session."""

    provider: Provider
    capabilities: CapabilityRuntime
    agent_loop: AgentLoop
    workflow_runner: WorkflowService | None

    @property
    def registry(self) -> ToolRegistry:
        return self.capabilities.chat_registry

    @property
    def skill_registry(self) -> SkillRegistry:
        return self.capabilities.skill_registry

    @property
    def memory(self) -> MemoryStore | None:
        return self.capabilities.memory

    @property
    def durable_tasks(self) -> ScopedDurableTaskStore:
        return self.capabilities.durable_tasks

    @property
    def workflow_registry(self) -> WorkflowRegistry | None:
        return self.capabilities.workflow_registry


def delegable_effect_tools(settings: RickySettings) -> list[Tool]:
    """Construct shipped effect tools that support task-scoped delegation."""

    del settings
    return []


def built_in_guardrail_evaluators() -> tuple[Any, ...]:
    """Construct shipped capability-specific live-guardrail evaluators."""

    return browser_guardrail_evaluators()


# Shared policy meaning only. Tool membership is declared by each tool and
# derived by the capability registry; these specs intentionally name no tools.
_BASE_CAPABILITY_SPECS: tuple[CapabilitySpec, ...] = tuple(
    CapabilitySpec(id=capability_id, owner="builtin", description=description)
    for capability_id, description in (
        ("builtin.project.read", "Read and search project files."),
        ("builtin.project.mutate", "Create or edit project files."),
        ("builtin.host.execute", "Run a local shell command."),
        ("builtin.session.read", "Read session-owned tool artifacts."),
        ("builtin.session.mutate", "Maintain the session-local working plan."),
        ("builtin.memory.read", "Recall durable memory."),
        ("builtin.memory.mutate", "Create, replace, or delete durable memory."),
        ("builtin.skill.use", "Activate and read declarative skill resources."),
        ("builtin.automation.read", "Inspect and validate automation state."),
        ("builtin.automation.mutate", "Start or manage workflows and background work."),
        ("builtin.task.read", "Search and read durable tasks and artifacts."),
        ("builtin.task.mutate", "Create, coordinate, or update durable tasks and artifacts."),
        ("builtin.notification.mutate", "Queue a notification on an approved logical route."),
        (
            "builtin.protected_value.read",
            "Inspect safe protected-value aliases, fields, and policies.",
        ),
        ("builtin.browser.handoff", "Hand one headed browser step to the local user."),
        ("builtin.web.read", "Search public Web evidence."),
        ("builtin.email.read", "Search and read email data."),
        ("builtin.email.mutate", "Draft, send, label, trash, or download email data."),
        ("builtin.calendar.read", "Read calendars, events, and availability."),
        ("builtin.calendar.mutate", "Create, update, respond to, or delete calendar events."),
        ("builtin.chat.read", "Read and search chat services."),
        ("builtin.chat.mutate", "Mark, send, or download chat data."),
        ("builtin.authorization.review", "Prepare one exact source-bound capability call."),
    )
)

CAPABILITY_SPECS: tuple[CapabilitySpec, ...] = (
    *_BASE_CAPABILITY_SPECS,
    CapabilitySpec(
        id="builtin.browser.read",
        owner="builtin",
        description="Control and inspect a read-oriented browser session.",
        guardrail_schema_id="browser.read",
    ),
    CapabilitySpec(
        id="builtin.browser.interact",
        owner="builtin",
        description="Perform an explicitly authorized ordinary browser interaction.",
        guardrail_schema_id="browser.interact",
        authority_capability="browser_interact",
    ),
    CapabilitySpec(
        id="builtin.protected_value.use",
        owner="builtin",
        description="Use an authorized protected value through a purpose-built local consumer.",
        guardrail_schema_id="protected_value.use",
        authority_capability="protected_value_use",
    ),
    CapabilitySpec(
        id="builtin.browser.commit",
        owner="builtin",
        description="Perform an explicitly reviewed consequential browser action.",
        guardrail_schema_id="browser.commit",
        authority_capability="browser_commit",
    ),
)


@asynccontextmanager
async def build_capability_runtime(
    settings: RickySettings,
    *,
    session: AgentSession,
    project_root: Path | None = None,
    project_scope: ProjectScope | None = None,
    slack_factory: Callable[[RickySettings], Any] = slack_toolset,
    gmail_factory: Callable[..., Any] = gmail_toolset,
    gcal_factory: Callable[..., Any] = gcal_toolset,
    web_search_factory: Callable[[RickySettings], Any] = web_search_toolset,
    browser_factory: Callable[..., Awaitable[BrowserService]] | None = None,
    background_browser: BackgroundBrowserRuntime | None = None,
    background_browser_factory: Callable[..., Awaitable[BrowserService]] = BrowserService.create,
    protected_value_backend_factory: ProtectedValueBackendFactory = ProfileVaultStore,
    protected_value_registry: ResidentProtectedValueRegistry | None = None,
    protected_value_broker: ProtectedValueBroker | None = None,
    unlock_responder: UnlockResponder = deny_unlock,
    secure_value_responder: SecureValueResponder = deny_secure_value,
    destination_responder: DestinationApprovalResponder = deny_destination,
    google_auth_factory: Callable[..., GoogleAuth] = GoogleAuth,
    skill_factory: Callable[..., SkillRegistry] = discover_skills,
    registry_factory: Callable[..., ToolRegistry] = ToolRegistry,
) -> AsyncIterator[CapabilityRuntime]:
    """Build the current session tool surface without constructing a provider."""

    scope = project_scope or (
        ProjectScope.bound(project_root) if project_root is not None else ProjectScope.discover()
    )
    project_root = scope.root
    runtime_settings = settings.resolve_profile_runtime_settings(session.profile_scope)
    resources = AsyncExitStack()
    try:
        slack = slack_factory(runtime_settings)
        if slack is not None:
            resources.push_async_callback(slack.aclose)
        google_auth: GoogleAuth | None = None
        if google_accounts_available(runtime_settings):
            google_auth = google_auth_factory(runtime_settings, scopes=ALL_SERVICE_SCOPES)
            resources.push_async_callback(google_auth.aclose)
        gmail = gmail_factory(runtime_settings, auth=google_auth)
        if gmail is not None:
            resources.push_async_callback(gmail.aclose)
        gcal = gcal_factory(runtime_settings, auth=google_auth)
        if gcal is not None:
            resources.push_async_callback(gcal.aclose)
        web_search = web_search_factory(runtime_settings)
        if web_search is not None:
            resources.push_async_callback(web_search.aclose)

        memory = (
            MemoryStore.create(runtime_settings, scope=session.profile_scope)
            if runtime_settings.memory.enabled
            else None
        )
        task_store = await ScopedDurableTaskStore.create(settings, scope=session.profile_scope)
        resources.push_async_callback(task_store.release_session_leases, session.id)
        artifacts = ScopedTaskArtifactStore(task_store)
        session_artifacts = SessionArtifactStore.create(settings, session.id)
        session_media = SessionMediaStore.create(runtime_settings, session.id)
        resources.push_async_callback(session_media.remove_retention, session, "runtime")
        protected_values: ProtectedValueBroker | None = protected_value_broker
        if protected_value_broker is not None and not runtime_settings.protected_values.enabled:
            raise ValueError("an injected protected-value broker requires the subsystem enabled")
        if protected_values is None and runtime_settings.protected_values.enabled:
            protected_values = (
                protected_value_registry.lease(
                    scope=session.profile_scope,
                    consumer_ids=frozenset({"browser.fill"}),
                    unlock_responder=unlock_responder,
                    secure_value_responder=secure_value_responder,
                    destination_responder=destination_responder,
                )
                if protected_value_registry is not None
                else ProtectedValueBroker(
                    runtime_settings,
                    scope=session.profile_scope,
                    consumer_ids=frozenset({"browser.fill"}),
                    unlock_responder=unlock_responder,
                    secure_value_responder=secure_value_responder,
                    destination_responder=destination_responder,
                    backend_factory=protected_value_backend_factory,
                )
            )
            resources.push_async_callback(protected_values.aclose)
        skills = skill_factory(
            settings=settings,
            profile_scope=session.profile_scope,
        )
        tools: list[Tool] = [
            *builtin_tools(),
            ReadToolArtifactTool(session_artifacts),
            *(
                [
                    NotifyUserTool(
                        NotificationService(runtime_settings),
                        allowed_routes=set(runtime_settings.messaging.agent_routes),
                    )
                ]
                if runtime_settings.messaging.agent_routes
                else []
            ),
            *(
                execution_tools(
                    ExecutionDispatcher(
                        runtime_settings,
                        project_root=project_root,
                        project_scope=scope,
                    ),
                    allowed_routes=set(runtime_settings.messaging.agent_routes),
                )
                if runtime_settings.messaging.agent_routes
                else []
            ),
            *(slack.tools if slack is not None else []),
            *(gmail.tools if gmail is not None else []),
            *(gcal.tools if gcal is not None else []),
            *(web_search.tools if web_search is not None else []),
            *(memory_tools(memory) if memory is not None else []),
            *durable_task_tools(task_store, artifacts),
            *delegable_effect_tools(runtime_settings),
            UseSkillTool(skills),
            ReadSkillResourceTool(skills),
            *(
                [ProtectedValuesCatalogTool(protected_values)]
                if protected_values is not None
                else []
            ),
        ]
        workflows: WorkflowRegistry | None = None
        if runtime_settings.workflow.enabled:
            skill_names = skills.identifiers()
            workflows = discover_workflows(
                settings=runtime_settings,
                profile_scope=session.profile_scope,
                skill_names=skill_names,
                workflow_settings=runtime_settings.workflow,
                tool_registry=registry_factory(tools),
            )
            tools.append(StartWorkflowTool(workflows))
            tools.append(
                ValidateWorkflowTool(
                    skill_names=skill_names,
                    workflow_registry=workflows,
                    tool_registry=registry_factory(tools),
                )
            )
        # Browser tools never participate in workflow discovery. A foreground
        # runtime may own its interactive browser, while an execution runtime
        # receives only the exact guarded subset compiled for that attempt.
        if browser_factory is not None and background_browser is not None:
            raise ValueError("foreground and background browser composition are exclusive")
        browser_runtime_tools: list[Tool] = []
        if background_browser is not None:
            if not (
                runtime_settings.browser.enabled
                and runtime_settings.browser.background.enabled
                and runtime_settings.browser.background.read_enabled
            ):
                raise ValueError("background browser ownership is disabled by current settings")
            browser = await background_browser_factory(
                runtime_settings,
                scope=session.profile_scope,
                runtime_guard=background_browser.guard,
            )
            resources.push_async_callback(browser.aclose)
            browser_runtime_tools.extend(
                background_browser_tools(
                    browser,
                    mode=background_browser.mode,
                    allowed_tools=background_browser.allowed_tools,
                    media=session_media,
                    protected_values=protected_values,
                    attachment_resolver=background_browser.attachment_resolver,
                )
            )
            if background_browser.tool_wrapper is not None:
                browser_runtime_tools = [
                    background_browser.tool_wrapper(tool) for tool in browser_runtime_tools
                ]
        elif browser_factory is not None and runtime_settings.browser.enabled:
            browser = await browser_factory(runtime_settings, scope=session.profile_scope)
            resources.push_async_callback(browser.aclose)
            browser_runtime_tools.extend(browser_tools(browser, session_media, protected_values))
        runtime_tools = [*tools, *browser_runtime_tools]
        full_registry = registry_factory(runtime_tools if background_browser is not None else tools)
        capability_inventory_tools = list(runtime_tools)
        if browser_factory is None:
            background = runtime_settings.browser.background
            inventory_names: set[str] = set()
            if runtime_settings.browser.enabled and background.enabled:
                if background.read_enabled:
                    inventory_names.update(BROWSER_READ_TOOLS)
                if background.interaction_enabled:
                    inventory_names.update(BROWSER_INTERACT_TOOLS)
                if background.commit_enabled:
                    inventory_names.update(BROWSER_COMMIT_TOOLS)
                if background.protected_values_enabled and (
                    protected_value_broker is not None
                    or (
                        protected_value_registry is not None
                        and set(protected_value_registry.unlocked_profiles)
                        & set(session.profile_scope.profiles)
                    )
                ):
                    inventory_names.update(BROWSER_PROTECTED_TOOLS)
            actual_names = {tool.name for tool in capability_inventory_tools}
            capability_inventory_tools.extend(
                tool
                for tool in browser_tool_descriptors()
                if tool.name in inventory_names and tool.name not in actual_names
            )
        capability_registry = build_capability_registry(
            capability_inventory_tools,
            skills,
            capability_specs=CAPABILITY_SPECS,
            external_tool_owners=_external_tool_owners(capability_inventory_tools),
            skill_owners=derive_skill_owners(skills, settings=runtime_settings),
            state_guards=StateGuardRegistry([DurableTaskStateGuard(task_store)]),
        )
        guardrail_registry = GuardrailRegistry(built_in_guardrail_evaluators())
        inventory_diagnostics = validate_capability_inventory(
            capability_registry,
            guardrail_registry,
        )
        inventory_errors = [
            item.message for item in inventory_diagnostics if item.severity == "error"
        ]
        if inventory_errors:
            raise ValueError("; ".join(inventory_errors))
        chat_registry = registry_factory(
            [
                tool
                for tool in runtime_tools
                if not getattr(tool, "workflow_only", False)
                and not getattr(tool, "deferred_until_artifact", False)
            ]
        )
        yield CapabilityRuntime(
            tools=tuple(runtime_tools),
            chat_registry=chat_registry,
            full_registry=full_registry,
            skill_registry=skills,
            memory=memory,
            durable_tasks=task_store,
            workflow_registry=workflows,
            permission_engine=PermissionEngine(durable_task_policy()),
            job_stream_adapters=tuple(slack.job_stream_adapters if slack is not None else []),
            session_artifacts=session_artifacts,
            session_media=session_media,
            capability_registry=capability_registry,
            guardrail_registry=guardrail_registry,
            protected_values=protected_values,
        )
    finally:
        await resources.aclose()


def _external_tool_owners(tools: list[Tool]) -> dict[str, str]:
    """Identify user/plugin tools without weakening built-in mapping checks.

    Ricky-owned tools must declare a built-in capability. Tools from another
    Python package are direct capabilities unless they declare a trusted group;
    a plugin may declare its stable namespace with ``capability_owner``,
    otherwise the user owns it.
    """

    owners: dict[str, str] = {}
    for tool in tools:
        module = type(tool).__module__
        if module == "ricky" or module.startswith("ricky."):
            continue
        owner = getattr(tool, "capability_owner", "user")
        if not isinstance(owner, str):
            raise TypeError(f"capability owner for {tool.name} must be a string")
        owners[tool.name] = owner
    return owners


@asynccontextmanager
async def build_session_runtime(
    settings: RickySettings,
    *,
    session: AgentSession,
    provider: Provider | None = None,
    permission_responder: PermissionResponder | None = None,
    approval_responder: ApprovalResponder | None = None,
    project_root: Path | None = None,
    project_scope: ProjectScope | None = None,
    slack_factory: Callable[[RickySettings], Any] = slack_toolset,
    gmail_factory: Callable[..., Any] = gmail_toolset,
    gcal_factory: Callable[..., Any] = gcal_toolset,
    web_search_factory: Callable[[RickySettings], Any] = web_search_toolset,
    browser_factory: Callable[..., Awaitable[BrowserService]] | None = None,
    background_browser: BackgroundBrowserRuntime | None = None,
    background_browser_factory: Callable[..., Awaitable[BrowserService]] = BrowserService.create,
    protected_value_backend_factory: ProtectedValueBackendFactory = ProfileVaultStore,
    protected_value_registry: ResidentProtectedValueRegistry | None = None,
    protected_value_broker: ProtectedValueBroker | None = None,
    unlock_responder: UnlockResponder = deny_unlock,
    secure_value_responder: SecureValueResponder = deny_secure_value,
    destination_responder: DestinationApprovalResponder = deny_destination,
    google_auth_factory: Callable[..., GoogleAuth] = GoogleAuth,
    skill_factory: Callable[..., SkillRegistry] = discover_skills,
    registry_factory: Callable[..., ToolRegistry] = ToolRegistry,
) -> AsyncIterator[SessionRuntime]:
    """Build and close the provider plus all capabilities for one session."""

    runtime_settings = settings.resolve_profile_runtime_settings(session.profile_scope)
    owned_provider = provider or create_provider(session.provider, runtime_settings)
    try:
        async with build_capability_runtime(
            settings,
            session=session,
            project_root=project_root,
            project_scope=project_scope,
            slack_factory=slack_factory,
            gmail_factory=gmail_factory,
            gcal_factory=gcal_factory,
            web_search_factory=web_search_factory,
            browser_factory=browser_factory,
            background_browser=background_browser,
            background_browser_factory=background_browser_factory,
            protected_value_backend_factory=protected_value_backend_factory,
            protected_value_registry=protected_value_registry,
            protected_value_broker=protected_value_broker,
            unlock_responder=unlock_responder,
            secure_value_responder=secure_value_responder,
            destination_responder=destination_responder,
            google_auth_factory=google_auth_factory,
            skill_factory=skill_factory,
            registry_factory=registry_factory,
        ) as capabilities:
            if isinstance(owned_provider, SupportsMediaResolver):
                owned_provider.bind_media_resolver(
                    capabilities.session_media.resolver(
                        session,
                        provider=session.provider,
                        profile_scope=session.profile_scope,
                    )
                )
            loop = AgentLoop(
                provider=owned_provider,
                registry=capabilities.chat_registry,
                settings=runtime_settings,
                permission_engine=capabilities.permission_engine,
                permission_responder=permission_responder,
                skill_registry=capabilities.skill_registry,
                memory=capabilities.memory,
                workflow_registry=capabilities.workflow_registry,
                cwd=project_root,
                artifact_store=capabilities.session_artifacts,
                deferred_tools=tuple(
                    tool
                    for tool in capabilities.tools
                    if getattr(tool, "deferred_until_artifact", False)
                ),
            )
            workflow_runner: WorkflowService | None = None
            if capabilities.workflow_registry is not None:
                skill_bodies: dict[str, str] = {}
                for identity in capabilities.skill_registry.identifiers():
                    skill = capabilities.skill_registry.get(identity)
                    if skill is not None:
                        skill_bodies[identity] = skill.body
                workflow_runner = WorkflowService(
                    provider=owned_provider,
                    tool_registry=capabilities.full_registry,
                    settings=runtime_settings,
                    workflow_registry=capabilities.workflow_registry,
                    skill_bodies=skill_bodies,
                    permission_engine=capabilities.permission_engine,
                    permission_responder=permission_responder,
                    approval_responder=approval_responder,
                    cwd=project_root,
                )
            yield SessionRuntime(
                provider=owned_provider,
                capabilities=capabilities,
                agent_loop=loop,
                workflow_runner=workflow_runner,
            )
    finally:
        await owned_provider.aclose()
