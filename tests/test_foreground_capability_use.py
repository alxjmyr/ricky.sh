from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel, ConfigDict, Field

from ricky.agent.session import AgentSession
from ricky.capabilities import (
    AuthenticatedSource,
    CapabilitySpec,
    CollectedGuardrailField,
    CompiledGuardrail,
    GuardrailDecision,
    GuardrailFieldDecision,
    GuardrailFieldProposal,
    GuardrailIntakeField,
    GuardrailIntakeSpec,
    GuardrailProposal,
    GuardrailRegistry,
    GuardrailUsage,
    GuardrailVerdict,
    build_capability_registry,
    compile_guardrail,
    resolve_capability_policy,
)
from ricky.capabilities.enforcement import GuardrailEnforcementError, build_guardrailed_tools
from ricky.config import RickySettings
from ricky.executions.drafts import DraftActivityKind, ExecutionDraft
from ricky.executions.store import ExecutionStore
from ricky.gateway.capability_use import (
    CapabilityUseProposal,
    ForegroundAuthorizedTool,
    ForegroundCapabilityUseManager,
)
from ricky.gateway.conversations import build_gateway_runtime
from ricky.gateway.recovery import GatewayRecovery
from ricky.gateway.types import Conversation, ConversationKey, GatewayActivity
from ricky.messaging.types import InboundMessage
from ricky.skills.registry import SkillRegistry
from ricky.tools import Tool, ToolContext, ToolResult
from ricky.tools.base import Risk

NOW = datetime(2026, 8, 17, 15, tzinfo=UTC)


class _Provider:
    name = "test"

    async def stream(self, request):  # type: ignore[no-untyped-def]
        del request
        if False:
            yield None

    async def aclose(self) -> None:
        return None


class EchoParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str = Field(min_length=1)


class EchoTool:
    name: ClassVar[str] = "echo_read"
    description: ClassVar[str] = "Return one exact test value."
    Params: ClassVar[type[BaseModel]] = EchoParams
    risk: ClassVar[Risk] = "read_only"
    capability_id = "builtin.test.read"
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        parsed = EchoParams.model_validate(params)
        self.calls += 1
        return ToolResult(content=parsed.text)


class NestedEchoPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


class NestedEchoParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    payload: NestedEchoPayload


class NestedEchoTool(EchoTool):
    Params: ClassVar[type[BaseModel]] = NestedEchoParams

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        parsed = NestedEchoParams.model_validate(params)
        self.calls += 1
        return ToolResult(content=parsed.payload.text)


class PrefixGuardrail:
    capability_id: ClassVar[str] = "builtin.test.read"
    schema_id: ClassVar[str] = "test.prefix"
    schema_version: ClassVar[int] = 1
    tools: ClassVar[frozenset[str]] = frozenset({"echo_read"})
    intake_spec: ClassVar[GuardrailIntakeSpec] = GuardrailIntakeSpec(
        schema_id=schema_id,
        schema_version=schema_version,
        fields=(
            GuardrailIntakeField(
                name="prefix",
                value_type="string",
                description="Exact permitted text prefix.",
                question="Which exact text prefix is allowed?",
            ),
        ),
    )

    def normalize_field(
        self,
        proposal: GuardrailFieldProposal,
    ) -> GuardrailFieldDecision:
        if proposal.field != "prefix":
            return GuardrailFieldDecision(accepted=False, reason="unknown prefix field")
        if not isinstance(proposal.value, str) or not proposal.value:
            return GuardrailFieldDecision(
                accepted=False, question="Which exact text prefix is allowed?"
            )
        return GuardrailFieldDecision(accepted=True, value=proposal.value)

    def validate_collected(
        self,
        fields: tuple[CollectedGuardrailField, ...],
        sources: tuple[AuthenticatedSource, ...],
    ) -> GuardrailDecision:
        by_source = {source.message_id: source for source in sources}
        prefix_field = next((item for item in fields if item.field == "prefix"), None)
        if prefix_field is None:
            return GuardrailDecision(questions=("Which exact text prefix is allowed?",))
        source = by_source.get(prefix_field.source_message_id)
        if source is None or source.text_digest != prefix_field.source_text_digest:
            return GuardrailDecision(reason="guardrail lacks authenticated turn provenance")
        prefix = prefix_field.value
        if not isinstance(prefix, str):
            return GuardrailDecision(questions=("Which exact text prefix is allowed?",))
        return GuardrailDecision(
            guardrail=compile_guardrail(
                capability_id=self.capability_id,
                schema_id=self.schema_id,
                schema_version=self.schema_version,
                constraints={"prefix": prefix},
                sources=(source,),
                summary=f"Text must start with {prefix}.",
            )
        )

    def summarize(self, guardrail: CompiledGuardrail) -> str:
        return guardrail.summary

    def evaluate_call(
        self,
        guardrail: CompiledGuardrail,
        tool_name: str,
        args: dict[str, object],
        usage: GuardrailUsage,
    ) -> GuardrailVerdict:
        del tool_name
        constraints = guardrail.constraints
        prefix = constraints.get("prefix") if isinstance(constraints, dict) else None
        text = args.get("text")
        allowed = (
            usage.calls < 1
            and isinstance(prefix, str)
            and isinstance(text, str)
            and text.startswith(prefix)
        )
        return GuardrailVerdict(
            allowed=allowed,
            reason="within prefix" if allowed else "outside prefix",
        )


