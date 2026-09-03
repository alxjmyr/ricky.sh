"""Reusable permission-contract tests for tools requiring fresh review."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel, ConfigDict

from ricky.agent import AgentSession, PermissionGrant
from ricky.agent.events import PermissionRequestedEvent
from ricky.agent.tool_dispatch import (
    GateOutcome,
    PermissionResponder,
    decide_tool_permission,
    deny_permission,
)
from ricky.config import RickySettings
from ricky.llm import ToolCallPart
from ricky.permissions import GrantScope, PermissionEngine, PermissionResponse, Policy, PolicyRule
from ricky.tools import (
    EffectIdentity,
    EffectReceipt,
    PreparedEffect,
    Risk,
    ToolContext,
    ToolRegistry,
    ToolResult,
    make_effect_identity,
)


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    target: str


@dataclass(frozen=True)
class _Prepared:
    tool_name: str
    identity: EffectIdentity
    permission_summary: str | None


class _PreparedTool:
    name: ClassVar[str] = "reviewed_commit"
    description: ClassVar[str] = "Perform one synthetic reviewed commit."
    Params: ClassVar[type[BaseModel]] = _Params
    risk: ClassVar[Risk] = "mutating"
    capability_id = None
    effect_kind = "external"
    unattended = "forbidden"
    state_guard_id = None

    def __init__(self) -> None:
        self.prepare_count = 0
        self.dispatch_count = 0

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        del ctx
        return make_effect_identity(
            operation=self.name,
            target=str(args["target"]),
            occurrence="one",
            summary="Perform the synthetic commit",
        )

    def permission_scope(self, args: dict[str, object], ctx: ToolContext) -> GrantScope | None:
        del ctx
        return GrantScope(
            params_equal={"target": args["target"]},
            label="this target",
            allow_unconstrained=True,
        )

    async def prepare_effect(self, args: dict[str, object], ctx: ToolContext) -> PreparedEffect:
        self.prepare_count += 1
        return _Prepared(
            tool_name=self.name,
            identity=self.effect_identity(args, ctx),
            permission_summary=f"Commit to {args['target']}",
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del params, ctx
        raise AssertionError("prepared tools must dispatch through run_prepared")

    async def run_prepared(
        self,
        params: BaseModel,
        prepared: PreparedEffect,
        ctx: ToolContext,
    ) -> ToolResult:
        del params, prepared, ctx
        self.dispatch_count += 1
        return ToolResult(
            content="committed",
            effect_receipt=EffectReceipt(disposition="performed"),
        )


class _FreshPreparedTool(_PreparedTool):
    review_mode = "fresh"


def _setup(tmp_path: Path, tool: _PreparedTool) -> tuple[AgentSession, ToolRegistry, ToolContext]:
    settings = RickySettings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    return (
        session,
        ToolRegistry([tool]),
        ToolContext(cwd=tmp_path, settings=settings, session=session),
    )


async def _decide(
    *,
    session: AgentSession,
    registry: ToolRegistry,
    engine: PermissionEngine,
    responder: PermissionResponder,
    ctx: ToolContext,
) -> GateOutcome:
    return await decide_tool_permission(
        session=session,
        registry=registry,
        engine=engine,
        responder=responder,
        turn_id="turn_test",
        call=ToolCallPart(
            id="call_test",
            name="reviewed_commit",
            args={"target": "merchant.example"},
        ),
        ctx=ctx,
    )


@pytest.mark.asyncio
async def test_fresh_review_converts_an_explicit_allow_rule_to_an_ask(tmp_path: Path) -> None:
    tool = _FreshPreparedTool()
    session, registry, ctx = _setup(tmp_path, tool)
    requests: list[PermissionRequestedEvent] = []

    async def responder(event: PermissionRequestedEvent) -> PermissionResponse:
        assert tool.prepare_count == 1
        requests.append(event)
        return PermissionResponse(decision="allow")

    outcome = await _decide(
        session=session,
        registry=registry,
        engine=PermissionEngine(Policy(rules=[PolicyRule(tool_name=tool.name, decision="allow")])),
        responder=responder,
        ctx=ctx,
    )

    assert outcome.decision == "allow"
    assert tool.prepare_count == 1
    assert len(requests) == 1
    assert requests[0].reason == "fresh interactive review required"
    assert requests[0].offered_grants == []
    assert outcome.normalized_args is not None
    assert outcome.prepared_effect is not None
    result = await registry.dispatch_prepared(
        tool.name,
        outcome.normalized_args,
        outcome.prepared_effect,
        ctx,
    )
    assert result.effect_receipt is not None
    assert result.effect_receipt.disposition == "performed"
    assert tool.prepare_count == 1
    assert tool.dispatch_count == 1


@pytest.mark.asyncio
async def test_fresh_review_converts_a_matching_session_grant_to_an_ask(
    tmp_path: Path,
) -> None:
    tool = _FreshPreparedTool()
    session, registry, ctx = _setup(tmp_path, tool)
    session.permission_grants.append(
        PermissionGrant(tool_name=tool.name, params_equal={"target": "merchant.example"})
    )
    asks = 0

    async def responder(_event: PermissionRequestedEvent) -> PermissionResponse:
        nonlocal asks
        asks += 1
        return PermissionResponse(decision="allow")

    outcome = await _decide(
        session=session,
        registry=registry,
        engine=PermissionEngine(),
        responder=responder,
        ctx=ctx,
    )

    assert outcome.decision == "allow"
    assert asks == 1
    assert tool.prepare_count == 1
    assert len(session.permission_grants) == 1


@pytest.mark.asyncio
async def test_fresh_review_explicit_deny_prevents_preparation(tmp_path: Path) -> None:
    tool = _FreshPreparedTool()
    session, registry, ctx = _setup(tmp_path, tool)

    async def responder(_event: PermissionRequestedEvent) -> PermissionResponse:
        raise AssertionError("a denied fresh-review tool must not prompt")

    outcome = await _decide(
        session=session,
        registry=registry,
        engine=PermissionEngine(Policy(rules=[PolicyRule(tool_name=tool.name, decision="deny")])),
        responder=responder,
        ctx=ctx,
    )

    assert outcome.decision == "deny"
    assert tool.prepare_count == 0
    assert not any(isinstance(event, PermissionRequestedEvent) for event in outcome.events)


@pytest.mark.asyncio
async def test_fresh_review_does_not_honor_a_forged_remembered_grant(tmp_path: Path) -> None:
    tool = _FreshPreparedTool()
    session, registry, ctx = _setup(tmp_path, tool)

    async def responder(event: PermissionRequestedEvent) -> PermissionResponse:
        assert event.offered_grants == []
        return PermissionResponse(decision="allow", grant="tool")

    outcome = await _decide(
        session=session,
        registry=registry,
        engine=PermissionEngine(),
        responder=responder,
        ctx=ctx,
    )

    assert outcome.decision == "allow"
    assert outcome.remembered_grant is None
    assert session.permission_grants == []


@pytest.mark.asyncio
async def test_fresh_review_without_an_interactive_responder_denies(tmp_path: Path) -> None:
    tool = _FreshPreparedTool()
    session, registry, ctx = _setup(tmp_path, tool)

    outcome = await _decide(
        session=session,
        registry=registry,
        engine=PermissionEngine(Policy(rules=[PolicyRule(tool_name=tool.name, decision="allow")])),
        responder=deny_permission,
        ctx=ctx,
    )

    assert outcome.decision == "deny"
    assert tool.prepare_count == 1
    assert tool.dispatch_count == 0


@pytest.mark.asyncio
async def test_cancelling_fresh_review_drops_the_prepared_effect(tmp_path: Path) -> None:
    tool = _FreshPreparedTool()
    session, registry, ctx = _setup(tmp_path, tool)

    async def responder(_event: PermissionRequestedEvent) -> PermissionResponse:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _decide(
            session=session,
            registry=registry,
            engine=PermissionEngine(),
            responder=responder,
            ctx=ctx,
        )

    assert tool.prepare_count == 1
    assert tool.dispatch_count == 0
    assert session.permission_grants == []


@pytest.mark.asyncio
async def test_default_policy_review_mode_preserves_allow_and_grant_behavior(
    tmp_path: Path,
) -> None:
    tool = _PreparedTool()
    session, registry, ctx = _setup(tmp_path, tool)
    session.permission_grants.append(
        PermissionGrant(tool_name=tool.name, params_equal={"target": "merchant.example"})
    )

    async def responder(_event: PermissionRequestedEvent) -> PermissionResponse:
        raise AssertionError("a normal tool's matching grant must still skip review")

    outcome = await _decide(
        session=session,
        registry=registry,
        engine=PermissionEngine(),
        responder=responder,
        ctx=ctx,
    )

    assert registry.review_mode(tool.name) == "policy"
    assert outcome.decision == "allow"
    assert tool.prepare_count == 1
    assert not any(isinstance(event, PermissionRequestedEvent) for event in outcome.events)
