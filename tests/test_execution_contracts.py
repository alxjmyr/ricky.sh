from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from pydantic import JsonValue

from authority_support import install_sandbox_runtime
from ricky.agent.session import AgentSession
from ricky.authority.compiler import ContractAuthorityCompiler
from ricky.authority.store import AuthorityStore
from ricky.authority.types import GrantSource, source_text_digest
from ricky.browser.guardrails import (
    BrowserAuthenticatedOriginSelection,
    BrowserGuardrailConstraints,
)
from ricky.capabilities import (
    AuthenticatedSource,
    GuardrailFieldProposal,
    GuardrailProposal,
    compile_guardrail,
)
from ricky.config import GatewayRetentionSettings, GatewayRouteSettings, RickySettings
from ricky.durable_tasks.artifacts import TaskArtifactStore
from ricky.durable_tasks.store import DurableTaskStore
from ricky.executions.compiler import (
    CompileBinding,
    ExecutionContractCompileError,
    ExecutionContractCompiler,
)
from ricky.executions.contracts import load_pinned_runtime
from ricky.executions.dispatcher import (
    ExecutionDispatcher,
    ExecutionDispatchError,
)
from ricky.executions.drafts import (
    AdHocConfirmation,
    AdHocExecutionProposal,
    AdHocGuardrailContinuation,
)
from ricky.executions.store import SCHEMA_VERSION, ExecutionDraftFenceError, ExecutionStore
from ricky.gateway.audit import GatewayAudit
from ricky.gateway.recovery import GatewayRecovery
from ricky.gateway.retention import GatewayRetention
from ricky.jobs.runner import (
    JobConfigurationError,
    JobRunner,
    _resolve_execution_browser_attachments,
)
from ricky.llm import CompletionRequest, Message, MessageDone, StreamEvent
from ricky.messaging.store import MessagingStore
from ricky.messaging.types import InboundMessage, ReceiveBatch, ReceivedUpdate, TransportCursor
from ricky.notifications.routes import RoutePolicy
from ricky.profiles import ProfileLabel, ProfileResourceRef
from ricky.runtime.composition import build_capability_runtime


@pytest.fixture(autouse=True)
def _test_only_guarded_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    install_sandbox_runtime(monkeypatch)


def _settings(tmp_path: Path, *, guarded: bool = False) -> RickySettings:
    background: dict[str, object] = {
        "exclude_capabilities": [],
        "confirmation_required_capabilities": [],
        "guardrail_required_capabilities": [],
        "execution": {
            "wall_clock_seconds": 30,
            "iterations": 3,
            "max_completion_tokens_per_request": 256,
            "effect_calls": 0,
        },
    }
    if guarded:
        background.update(
            {
                "confirmation_required_capabilities": ["builtin.sandbox.reservation"],
                "guardrail_required_capabilities": ["builtin.sandbox.reservation"],
                "execution": {
                    "wall_clock_seconds": 30,
                    "iterations": 3,
                    "max_completion_tokens_per_request": 256,
                    "effect_calls": 1,
                },
            }
        )
    return RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / "project-config"),
            "memory": {"enabled": False},
            "workflow": {"enabled": False},
            "agents": {"ad_hoc_background": background},
            "messaging": {
                "telegram_accounts": {
                    "personal/owner": {"bot_token": "test-token"},
                },
                "transports": {"owner-telegram": {"type": "telegram", "account": "personal/owner"}},
                "routes": {
                    "owner": {
                        "transport": "owner-telegram",
                        "destination": "chat",
                        "owner_profile": "personal",
                        "accepted_profiles": ["shared", "personal"],
                    }
                },
            },
            "gateway": {
                "routes": {
                    "owner": {
                        "provider": "openrouter",
                        "model": "test-model",
                        "primary_profile": "personal",
                        "project_root": str(tmp_path),
                    }
                }
            },
            "authority": {
                "enabled": guarded,
                "allowed_principals": ["telegram:personal/owner:1"] if guarded else [],
                "capabilities": {
                    "sandbox_reservation": {
                        "enabled": guarded,
                        "max_effect_calls": 1,
                    }
                },
            },
        }
    )


def _guardrail_fields(
    values: dict[str, JsonValue], *, quote: str
) -> tuple[GuardrailFieldProposal, ...]:
    return tuple(
        GuardrailFieldProposal(field=name, value=value, source_quote=quote)
        for name, value in values.items()
    )


async def _task(settings: RickySettings):
    store = await DurableTaskStore.create(settings, profile="personal")
    return await store.create_task(
        title="Background task",
        objective="Complete bounded work",
        closure_criteria="A report exists",
        execution_mode="agent",
        authority="deterministic_user_command",
        executor_id="test",
    )


def _source(
    text: str,
    *,
    conversation_id: str,
    principal: str = "telegram:personal/owner:1",
):
    return AuthenticatedSource(
        principal_id=principal,
        conversation_id=conversation_id,
        message_id=f"inbound_{uuid4().hex}",
        text_digest=hashlib.sha256(text.encode()).hexdigest(),
        text_snapshot=text,
        received_at=datetime.now(UTC),
    )


async def _compiler(
    settings: RickySettings,
    tmp_path: Path,
    *,
    conversation_id: str,
):
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="test-model",
    )
    runtime = build_capability_runtime(settings, session=session, project_root=tmp_path)
    entered = await runtime.__aenter__()
    route = GatewayRouteSettings(
        provider="openrouter",
        model="test-model",
        primary_profile="personal",
        project_root=str(tmp_path),
    )
    compiler = ExecutionContractCompiler(
        settings,
        capabilities=entered.capability_registry,
        guardrails=entered.guardrail_registry,
        skills=entered.skill_registry,
        route=route,
        binding=CompileBinding(
            principal_id="telegram:personal/owner:1",
            conversation_id=conversation_id,
            route_name="owner",
            notification_route=f"conversation:{conversation_id}",
            profile_scope=settings.resolve_profile_scope(),
            provider="openrouter",
            model="test-model",
            project_root_ref=str(tmp_path.resolve()),
        ),
    )
    return runtime, compiler


