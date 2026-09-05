"""Conversation-scoped inspection and revocation of delegated authority."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from authority_support import (
    durable_task,
    gateway_conversation,
    gateway_dispatcher,
    inbound,
    queue_contract_delegation,
    settings,
)
from ricky.agent.session import AgentSession
from ricky.authority.store import AuthorityStore
from ricky.authority.tools import GrantIdParams, delegation_management_tools
from ricky.config import RickySettings
from ricky.executions.dispatcher import ExecutionDispatcher
from ricky.tools import ToolContext


def _ctx(config: RickySettings) -> ToolContext:
    return ToolContext(
        cwd=Path.cwd(),
        settings=config,
        session=AgentSession.create(
            config,
            profile_scope=config.resolve_profile_scope(),
            provider="openrouter",
            model="test-model",
        ),
    )


async def _issued(config: RickySettings, tmp_path: Path) -> tuple[dict[str, Any], str, str]:
    conversation = await gateway_conversation(config)
    store = AuthorityStore(config)
    dispatcher = gateway_dispatcher(config, tmp_path)
    grant, _ = await queue_contract_delegation(
        config,
        tmp_path,
        dispatcher,
        task=await durable_task(config),
        message=inbound(),
    )
    tools = delegation_management_tools(
        dispatcher,
        store,
        conversation_id=conversation.id,
    )
    return {tool.name: tool for tool in tools}, conversation.id, grant.id


async def test_revocation_stops_future_work_and_is_conversation_scoped(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    tools, _, grant_id = await _issued(config, tmp_path)

    listed = await tools["list_delegations"].run(GrantIdParams.model_construct(), _ctx(config))
    assert grant_id in listed.content

    revoked = await tools["revoke_delegation"].run(
        GrantIdParams(grant_id=grant_id, reason="the plan changed"), _ctx(config)
    )
    assert not revoked.is_error
    assert "already confirmed effect is unchanged" in revoked.content

    store = AuthorityStore(config)
    await store.initialize()
    assert (
        await store.get(grant_id, scope=config.resolve_profile_scope("personal"))
    ).status == "revoked"

    repeat = await tools["revoke_delegation"].run(
        GrantIdParams(grant_id=grant_id, reason="again"), _ctx(config)
    )
    assert "already revoked" in repeat.content


async def test_another_conversation_cannot_revoke_this_grant(tmp_path: Path) -> None:
    config = settings(tmp_path)
    _, _, grant_id = await _issued(config, tmp_path)
    store = AuthorityStore(config)
    dispatcher = ExecutionDispatcher(config, project_root=tmp_path, authority=store)
    stranger = delegation_management_tools(
        dispatcher,
        store,
        conversation_id="conversation_" + "9" * 32,
    )
    revoke = next(tool for tool in stranger if tool.name == "revoke_delegation")
    result = await revoke.run(GrantIdParams(grant_id=grant_id, reason="not mine"), _ctx(config))
    assert result.is_error
    assert "not controlled by this conversation" in result.content


def test_authority_cli_lists_shows_revokes_and_traces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider-free commands keep contract-bound grants inspectable."""

    import asyncio

    from typer.testing import CliRunner

    from ricky.interfaces.cli.app import app

    config = settings(tmp_path)

    async def issue() -> str:
        _, _, grant_id = await _issued(config, tmp_path)
        return grant_id

    grant_id = asyncio.run(issue())

    monkeypatch.setattr("ricky.interfaces.cli.executions.load_settings", lambda: config)
    runner = CliRunner()

    listed = runner.invoke(app, ["authority", "list"])
    assert listed.exit_code == 0
    assert grant_id in listed.stdout

    shown = runner.invoke(app, ["authority", "show", grant_id])
    assert shown.exit_code == 0
    assert "Restaurant A" in shown.stdout
    assert config.authority.digest() in shown.stdout

    revoked = runner.invoke(app, ["authority", "revoke", grant_id, "--reason", "stop"])
    assert revoked.exit_code == 0

    async def status() -> str:
        store = AuthorityStore(config)
        await store.initialize()
        return (await store.get(grant_id, scope=config.resolve_profile_scope("personal"))).status

    assert asyncio.run(status()) == "revoked"

    activity = runner.invoke(app, ["authority", "activity", grant_id])
    assert activity.exit_code == 0
    assert "issued" in activity.stdout and "revoked" in activity.stdout

    missing = runner.invoke(app, ["authority", "show", "grant_" + "0" * 32])
    assert missing.exit_code == 1