def _settings(tmp_path: Path, *, guarded: bool = False) -> RickySettings:
    foreground = {
        "confirmation_required_capabilities": ["builtin.test.read"],
        "guardrail_required_capabilities": (["builtin.test.read"] if guarded else []),
    }
    return RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / "project"),
            "memory": {"enabled": False},
            "workflow": {"enabled": False},
            "agents": {"gateway_foreground": foreground},
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
                    }
                }
            },
        }
    )


def _conversation(session: AgentSession) -> Conversation:
    return Conversation(
        id="conversation_" + "a" * 32,
        key=ConversationKey(transport="telegram", account="personal/owner", destination_id="chat"),
        session_id=session.id,
        route_name="owner",
        provider=session.provider,
        model=session.model,
        profile_scope=session.profile_scope,
        status="active",
        revision=0,
        created_at=NOW,
        updated_at=NOW,
    )


def _inbound(suffix: str, text: str) -> InboundMessage:
    return InboundMessage(
        id="inbound_" + suffix * 32,
        transport="telegram",
        account="personal/owner",
        update_id=suffix,
        destination_id="chat",
        sender_id="user",
        platform_message_id=f"platform-{suffix}",
        text=text,
        received_at=NOW,
        status="pending",
    )


def _manager(
    settings: RickySettings,
    tool: Tool,
    *,
    session: AgentSession,
    inbound: InboundMessage,
    guardrails: GuardrailRegistry | None = None,
) -> ForegroundCapabilityUseManager:
    route = settings.gateway.routes["owner"]
    registry = build_capability_registry(
        [tool],
        SkillRegistry(),
        capability_specs=(
            CapabilitySpec(
                id="builtin.test.read",
                owner="builtin",
                description="Test read capability.",
            ),
        ),
    )
    decisions = {
        item.capability_id: item
        for item in resolve_capability_policy(
            registry,
            settings.agents.gateway_foreground,
            route=route,
        )
    }
    return ForegroundCapabilityUseManager(
        settings,
        registry=registry,
        guardrails=guardrails or GuardrailRegistry(),
        decisions=decisions,
        tools={tool.name: tool},
        route=route,
        conversation=_conversation(session),
        inbound=inbound,
        store=ExecutionStore(settings),
    )