@pytest.mark.asyncio
async def test_compiler_rejects_untrusted_project_and_callback_bindings(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="test-model",
    )
    runtime = build_capability_runtime(settings, session=session, project_root=tmp_path)
    entered = await runtime.__aenter__()
    route = settings.gateway.routes["owner"]
    base = {
        "principal_id": "telegram:personal/owner:1",
        "conversation_id": "conversation_test",
        "route_name": "owner",
        "notification_route": "conversation:conversation_test",
        "profile_scope": settings.resolve_profile_scope(),
        "provider": "openrouter",
        "model": "test-model",
        "project_root_ref": str(tmp_path.resolve()),
    }
    try:
        for override, message in (
            (
                {"project_root_ref": str((tmp_path / "other").resolve())},
                "project root",
            ),
            ({"notification_route": "conversation:other"}, "notification route"),
        ):
            with pytest.raises(ValueError, match=message):
                ExecutionContractCompiler(
                    settings,
                    capabilities=entered.capability_registry,
                    guardrails=entered.guardrail_registry,
                    skills=entered.skill_registry,
                    route=route,
                    binding=CompileBinding.model_validate(base | override),
                )

        substituted_route = route.model_copy(
            update={"project_root": str((tmp_path / "other").resolve())}
        )
        with pytest.raises(ValueError, match="another project root"):
            ExecutionContractCompiler(
                settings,
                capabilities=entered.capability_registry,
                guardrails=entered.guardrail_registry,
                skills=entered.skill_registry,
                route=substituted_route,
                binding=CompileBinding.model_validate(
                    base
                    | {
                        "project_root_ref": str((tmp_path / "other").resolve()),
                    }
                ),
            )
    finally:
        await runtime.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_project_capability_requires_an_exact_route_root(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.gateway.routes["owner"].project_root = None
    task = await _task(settings)
    conversation_id = f"conversation_{uuid4().hex}"
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="test-model",
    )
    runtime = build_capability_runtime(settings, session=session, project_root=None)
    entered = await runtime.__aenter__()
    try:
        compiler = ExecutionContractCompiler(
            settings,
            capabilities=entered.capability_registry,
            guardrails=entered.guardrail_registry,
            skills=entered.skill_registry,
            route=settings.gateway.routes["owner"],
            binding=CompileBinding(
                principal_id="telegram:personal/owner:1",
                conversation_id=conversation_id,
                route_name="owner",
                notification_route=f"conversation:{conversation_id}",
                profile_scope=settings.resolve_profile_scope(),
                provider="openrouter",
                model="test-model",
                project_root_ref=None,
            ),
        )
        with pytest.raises(ExecutionContractCompileError, match="configured project root"):
            await compiler.review(
                AdHocExecutionProposal(
                    task_id=task.id,
                    expected_task_revision=task.revision,
                    goal="Inspect a project that is not bound.",
                    requested_capabilities=("builtin.project.read",),
                ),
                source=_source("Inspect the project.", conversation_id=conversation_id),
            )
    finally:
        await runtime.__aexit__(None, None, None)


class _AllowRoutes:
    async def validate(self, route: str, profile_label: ProfileLabel) -> None:
        assert route.startswith("conversation:")
        assert profile_label.required_profiles == ("shared", "personal")


