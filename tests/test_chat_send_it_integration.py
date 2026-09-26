"""Send-it preserves real browser and protected-value enforcement boundaries."""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from browser_support import FakeBrowserBackend, fake_executable
from ricky.agent import AgentSession
from ricky.agent.events import PermissionDecidedEvent
from ricky.agent.tool_dispatch import decide_tool_permission, deny_permission
from ricky.agent.workflow import deny_approval_v2
from ricky.browser.backend import BackendTargetDescriptor
from ricky.browser.service import BrowserService
from ricky.browser.tools import BrowserCommitTool
from ricky.config import RickySettings
from ricky.interfaces.cli.chat_permissions import ChatPermissions
from ricky.llm import ToolCallPart
from ricky.permissions import PermissionEngine, Policy, PolicyRule
from ricky.profiles import ProfileScope
from ricky.protected_values import (
    ProtectedDestinationPolicy,
    ProtectedFieldDescriptor,
    ProtectedUseRequest,
    ProtectedValueBroker,
    ProtectedValueStoreError,
    deny_destination,
)
from ricky.tools import Tool, ToolContext, ToolRegistry

_ORIGIN = "http://127.0.0.1:8765"


def _permissions(*, enabled: bool = False) -> ChatPermissions:
    return ChatPermissions(
        permission_responder=deny_permission,
        approval_responder=deny_approval_v2,
        destination_responder=deny_destination,
        enabled=enabled,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_destination", [False, True])
@pytest.mark.parametrize("financial", [False, True])
async def test_send_it_browser_commit_retains_preparation_and_live_revalidation(
    tmp_path: Path, changed_destination: bool, financial: bool
) -> None:
    settings = RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / "project"),
            "browser": {"enabled": True, "allowed_private_origins": [_ORIGIN]},
        }
    )
    backend = FakeBrowserBackend()
    service = BrowserService(
        settings,
        scope=settings.resolve_profile_scope(),
        backend=backend,
        executable_path=fake_executable(tmp_path),
    )
    try:
        browser = await service.open_session()
        page = backend.sessions[0].page_handles[0]
        page.url = f"{_ORIGIN}/form"
        page.snapshot_text = '- button "Submit application" [ref=e1]'
        page.targets = (
            BackendTargetDescriptor(
                ref="e1",
                role="button",
                name="Submit application",
                control_kind="button",
                frame_origin=_ORIGIN,
                consequential=True,
            ),
        )
        page.effective_destinations = (f"{_ORIGIN}/applications",)
        snapshot = await service.snapshot(browser.session_id, page_id=None)
        session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
        ctx = ToolContext(cwd=tmp_path, settings=settings, session=session)
        tool = BrowserCommitTool(service)
        registry = ToolRegistry([cast(Tool, tool)])
        call = ToolCallPart(
            id="call_submit",
            name=tool.name,
            args={
                "target": {
                    "session_id": browser.session_id,
                    "page_id": snapshot.page.page_id,
                    "snapshot_id": snapshot.snapshot_id,
                    "ref": "e1",
                },
                "envelope": {
                    "kind": "browser",
                    "intent": "Submit application",
                    "destination": f"{_ORIGIN}/applications",
                    "consequences": ["Application is submitted"],
                    "disclosures": [],
                    "expected_result": "Submission confirmation",
                },
            },
        )
        if financial:
            page.financial_signal = True
            call.args["envelope"] = {
                "kind": "financial",
                "intent": "Purchase application credits",
                "payee": "Example merchant",
                "total": {"currency": "USD", "amount": "10.00"},
                "fees": [],
                "timing": "one_time",
                "source": {"kind": "site", "label": "Saved account"},
                "consequences": ["Account is charged ten dollars"],
                "expected_result": "Payment confirmation",
            }
        permissions = _permissions()

        async def gate(engine: PermissionEngine | None = None):
            return await decide_tool_permission(
                session=session,
                registry=registry,
                engine=engine or PermissionEngine(),
                responder=permissions.request_permission,
                turn_id="turn_submit",
                call=call,
                ctx=ctx,
            )

        assert (await gate()).decision == "deny"
        assert not page.actions
        permissions.enabled = True
        preflights = len(page.preflights)
        denied = await gate(
            PermissionEngine(Policy(rules=[PolicyRule(tool_name=tool.name, decision="deny")]))
        )
        assert denied.decision == "deny"
        assert len(page.preflights) == preflights
        outcome = await gate()
        assert outcome.decision == "allow"
        assert outcome.prepared_effect is not None
        assert len(page.preflights) == preflights + 1
        assert outcome.normalized_args is not None
        decision = next(e for e in outcome.events if isinstance(e, PermissionDecidedEvent))
        assert "send-it" in decision.reason
        assert not decision.remembered
        assert not session.permission_grants
        if changed_destination:
            page.effective_destinations = (f"{_ORIGIN}/different",)
        result = await registry.dispatch_prepared(
            tool.name, outcome.normalized_args, outcome.prepared_effect, ctx
        )
        assert result.effect_receipt is not None
        assert result.effect_receipt.disposition == (
            "not_performed" if changed_destination else "performed"
        )
        assert len(page.actions) == (0 if changed_destination else 1)
    finally:
        await service.aclose()
    assert backend.closed