@pytest.mark.asyncio
async def test_foreground_confirmation_is_exact_durable_and_one_time(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="test-model",
    )
    tool = EchoTool()
    initial = _manager(settings, tool, session=session, inbound=_inbound("b", "Echo hello."))
    proposal = CapabilityUseProposal(tool_name=tool.name, arguments={"text": "hello"})

    awaiting = await initial.review(proposal)
    assert await initial.review(proposal) == awaiting
    assert awaiting.target == "gateway_foreground"
    assert awaiting.status == "awaiting_confirmation"
    assert awaiting.foreground_call is not None
    assert "hello" in (awaiting.confirmation_summary or "")
    assert ExecutionDraft.model_validate_json(awaiting.model_dump_json()) == awaiting

    confirming = _manager(settings, tool, session=session, inbound=_inbound("c", "Yes."))
    ready = await confirming.review(
        CapabilityUseProposal(
            tool_name=tool.name,
            arguments={"text": "hello"},
            draft_id=awaiting.id,
            expected_draft_revision=awaiting.revision,
            confirm=True,
        )
    )
    assert ready.status == "ready"
    assert ready.confirmation is not None
    assert ready.confirmation.source_message_id == "inbound_" + "c" * 32

    wrapped = ForegroundAuthorizedTool(tool, confirming)
    context = ToolContext(cwd=tmp_path, settings=settings, session=session)
    result = await wrapped.run(EchoParams(text="hello"), context)
    assert result.content == "hello"
    assert tool.calls == 1
    assert (
        await ExecutionStore(settings).get_draft(ready.id, scope=session.profile_scope)
    ).status == "completed"

    denied = await wrapped.run(EchoParams(text="hello"), context)
    assert denied.is_error
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_terminal_foreground_draft_cannot_be_continued_or_replayed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="test-model",
    )
    tool = EchoTool()
    base = {"tool_name": tool.name, "arguments": {"text": "hello"}}
    initial = _manager(settings, tool, session=session, inbound=_inbound("5", "Echo hello."))
    awaiting = await initial.review(CapabilityUseProposal(**base))
    confirming = _manager(settings, tool, session=session, inbound=_inbound("6", "Yes."))
    ready = await confirming.review(
        CapabilityUseProposal(
            **base,
            draft_id=awaiting.id,
            expected_draft_revision=awaiting.revision,
            confirm=True,
        )
    )
    wrapped = ForegroundAuthorizedTool(tool, confirming)
    result = await wrapped.run(
        EchoParams(text="hello"),
        ToolContext(cwd=tmp_path, settings=settings, session=session),
    )
    assert not result.is_error
    completed = await ExecutionStore(settings).get_draft(ready.id, scope=session.profile_scope)
    before = completed.model_dump_json()

    continuation = _manager(
        settings,
        tool,
        session=session,
        inbound=_inbound("7", "Try that again."),
    )
    with pytest.raises(RuntimeError, match="completed; it cannot be continued"):
        await continuation.review(
            CapabilityUseProposal(
                **base,
                draft_id=completed.id,
                expected_draft_revision=completed.revision,
            )
        )

    stored = await ExecutionStore(settings).get_draft(completed.id, scope=session.profile_scope)
    assert stored.model_dump_json() == before
    assert tool.calls == 1


@pytest.mark.parametrize("status", ["completed", "uncertain", "expired", "cancelled", "rejected"])
async def test_every_terminal_foreground_state_rejects_continuation(
    tmp_path: Path,
    status: DraftActivityKind,
) -> None:
    settings = _settings(tmp_path)
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="test-model",
    )
    tool = EchoTool()
    proposal = CapabilityUseProposal(tool_name=tool.name, arguments={"text": "hello"})
    initial = _manager(settings, tool, session=session, inbound=_inbound("8", "Echo hello."))
    awaiting = await initial.review(proposal)
    terminal = awaiting.model_copy(
        update={
            "status": status,
            "revision": awaiting.revision + 1,
            "updated_at": datetime.now(UTC),
            "confirmation_required": False,
            "reason": f"test terminal state: {status}",
        }
    )
    await initial.store.update_draft(
        terminal,
        expected_revision=awaiting.revision,
        kind=status,
        summary=f"Entered {status}",
        scope=session.profile_scope,
    )

    continuation = _manager(
        settings,
        tool,
        session=session,
        inbound=_inbound("9", "Try that again."),
    )
    with pytest.raises(RuntimeError, match=rf"{status}; it cannot be continued"):
        await continuation.review(
            CapabilityUseProposal(
                tool_name=tool.name,
                arguments={"text": "hello"},
                draft_id=awaiting.id,
                expected_draft_revision=terminal.revision,
            )
        )

    assert (
        await initial.store.get_draft(awaiting.id, scope=session.profile_scope)
    ).status == status
    assert tool.calls == 0