class _ContractProvider:
    name = "contract-test"

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        yield MessageDone(message=Message.text("assistant", "Contract work complete."))

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_read_only_proposal_compiles_without_a_named_profile(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    task = await _task(settings)
    conversation_id = f"conversation_{uuid4().hex}"
    runtime, compiler = await _compiler(settings, tmp_path, conversation_id=conversation_id)
    try:
        source = _source("Research the project and report back.", conversation_id=conversation_id)
        proposal = AdHocExecutionProposal(
            task_id=task.id,
            expected_task_revision=task.revision,
            goal="Research the project and report back.",
            requested_capabilities=("builtin.project.read",),
        )
        draft = await compiler.review(proposal, source=source)
        assert await compiler.review(proposal, source=source) == draft
        assert draft.status == "ready"
        contract = await compiler.compile(draft)
        assert contract.tools
        assert {item.id for item in contract.tools} == {
            "glob_search",
            "grep_search",
            "list_dir",
            "read_file",
        }
        assert contract.capabilities[0].id == "builtin.project.read"
        assert contract.source_message_ids == (source.message_id,)
        stored = await ExecutionStore(settings).get_contract(
            contract.digest,
            scope=contract.profile_scope,
        )
        assert stored == contract
        snapshot = (
            Path(settings.user_data_dir)
            / settings.executions.contract_snapshot_dir
            / contract.digest
            / "contract.json"
        )
        assert snapshot.is_file()

        settings.gateway.routes["owner"].project_root = str(tmp_path / "other")
        dispatcher = ExecutionDispatcher(
            settings,
            project_root=tmp_path,
            routes=cast(RoutePolicy, _AllowRoutes()),
        )
        with pytest.raises(ExecutionDispatchError, match="project root changed"):
            await dispatcher.create_contract_execution_request(
                contract,
                request_key="route-root-drift",
            )
    finally:
        await runtime.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_browser_upload_contract_pins_exact_task_artifact(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    tasks = await DurableTaskStore.create(settings, profile="personal")
    task = await tasks.create_task(
        title="Application",
        objective="Submit the reviewed application",
        closure_criteria="The application is submitted",
        execution_mode="agent",
        authority="agent_autonomy",
        executor_id="fixture",
    )
    claimed = await tasks.claim(
        task.id,
        holder_session_id="fixture",
        authority="agent_autonomy",
        executor_id="fixture",
    )
    assert claimed.lease is not None
    written = await TaskArtifactStore(tasks).write(
        task.id,
        "application.txt",
        "reviewed application\n",
        lease=claimed.lease,
        expected_revision=claimed.revision,
        expected_sha256=None,
        authority="agent_autonomy",
        executor_id="fixture",
    )
    attachment_id = f"task/personal/{task.id}/application.txt"
    read_constraints = BrowserGuardrailConstraints(
        capability_id="builtin.browser.read",
        mode="transaction",
        allowed_tools=("browser_session_open", "browser_snapshot"),
        allow_ephemeral=True,
        allow_public_https_research=True,
    )
    interact_constraints = BrowserGuardrailConstraints(
        capability_id="builtin.browser.interact",
        mode="transaction",
        allowed_tools=("browser_upload",),
        attachment_ids=(attachment_id,),
    )
    conversation_id = f"conversation_{uuid4().hex}"
    source = _source(
        f"Upload {attachment_id} to submit the reviewed application.",
        conversation_id=conversation_id,
    )
    guardrails = tuple(
        compile_guardrail(
            capability_id=constraints.capability_id,
            schema_id=(
                "browser.read"
                if constraints.capability_id == "builtin.browser.read"
                else "browser.interact"
            ),
            schema_version=1,
            constraints=cast(JsonValue, constraints.model_dump(mode="json")),
            sources=(source,),
            summary="Exact browser fixture scope.",
        )
        for constraints in (read_constraints, interact_constraints)
    )
    runtime, compiler = await _compiler(settings, tmp_path, conversation_id=conversation_id)
    try:
        settings.browser.enabled = True
        settings.browser.background.enabled = True
        settings.browser.background.read_enabled = True
        settings.browser.background.interaction_enabled = True
        settings.browser.background.allow_ephemeral = True
        settings.browser.background.allow_public_https_research = True
        scope = await compiler._compile_browser_scope(guardrails)  # noqa: SLF001
    finally:
        await runtime.__aexit__(None, None, None)

    assert scope is not None
    assert scope.attachments[0].id == attachment_id
    assert scope.attachments[0].sha256 == written.entry.sha256
    assert scope.attachments[0].byte_count == written.entry.size
    assert scope.attachments[0].artifact_path == "application.txt"
    frozen = await _resolve_execution_browser_attachments(
        settings,
        project_root=tmp_path,
        profile_scope=settings.resolve_profile_scope(),
        scope=scope,
        attachment_ids=(attachment_id,),
    )
    assert frozen[0].content == b"reviewed application\n"

    source_path = tasks.artifact_root / task.id / "application.txt"
    source_path.write_text("changed after approval\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed after execution approval"):
        await _resolve_execution_browser_attachments(
            settings,
            project_root=tmp_path,
            profile_scope=settings.resolve_profile_scope(),
            scope=scope,
            attachment_ids=(attachment_id,),
        )


@pytest.mark.asyncio
async def test_ad_hoc_browser_scope_intersects_installation_owner_policy(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    constraints = BrowserGuardrailConstraints(
        capability_id="builtin.browser.read",
        mode="read_only",
        allowed_tools=("browser_session_open", "browser_navigate", "browser_snapshot"),
        allow_ephemeral=True,
        allow_public_https_research=True,
        private_origin_ceiling=("https://127.0.0.1:9443",),
    )
    conversation_id = f"conversation_{uuid4().hex}"
    source = _source("Research with the reviewed browser scope.", conversation_id=conversation_id)
    guardrail = compile_guardrail(
        capability_id=constraints.capability_id,
        schema_id="browser.read",
        schema_version=1,
        constraints=cast(JsonValue, constraints.model_dump(mode="json")),
        sources=(source,),
        summary="Exact browser fixture scope.",
    )
    runtime, compiler = await _compiler(settings, tmp_path, conversation_id=conversation_id)
    try:
        settings.browser.enabled = True
        settings.browser.background.enabled = True
        settings.browser.background.read_enabled = True
        with pytest.raises(ExecutionContractCompileError, match="ephemeral"):
            await compiler._compile_browser_scope((guardrail,))  # noqa: SLF001

        settings.browser.background.allow_ephemeral = True
        with pytest.raises(ExecutionContractCompileError, match="public HTTPS"):
            await compiler._compile_browser_scope((guardrail,))  # noqa: SLF001

        settings.browser.background.allow_public_https_research = True
        with pytest.raises(ExecutionContractCompileError, match="installation ceiling"):
            await compiler._compile_browser_scope((guardrail,))  # noqa: SLF001

        settings.browser.allowed_private_origins = ["https://127.0.0.1:9443"]
        scope = await compiler._compile_browser_scope((guardrail,))  # noqa: SLF001
    finally:
        await runtime.__aexit__(None, None, None)

    assert scope is not None
    assert scope.allow_ephemeral is True
    assert scope.allow_public_https_research is True
    assert scope.private_origin_ceiling == ("https://127.0.0.1:9443",)


@pytest.mark.asyncio
async def test_ad_hoc_persistent_resource_pins_reviewed_authenticated_origins(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    configured = settings.model_dump(mode="python")
    configured["browser"] = {
        "enabled": True,
        "background": {
            "enabled": True,
            "read_enabled": True,
            "interaction_enabled": True,
        },
    }
    configured["profile_configs"] = {
        "personal": {
            "browser": {
                "resources": {
                    "research": {
                        "kind": "persistent",
                        "description": "Dedicated research profile.",
                        "headless": True,
                    }
                }
            }
        }
    }
    configured_settings = RickySettings.model_validate(configured)
    resource = ProfileResourceRef(profile="personal", name="research")
    constraints = BrowserGuardrailConstraints(
        capability_id="builtin.browser.interact",
        mode="read_only",
        allowed_tools=("browser_session_open_resource",),
        resources=(resource,),
        authenticated_origins=(
            BrowserAuthenticatedOriginSelection(
                resource=resource,
                origins=("https://accounts.example.com",),
            ),
        ),
    )
    conversation_id = f"conversation_{uuid4().hex}"
    source = _source(
        "Use my research browser only on accounts.example.com.", conversation_id=conversation_id
    )
    guardrail = compile_guardrail(
        capability_id=constraints.capability_id,
        schema_id="browser.interact",
        schema_version=1,
        constraints=cast(JsonValue, constraints.model_dump(mode="json")),
        sources=(source,),
        summary="Exact authenticated browser fixture scope.",
    )
    runtime, compiler = await _compiler(settings, tmp_path, conversation_id=conversation_id)
    try:
        settings.browser = configured_settings.browser
        settings.profile_configs = configured_settings.profile_configs
        scope = await compiler._compile_browser_scope((guardrail,))  # noqa: SLF001
    finally:
        await runtime.__aexit__(None, None, None)

    assert scope is not None
    assert scope.resources[0].authenticated_origin_ceiling == ("https://accounts.example.com",)


@pytest.mark.asyncio
async def test_selected_skill_body_and_resources_are_pinned_independently(
    tmp_path: Path,
    bundled_root: Path,
) -> None:
    bundle = bundled_root / "skills" / "demo"
    bundle.mkdir(parents=True, exist_ok=True)
    source = bundle / "SKILL.md"
    resource = bundle / "reference.txt"
    source.write_text(
        "---\nname: demo\ndescription: Demo pinned skill\n---\nRead reference.txt when needed.\n",
        encoding="utf-8",
    )
    resource.write_text("immutable reference\n", encoding="utf-8")
    settings = _settings(tmp_path)
    task = await _task(settings)
    conversation_id = f"conversation_{uuid4().hex}"
    runtime, compiler = await _compiler(settings, tmp_path, conversation_id=conversation_id)
    try:
        draft = await compiler.review(
            AdHocExecutionProposal(
                task_id=task.id,
                expected_task_revision=task.revision,
                goal="Use the demo skill.",
                requested_capabilities=("bundled.skill.demo",),
            ),
            source=_source("Use the demo skill.", conversation_id=conversation_id),
        )
        contract = await compiler.compile(draft)
        source.unlink()
        resource.unlink()
        bundle.rmdir()
        pinned = load_pinned_runtime(settings, contract)
        pinned_bundle = Path(pinned.skill_bundle_paths["bundled/demo"])
        assert pinned.skill_instructions["bundled/demo"] == "Read reference.txt when needed."
        assert (pinned_bundle / "reference.txt").read_text(encoding="utf-8") == (
            "immutable reference\n"
        )
    finally:
        await runtime.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_background_runner_exposes_only_exact_contract_tools_and_rejects_drift(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    task = await _task(settings)
    conversation_id = f"conversation_{uuid4().hex}"
    runtime, compiler = await _compiler(settings, tmp_path, conversation_id=conversation_id)
    try:
        draft = await compiler.review(
            AdHocExecutionProposal(
                task_id=task.id,
                expected_task_revision=task.revision,
                goal="Inspect the project.",
                requested_capabilities=("builtin.project.read",),
            ),
            source=_source("Inspect the project.", conversation_id=conversation_id),
        )
        contract = await compiler.compile(draft)
        pinned = load_pinned_runtime(settings, contract)
        provider = _ContractProvider()
        run = await JobRunner(settings, project_root=tmp_path).run_spec(
            contract.job_spec(),
            contract.goal,
            source_digest=contract.digest,
            profile_scope=contract.profile_scope,
            provider=provider,
            pinned_runtime=pinned,
        )
        assert run.outcome == "succeeded"
        assert len(provider.requests) == 1
        assert {tool.name for tool in provider.requests[0].tools} == {
            "glob_search",
            "grep_search",
            "list_dir",
            "read_file",
        }

        drifted = pinned.model_copy(
            update={
                "tool_digests": {
                    **pinned.tool_digests,
                    "read_file": "0" * 64,
                }
            }
        )
        untouched = _ContractProvider()
        with pytest.raises(JobConfigurationError, match="contracted tool schema changed"):
            await JobRunner(settings, project_root=tmp_path).run_spec(
                contract.job_spec(),
                contract.goal,
                source_digest=contract.digest,
                profile_scope=contract.profile_scope,
                provider=untouched,
                pinned_runtime=drifted,
            )
        assert untouched.requests == []
    finally:
        await runtime.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_missing_guardrail_questions_then_proactive_values_avoid_redundant_prompt(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, guarded=True)
    task = await _task(settings)
    conversation_id = f"conversation_{uuid4().hex}"
    runtime, compiler = await _compiler(settings, tmp_path, conversation_id=conversation_id)
    try:
        initial = _source("Book a sandbox reservation.", conversation_id=conversation_id)
        base = {
            "task_id": task.id,
            "expected_task_revision": task.revision,
            "goal": "Book one sandbox reservation.",
            "requested_capabilities": ("builtin.sandbox.reservation",),
        }
        collecting = await compiler.review(AdHocExecutionProposal(**base), source=initial)
        assert collecting.status == "collecting_guardrails"
        assert collecting.pending_questions

        details_text = (
            "Use venue-1 (Venue One), party of 2, 2026-08-18 between 18:00 and "
            "19:00 America/Chicago, account alex@example.com, no deposit."
        )
        details = _source(details_text, conversation_id=conversation_id)
        awaiting = await compiler.review(
            AdHocGuardrailContinuation(
                action="supply_guardrails",
                draft_id=collecting.id,
                expected_draft_revision=collecting.revision,
                guardrails=(
                    GuardrailProposal(
                        capability_id="builtin.sandbox.reservation",
                        fields=_guardrail_fields(
                            {
                                "venue_id": "venue-1",
                                "venue_name": "Venue One",
                                "party_size": 2,
                                "local_date": "2026-08-18",
                                "window_start": "18:00",
                                "window_end": "19:00",
                                "timezone": "America/Chicago",
                                "account_identity": "alex@example.com",
                                "deposit_limit_minor": 0,
                            },
                            quote=details_text,
                        ),
                    ),
                ),
            ),
            source=details,
        )
        assert awaiting.status == "awaiting_confirmation"
        assert awaiting.pending_questions == ()
        assert awaiting.confirmation_summary is not None

        yes = _source("Yes.", conversation_id=conversation_id)
        ready = await compiler.review(
            AdHocConfirmation(
                action="confirm",
                draft_id=awaiting.id,
                expected_draft_revision=awaiting.revision,
            ),
            source=yes,
        )
        assert ready.status == "ready"
        assert ready.confirmation is not None
        assert ready.guardrails[0].source_message_ids == (details.message_id,)
        assert (
            await compiler.store.get_draft(
                ready.id,
                scope=ready.profile_scope,
            )
            == ready
        )
        contract = await compiler.compile(ready)
        assert contract.confirmations[0].source_message_id == yes.message_id
        assert contract.guardrails[0].capability_id == "builtin.sandbox.reservation"
        pinned = load_pinned_runtime(settings, contract)
        assert pinned.authorized_mutating_tools == ("sandbox_reserve",)

        authority = AuthorityStore(settings)
        grant = await ContractAuthorityCompiler(settings, store=authority).compile(
            contract,
            source=GrantSource(
                principal_id=yes.principal_id,
                transport="telegram",
                account="personal/owner",
                sender_id="1",
                destination_id="chat",
                platform_message_id="platform-yes",
                inbound_message_id=yes.message_id,
                conversation_id=conversation_id,
                text_digest=source_text_digest(yes.text_snapshot),
                text_snapshot=yes.text_snapshot,
                received_at=yes.received_at,
            ),
        )
        assert grant is not None
        assert grant.contract_id == contract.id
        assert grant.confirmations == contract.confirmations
        assert await authority.get(grant.id, scope=contract.profile_scope) == grant

        dispatcher = ExecutionDispatcher(
            settings,
            project_root=tmp_path,
            store=compiler.store,
            routes=_AllowRoutes(),  # type: ignore[arg-type]
            authority=authority,
        )
        request = await dispatcher.create_contract_execution_request(
            contract,
            request_key="contract-authority-test",
            grant_id=grant.id,
        )
        attached_draft = await compiler.store.get_draft(
            ready.id,
            scope=ready.profile_scope,
        )
        queued = attached_draft.model_copy(
            update={
                "status": "queued",
                "revision": attached_draft.revision + 1,
                "contract_id": contract.id,
                "request_id": request.id,
                "updated_at": datetime.now(UTC),
            }
        )
        await compiler.store.update_draft(
            queued,
            expected_revision=attached_draft.revision,
            kind="queued",
            summary=f"Queued execution request {request.id}",
            scope=ready.profile_scope,
        )
        attached = await authority.get(grant.id, scope=contract.profile_scope)
        assert request.contract_id == contract.id
        assert attached.execution_request_id == request.id
        audit = await GatewayAudit(
            settings,
            scope=contract.profile_scope,
        ).trace(request.id)
        present = {link.kind for link in audit.present}
        assert {
            "durable_task",
            "execution_draft",
            "guardrail",
            "confirmation",
            "execution_contract",
            "delegation_grant",
            "execution_request",
        } <= present
        assert details_text not in "\n".join(link.detail for link in audit.links)
    finally:
        await runtime.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_external_effect_capability_requires_positive_contract_budget(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, guarded=True)
    settings.agents.ad_hoc_background.execution.effect_calls = 0
    task = await _task(settings)
    conversation_id = f"conversation_{uuid4().hex}"
    runtime, compiler = await _compiler(settings, tmp_path, conversation_id=conversation_id)
    try:
        with pytest.raises(
            ExecutionContractCompileError,
            match="external-effect capabilities require a positive",
        ):
            await compiler.review(
                AdHocExecutionProposal(
                    task_id=task.id,
                    expected_task_revision=task.revision,
                    goal="Perform one external effect.",
                    requested_capabilities=("builtin.sandbox.reservation",),
                ),
                source=_source("Perform it.", conversation_id=conversation_id),
            )
    finally:
        await runtime.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_partial_guardrail_fields_survive_restart_and_reply_adds_only_missing_value(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, guarded=True)
    task = await _task(settings)
    conversation_id = f"conversation_{uuid4().hex}"
    runtime, compiler = await _compiler(settings, tmp_path, conversation_id=conversation_id)
    try:
        initial_text = (
            "Use venue-1 (Venue One) on 2026-08-18 between 18:00 and 19:00 "
            "America/Chicago, account alex@example.com, no deposit."
        )
        initial = _source(initial_text, conversation_id=conversation_id)
        collecting = await compiler.review(
            AdHocExecutionProposal(
                task_id=task.id,
                expected_task_revision=task.revision,
                goal="Book one sandbox reservation.",
                requested_capabilities=("builtin.sandbox.reservation",),
                guardrails=(
                    GuardrailProposal(
                        capability_id="builtin.sandbox.reservation",
                        fields=_guardrail_fields(
                            {
                                "venue_id": "venue-1",
                                "venue_name": "Venue One",
                                "local_date": "2026-08-18",
                                "window_start": "18:00",
                                "window_end": "19:00",
                                "timezone": "America/Chicago",
                                "account_identity": "alex@example.com",
                                "deposit_limit_minor": 0,
                            },
                            quote=initial_text,
                        ),
                    ),
                ),
            ),
            source=initial,
        )
        assert collecting.status == "collecting_guardrails"
        assert collecting.pending_questions == ("How many people is the reservation for?",)
        assert len(collecting.collected_guardrail_fields) == 8

        restarted_store = ExecutionStore(settings)
        await restarted_store.initialize()
        persisted = await restarted_store.get_draft(
            collecting.id,
            scope=collecting.profile_scope,
        )
        assert persisted.collected_guardrail_fields == collecting.collected_guardrail_fields
        with sqlite3.connect(restarted_store.db_path) as database:
            assert database.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            assert (
                database.execute(
                    "SELECT count(*) FROM execution_draft_guardrail_fields WHERE draft_id=?",
                    (collecting.id,),
                ).fetchone()[0]
                == 8
            )

        party_text = "The reservation is for 2 people."
        party_source = _source(party_text, conversation_id=conversation_id)
        awaiting = await compiler.review(
            AdHocGuardrailContinuation(
                action="supply_guardrails",
                draft_id=persisted.id,
                expected_draft_revision=persisted.revision,
                guardrails=(
                    GuardrailProposal(
                        capability_id="builtin.sandbox.reservation",
                        fields=(
                            GuardrailFieldProposal(
                                field="party_size",
                                value=2,
                                source_quote="for 2 people",
                            ),
                        ),
                    ),
                ),
            ),
            source=party_source,
        )

        assert awaiting.status == "awaiting_confirmation"
        assert awaiting.pending_questions == ()
        assert len(awaiting.collected_guardrail_fields) == 9
        fields = {item.field: item for item in awaiting.collected_guardrail_fields}
        assert fields["party_size"].source_message_id == party_source.message_id
        assert fields["window_start"].source_message_id == initial.message_id
        assert awaiting.guardrails[0].source_message_ids == (
            initial.message_id,
            party_source.message_id,
        )
        with sqlite3.connect(restarted_store.db_path) as database:
            assert (
                database.execute(
                    "SELECT count(*) FROM execution_draft_guardrail_fields WHERE draft_id=?",
                    (collecting.id,),
                ).fetchone()[0]
                == 9
            )

        correction_text = "Actually, make that 3 people."
        correction_source = _source(correction_text, conversation_id=conversation_id)
        corrected = await compiler.review(
            AdHocGuardrailContinuation(
                action="supply_guardrails",
                draft_id=awaiting.id,
                expected_draft_revision=awaiting.revision,
                guardrails=(
                    GuardrailProposal(
                        capability_id="builtin.sandbox.reservation",
                        fields=(
                            GuardrailFieldProposal(
                                field="party_size",
                                value=3,
                                source_quote="3 people",
                            ),
                        ),
                    ),
                ),
            ),
            source=correction_source,
        )
        corrected_fields = {item.field: item for item in corrected.collected_guardrail_fields}
        assert corrected.status == "awaiting_confirmation"
        assert corrected.confirmation_summary_digest != awaiting.confirmation_summary_digest
        assert corrected.confirmation is None
        assert corrected_fields["party_size"].value == 3
        assert corrected_fields["party_size"].source_message_id == correction_source.message_id
    finally:
        await runtime.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_complete_proactive_guardrail_reaches_confirmation_without_questions(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, guarded=True)
    task = await _task(settings)
    conversation_id = f"conversation_{uuid4().hex}"
    runtime, compiler = await _compiler(settings, tmp_path, conversation_id=conversation_id)
    try:
        text = (
            "Reserve Venue One (venue-1) for 2 people on 2026-08-18 between 18:00 "
            "and 19:00 America/Chicago using alex@example.com with no deposit."
        )
        source = _source(text, conversation_id=conversation_id)
        draft = await compiler.review(
            AdHocExecutionProposal(
                task_id=task.id,
                expected_task_revision=task.revision,
                goal="Book one sandbox reservation.",
                requested_capabilities=("builtin.sandbox.reservation",),
                guardrails=(
                    GuardrailProposal(
                        capability_id="builtin.sandbox.reservation",
                        fields=_guardrail_fields(
                            {
                                "venue_id": "venue-1",
                                "venue_name": "Venue One",
                                "party_size": 2,
                                "local_date": "2026-08-18",
                                "window_start": "18:00",
                                "window_end": "19:00",
                                "timezone": "America/Chicago",
                                "account_identity": "alex@example.com",
                                "deposit_limit_minor": 0,
                            },
                            quote=text,
                        ),
                    ),
                ),
            ),
            source=source,
        )

        assert draft.status == "awaiting_confirmation"
        assert draft.pending_questions == ()
        assert draft.confirmation_summary is not None
        assert "Venue One (venue-1) for 2" in draft.confirmation_summary
    finally:
        await runtime.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_structural_guardrail_accepts_interpretation_without_citation_matching(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, guarded=True)
    task = await _task(settings)
    conversation_id = f"conversation_{uuid4().hex}"
    runtime, compiler = await _compiler(settings, tmp_path, conversation_id=conversation_id)
    try:
        text = (
            "Reserve Venue One (venue-1) for 2 people on 2026-08-18 between 6 PM "
            "and 7 PM Chicago time using alex@example.com. Do not permit any deposit."
        )
        source = _source(text, conversation_id=conversation_id)
        draft = await compiler.review(
            AdHocExecutionProposal(
                task_id=task.id,
                expected_task_revision=task.revision,
                goal="Book one sandbox reservation.",
                requested_capabilities=("builtin.sandbox.reservation",),
                guardrails=(
                    GuardrailProposal(
                        capability_id="builtin.sandbox.reservation",
                        fields=_guardrail_fields(
                            {
                                "venue_id": "venue-1",
                                "venue_name": "Venue One",
                                "party_size": 2,
                                "local_date": "2026-08-18",
                                "window_start": "18:00",
                                "window_end": "19:00",
                                "timezone": "America/Chicago",
                                "account_identity": "alex@example.com",
                                "deposit_limit_minor": 0,
                            },
                            quote="foreground interpretation for audit",
                        ),
                    ),
                ),
            ),
            source=source,
        )

        assert draft.status == "awaiting_confirmation"
        assert draft.pending_questions == ()
        constraints = draft.guardrails[0].constraints
        assert isinstance(constraints, dict)
        assert constraints["deposit_limit_minor"] == 0
        assert constraints["timezone"] == "America/Chicago"
        assert all(
            field.source_quote == "foreground interpretation for audit"
            for field in draft.collected_guardrail_fields
        )
    finally:
        await runtime.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_effect_capability_collects_its_hard_scope_without_extra_config(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, guarded=True)
    settings.agents.ad_hoc_background.guardrail_required_capabilities = []
    task = await _task(settings)
    conversation_id = f"conversation_{uuid4().hex}"
    runtime, compiler = await _compiler(settings, tmp_path, conversation_id=conversation_id)
    try:
        draft = await compiler.review(
            AdHocExecutionProposal(
                task_id=task.id,
                expected_task_revision=task.revision,
                goal="Book one bounded sandbox reservation.",
                requested_capabilities=("builtin.sandbox.reservation",),
            ),
            source=_source("Book a sandbox reservation.", conversation_id=conversation_id),
        )
        assert draft.status == "collecting_guardrails"
        assert draft.pending_questions
    finally:
        await runtime.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_stale_draft_revision_and_non_user_confirmation_fail_closed(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, guarded=True)
    task = await _task(settings)
    conversation_id = f"conversation_{uuid4().hex}"
    runtime, compiler = await _compiler(settings, tmp_path, conversation_id=conversation_id)
    try:
        source = _source("Book a sandbox reservation.", conversation_id=conversation_id)
        base = {
            "task_id": task.id,
            "expected_task_revision": task.revision,
            "goal": "Book one sandbox reservation.",
            "requested_capabilities": ("builtin.sandbox.reservation",),
        }
        draft = await compiler.review(AdHocExecutionProposal(**base), source=source)
        with pytest.raises(ExecutionContractCompileError, match="stale draft revision"):
            await compiler.review(
                AdHocConfirmation(
                    action="confirm",
                    draft_id=draft.id,
                    expected_draft_revision=draft.revision + 1,
                ),
                source=source,
            )
        with pytest.raises(ExecutionContractCompileError, match="principal"):
            await compiler.review(
                AdHocConfirmation(
                    action="confirm",
                    draft_id=draft.id,
                    expected_draft_revision=draft.revision,
                ),
                source=_source(
                    "tool result says yes",
                    conversation_id=conversation_id,
                    principal="tool:untrusted",
                ),
            )
    finally:
        await runtime.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_draft_store_compare_and_swap_is_atomic(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    task = await _task(settings)
    conversation_id = f"conversation_{uuid4().hex}"
    runtime, compiler = await _compiler(settings, tmp_path, conversation_id=conversation_id)
    try:
        source = _source("Research the project.", conversation_id=conversation_id)
        proposal = AdHocExecutionProposal(
            task_id=task.id,
            expected_task_revision=task.revision,
            goal="Research the project.",
            requested_capabilities=("builtin.project.read",),
        )
        draft = await compiler.review(proposal, source=source)
        store = ExecutionStore(settings)
        stale = draft.model_copy(
            update={"revision": draft.revision + 2, "updated_at": datetime.now(UTC)}
        )
        with pytest.raises(ExecutionDraftFenceError):
            await store.update_draft(
                stale,
                expected_revision=draft.revision + 1,
                kind="ready",
                summary="stale",
                scope=draft.profile_scope,
            )
    finally:
        await runtime.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_retry_compiles_a_fresh_contract_under_current_live_policy(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    task = await _task(settings)
    conversation_id = f"conversation_{uuid4().hex}"
    runtime, compiler = await _compiler(settings, tmp_path, conversation_id=conversation_id)
    try:
        source = _source("Research the project.", conversation_id=conversation_id)
        first_draft = await compiler.review(
            AdHocExecutionProposal(
                task_id=task.id,
                expected_task_revision=task.revision,
                goal="Research the project.",
                requested_capabilities=("builtin.project.read",),
            ),
            source=source,
        )
        first_contract = await compiler.compile(first_draft)
        store = ExecutionStore(settings)
        dispatcher = ExecutionDispatcher(
            settings,
            project_root=tmp_path,
            store=store,
            routes=_AllowRoutes(),  # type: ignore[arg-type]
        )
        first = await dispatcher.create_contract_execution_request(
            first_contract,
            request_key="first-attempt",
        )
        claimed = (
            await store.claim(
                scope=first_contract.profile_scope,
                worker_id="test-worker",
                limit=1,
            )
        )[0]
        assert claimed.id == first.id and claimed.claim_token is not None
        await store.start(
            first.id,
            scope=first_contract.profile_scope,
            token=claimed.claim_token,
            fence=claimed.claim_fence,
            run_id=f"jobrun_{uuid4().hex}",
        )
        await store.finish(
            first.id,
            scope=first_contract.profile_scope,
            token=claimed.claim_token,
            fence=claimed.claim_fence,
            status="failed",
            error="first attempt failed",
        )

        draft = await compiler.review(
            AdHocExecutionProposal(
                task_id=task.id,
                expected_task_revision=task.revision,
                retry_of=first.id,
                goal="Retry with current capability policy.",
                requested_capabilities=("builtin.project.read",),
            ),
            source=_source(
                "Retry with current capability policy.", conversation_id=conversation_id
            ),
        )
        contract = await compiler.compile(draft)
        request = await dispatcher.create_contract_execution_request(
            contract,
            request_key="compiled-retry",
        )
        assert request.parent_request_id == first.id
        assert request.contract_id == contract.id
    finally:
        await runtime.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_recovery_distinguishes_resumable_draft_and_unqueued_contract(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    task = await _task(settings)
    conversation_id = f"conversation_{uuid4().hex}"
    runtime, compiler = await _compiler(settings, tmp_path, conversation_id=conversation_id)
    try:
        draft = await compiler.review(
            AdHocExecutionProposal(
                task_id=task.id,
                expected_task_revision=task.revision,
                goal="Research without a profile.",
                requested_capabilities=("builtin.project.read",),
            ),
            source=_source("Research without a profile.", conversation_id=conversation_id),
        )
        contract = await compiler.compile(draft)
        assert await compiler.compile(draft) == contract
        attached = await compiler.store.get_draft(
            draft.id,
            scope=draft.profile_scope,
        )
        assert attached.contract_id == contract.id
        assert await compiler.compile(attached) == contract
        assert await compiler.store.list_contracts(
            scope=contract.profile_scope,
            limit=10,
        ) == [contract]

        inspected = await GatewayRecovery(
            settings,
            scope=contract.profile_scope,
        ).inspect(now=datetime.now(UTC))

        draft_actions = inspected.by_subsystem("execution_draft")
        assert any(
            item.record_id == draft.id
            and item.to_state == "ready"
            and item.disposition == "requires_review"
            for item in draft_actions
        )
        contract_actions = inspected.by_subsystem("execution_contract")
        assert any(
            item.record_id == contract.id
            and item.to_state == "awaiting_submission_review"
            and item.disposition == "requires_review"
            for item in contract_actions
        )
        assert (
            await compiler.store.get_draft(draft.id, scope=draft.profile_scope)
        ).status == "ready"
        assert (
            await compiler.store.request_for_contract(
                contract.id,
                scope=contract.profile_scope,
            )
            is None
        )
    finally:
        await runtime.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_retention_protects_every_authenticated_draft_source(tmp_path: Path) -> None:
    base = _settings(tmp_path)
    settings = base.model_copy(
        update={
            "gateway": base.gateway.model_copy(
                update={
                    "retention": GatewayRetentionSettings(
                        enabled=True,
                        min_age_seconds=0,
                        inbound_messages=0,
                    )
                }
            )
        }
    )
    old = datetime(2020, 1, 1, tzinfo=UTC)
    conversation_id = f"conversation_{uuid4().hex}"
    source = _source("Research this safely.", conversation_id=conversation_id)
    source = source.model_copy(update={"received_at": old})
    inbound = InboundMessage(
        id=source.message_id,
        transport="telegram",
        account="personal/owner",
        update_id="retention-source",
        destination_id="chat",
        sender_id="1",
        platform_message_id="platform-retention-source",
        text=source.text_snapshot,
        received_at=old,
        status="pending",
    )
    messaging = MessagingStore(settings)
    await messaging.initialize()
    await messaging.ingest(
        ReceiveBatch(
            transport="telegram",
            account="personal/owner",
            updates=[ReceivedUpdate(update_id=inbound.update_id, message=inbound)],
            next_cursor=TransportCursor(
                transport="telegram", account="personal/owner", value=inbound.update_id
            ),
        )
    )
    claim = await messaging.claim_inbox(inbound.id, owner="test", lease_seconds=60)
    await messaging.finish_inbox(claim, status="processed")
    task = await _task(settings)
    runtime, compiler = await _compiler(settings, tmp_path, conversation_id=conversation_id)
    try:
        await compiler.review(
            AdHocExecutionProposal(
                task_id=task.id,
                expected_task_revision=task.revision,
                goal="Research this safely.",
                requested_capabilities=("builtin.project.read",),
            ),
            source=source,
        )

        plan = await GatewayRetention(
            settings,
            scope=settings.resolve_profile_scope(),
        ).apply()

        inbound_group = plan.group("inbound_messages")
        assert inbound_group is not None
        assert source.message_id in inbound_group.protected_ids
        assert source.message_id not in inbound_group.removable_ids
        assert (await messaging.get_inbox(source.message_id)).id == source.message_id
    finally:
        await runtime.__aexit__(None, None, None)