@pytest.mark.asyncio
async def test_send_it_protected_destination_is_once_and_secure_input_still_required(
    tmp_path: Path,
) -> None:
    settings = RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / "project"),
            "protected_values": {
                "enabled": True,
                "argon2_iterations": 1,
                "argon2_lanes": 1,
                "argon2_memory_kib": 8192,
            },
        }
    )
    permissions = _permissions(enabled=True)
    secure_input = AsyncMock(return_value=SecretStr("protected-test-input"))
    broker = ProtectedValueBroker(
        settings,
        scope=ProfileScope.create("personal"),
        consumer_ids=frozenset({"browser.fill"}),
        destination_responder=permissions.request_protected_destination,
        secure_value_responder=secure_input,
    )
    try:
        await broker.initialize("personal", SecretStr("test-passphrase"))
        await broker.unlock("personal", SecretStr("test-passphrase"))
        descriptor = await broker.create(
            profile="personal",
            name="login",
            kind="credential",
            label="Login",
            description="Test account",
            fields=(
                ProtectedFieldDescriptor(
                    name="password",
                    label="Password",
                    mode="prompt_each_use",
                    compatible_controls=("password",),
                ),
            ),
            policy=ProtectedDestinationPolicy(mode="confirm_new"),
            values={},
        )
        request = ProtectedUseRequest(
            ref=descriptor.ref,
            field="password",
            consumer_id="browser.fill",
            control_kind="password",
            top_level_origin="https://example.com",
            frame_origin="https://example.com",
            occurrence="test-session/page/snapshot/field",
        )
        material = await broker.prepare(request)
        await broker.revalidate(material)
        secure_input.assert_awaited_once()
        assert await broker.approvals(descriptor.ref) == []
        assert "protected-test-input" not in material.use.model_dump_json()
        permissions.enabled = False
        with pytest.raises(ProtectedValueStoreError, match="approval was denied"):
            await broker.prepare(request)
        secure_input.assert_awaited_once()
        permissions.enabled = True
        await broker.revise(descriptor, policy=ProtectedDestinationPolicy(mode="strict"))
        with pytest.raises(ProtectedValueStoreError, match="destination policy denied"):
            await broker.prepare(request)
        secure_input.assert_awaited_once()
        assert not (tmp_path / "project").exists()
    finally:
        await broker.aclose()


@pytest.mark.asyncio
async def test_chat_composes_live_toggle_into_loop_and_workflows_without_next_chat_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib
    from io import StringIO

    from rich.console import Console

    from ricky.agent.events import PermissionRequestedEvent
    from ricky.agent.workflow import ApprovalRequest
    from ricky.interfaces.cli.chat import ChatController
    from ricky.interfaces.cli.render import CliRenderer

    app_module = importlib.import_module("ricky.interfaces.cli.app")
    settings = RickySettings(
        user_data_dir=str(tmp_path / "user"),
        project_data_dir=str(tmp_path / "project"),
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(app_module, "load_settings", lambda: settings)
    providers = []

    class Provider:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    def create_provider(*args, **kwargs):
        provider = Provider()
        providers.append(provider)
        return provider

    monkeypatch.setattr(app_module, "create_provider", create_provider)
    renderer = CliRenderer(console=Console(file=StringIO()))
    monkeypatch.setattr(renderer, "request_permission", deny_permission)
    monkeypatch.setattr(renderer, "request_workflow_approval", deny_approval_v2)
    observed = []

    async def run(controller: ChatController) -> None:
        permissions = controller.permissions
        assert permissions is not None
        observed.append(permissions.enabled)
        session_before = controller.session.model_dump_json()
        service = controller.workflow_runner
        assert service is not None
        assert service.permission_responder is not None
        assert service.approval_responder is not None
        request = PermissionRequestedEvent(
            turn_id="turn_test",
            call_id="call_test",
            tool_name="write_file",
            args={},
            reason="mutating tool",
        )
        approval = ApprovalRequest(
            run_id="run_test", step_id="review", mode="confirm", prompt="Approve?"
        )
        for enabled in [permissions.enabled, not permissions.enabled, permissions.enabled]:
            await controller._handle_slash_command(f"/send-it {'on' if enabled else 'off'}")
            loop_response = await controller.agent_loop._permission_responder(request)
            workflow_response = await service.permission_responder(request)
            confirmation = await service.approval_responder(approval)
            expected = "allow" if enabled else "deny"
            assert loop_response.decision == expected
            assert workflow_response.decision == expected
            assert confirmation.approved == enabled
        assert controller.session.model_dump_json() == session_before

    monkeypatch.setattr(ChatController, "run", run)
    await app_module._chat(None, None, renderer, send_it=True)
    await app_module._chat(None, None, renderer)
    assert observed == [True, False]
    assert len(providers) == 2
    assert all(provider.closed for provider in providers)