@pytest.mark.asyncio
async def test_foreground_authority_digest_uses_canonical_nested_arguments(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="test-model",
    )
    tool = NestedEchoTool()
    initial = _manager(settings, tool, session=session, inbound=_inbound("1", "Echo hello."))
    awaiting = await initial.review(
        CapabilityUseProposal(
            tool_name=tool.name,
            arguments={"payload": '{"text":"hello"}'},
        )
    )
    confirming = _manager(settings, tool, session=session, inbound=_inbound("2", "Yes."))

    ready = await confirming.review(
        CapabilityUseProposal(
            tool_name=tool.name,
            arguments={"payload": {"text": "hello"}},
            draft_id=awaiting.id,
            expected_draft_revision=awaiting.revision,
            confirm=True,
        )
    )

    assert ready.status == "ready"
    wrapped = ForegroundAuthorizedTool(tool, confirming)
    result = await wrapped.run(
        NestedEchoParams(payload=NestedEchoPayload(text="hello")),
        ToolContext(cwd=tmp_path, settings=settings, session=session),
    )
    assert result.content == "hello"
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_foreground_confirmation_cannot_change_exact_arguments(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="test-model",
    )
    tool = EchoTool()
    initial = _manager(settings, tool, session=session, inbound=_inbound("d", "Echo hello."))
    awaiting = await initial.review(
        CapabilityUseProposal(tool_name=tool.name, arguments={"text": "hello"})
    )
    confirming = _manager(settings, tool, session=session, inbound=_inbound("e", "Yes."))
    with pytest.raises(RuntimeError, match="changes the exact proposed call"):
        await confirming.review(
            CapabilityUseProposal(
                tool_name=tool.name,
                arguments={"text": "wider"},
                draft_id=awaiting.id,
                expected_draft_revision=awaiting.revision,
                confirm=True,
            )
        )


@pytest.mark.asyncio
async def test_proactive_foreground_guardrail_skips_questions_but_still_confirms(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, guarded=True)
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="test-model",
    )
    tool = EchoTool()
    guardrails = GuardrailRegistry((PrefixGuardrail(),))
    initial = _manager(
        settings,
        tool,
        session=session,
        inbound=_inbound("f", "Allow prefix safe: and echo safe:value."),
        guardrails=guardrails,
    )
    base = {"tool_name": tool.name, "arguments": {"text": "safe:value"}}
    collecting = await initial.review(CapabilityUseProposal(**base))
    assert collecting.status == "collecting_guardrails"
    assert collecting.pending_questions == ("Which exact text prefix is allowed?",)

    proactive = await initial.review(
        CapabilityUseProposal(
            **base,
            draft_id=collecting.id,
            expected_draft_revision=collecting.revision,
            guardrail=GuardrailProposal(
                capability_id="builtin.test.read",
                fields=(
                    GuardrailFieldProposal(
                        field="prefix",
                        value="safe:",
                        source_quote="prefix safe:",
                    ),
                ),
            ),
        )
    )
    assert proactive.status == "awaiting_confirmation"
    assert proactive.pending_questions == ()
    assert proactive.guardrails[0].source_message_ids == ("inbound_" + "f" * 32,)

    confirming = _manager(
        settings,
        tool,
        session=session,
        inbound=_inbound("1", "Yes."),
        guardrails=guardrails,
    )
    ready = await confirming.review(
        CapabilityUseProposal(
            **base,
            draft_id=proactive.id,
            expected_draft_revision=proactive.revision,
            confirm=True,
        )
    )
    wrapped = ForegroundAuthorizedTool(tool, confirming)
    result = await wrapped.run(
        EchoParams(text="safe:value"),
        ToolContext(cwd=tmp_path, settings=settings, session=session),
    )
    assert ready.confirmation is not None
    assert result.content == "safe:value"


@pytest.mark.asyncio
async def test_gateway_runtime_exposes_preparation_and_wraps_configured_tool(
    tmp_path: Path,
) -> None:
    settings = RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / "project"),
            "memory": {"enabled": False},
            "workflow": {"enabled": False},
            "agents": {
                "gateway_foreground": {
                    "confirmation_required_capabilities": ["builtin.project.read"]
                }
            },
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
        }
    )
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="test-model",
    )
    async with build_gateway_runtime(
        settings,
        session=session,
        conversation=_conversation(session),
        inbound=_inbound("2", "Read the project file."),
        activity=GatewayActivity(profile_label=session.profile_scope.label()),
        provider=_Provider(),  # type: ignore[arg-type]
    ) as runtime:
        tools = {tool.name: tool for tool in runtime.registry.tools()}
        assert "prepare_capability_use" in tools
        assert isinstance(tools["read_file"], ForegroundAuthorizedTool)


@pytest.mark.asyncio
async def test_background_guardrail_reservation_is_concurrency_safe(tmp_path: Path) -> None:
    settings = _settings(tmp_path, guarded=True)
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="test-model",
    )
    source = AuthenticatedSource(
        principal_id="telegram:owner:user",
        conversation_id="conversation_" + "a" * 32,
        message_id="inbound_" + "b" * 32,
        text_digest="c" * 64,
        text_snapshot="Allow prefix safe:.",
        received_at=NOW,
    )
    guardrail = compile_guardrail(
        capability_id="builtin.test.read",
        schema_id="test.prefix",
        schema_version=1,
        constraints={"prefix": "safe:"},
        sources=(source,),
        summary="Text must start with safe:.",
    )
    tool = EchoTool()
    wrapped = build_guardrailed_tools(
        [tool],
        guardrails=(guardrail,),
        guardrail_tools={"builtin.test.read": (tool.name,)},
        registry=GuardrailRegistry((PrefixGuardrail(),)),
    )[0]
    context = ToolContext(cwd=tmp_path, settings=settings, session=session)
    results = await asyncio.gather(
        wrapped.run(EchoParams(text="safe:one"), context),
        wrapped.run(EchoParams(text="safe:two"), context),
    )
    assert sum(not result.is_error for result in results) == 1
    assert tool.calls == 1


def test_background_guardrail_can_bind_an_explicit_tool_subset() -> None:
    class GroupGuardrail(PrefixGuardrail):
        tools: ClassVar[frozenset[str]] = frozenset({"echo_read", "echo_other"})

    source = AuthenticatedSource(
        principal_id="telegram:owner:user",
        conversation_id="conversation_" + "a" * 32,
        message_id="inbound_" + "b" * 32,
        text_digest="c" * 64,
        text_snapshot="Allow only the echo read tool.",
        received_at=NOW,
    )
    explicit = compile_guardrail(
        capability_id="builtin.test.read",
        schema_id="test.prefix",
        schema_version=1,
        constraints={"prefix": "safe:", "allowed_tools": ["echo_read"]},
        sources=(source,),
        summary="Only echo_read may use the safe prefix.",
    )
    tool = EchoTool()
    wrapped = build_guardrailed_tools(
        [tool],
        guardrails=(explicit,),
        guardrail_tools={"builtin.test.read": (tool.name,)},
        registry=GuardrailRegistry((GroupGuardrail(),)),
    )
    assert len(wrapped) == 1
    assert wrapped[0].name == "echo_read"

    unbound = explicit.model_copy(update={"constraints": {"prefix": "safe:"}})
    with pytest.raises(GuardrailEnforcementError, match="not bound"):
        build_guardrailed_tools(
            [tool],
            guardrails=(unbound,),
            guardrail_tools={"builtin.test.read": (tool.name,)},
            registry=GuardrailRegistry((GroupGuardrail(),)),
        )


@pytest.mark.asyncio
async def test_recovery_strands_an_interrupted_exact_foreground_call(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="test-model",
    )
    tool = EchoTool()
    initial = _manager(settings, tool, session=session, inbound=_inbound("3", "Echo safe."))
    base = {"tool_name": tool.name, "arguments": {"text": "safe"}}
    awaiting = await initial.review(CapabilityUseProposal(**base))
    confirming = _manager(settings, tool, session=session, inbound=_inbound("4", "Yes."))
    ready = await confirming.review(
        CapabilityUseProposal(
            **base,
            draft_id=awaiting.id,
            expected_draft_revision=awaiting.revision,
            confirm=True,
        )
    )
    reserved = await confirming.reserve(tool.name, {"text": "safe"})
    assert reserved is not None and reserved.status == "executing"

    plan = await GatewayRecovery(settings, scope=session.profile_scope).apply()

    action = next(
        item for item in plan.by_subsystem("execution_draft") if item.record_id == ready.id
    )
    assert action.to_state == "uncertain"
    assert (
        await ExecutionStore(settings).get_draft(ready.id, scope=session.profile_scope)
    ).status == "uncertain"
    denied = await ForegroundAuthorizedTool(tool, confirming).run(
        EchoParams(text="safe"),
        ToolContext(cwd=tmp_path, settings=settings, session=session),
    )
    assert denied.is_error
    assert tool.calls == 0
