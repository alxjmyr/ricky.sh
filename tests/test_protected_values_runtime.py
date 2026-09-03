"""Scoped protected-value runtime composition and catalog tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import SecretStr

from browser_support import FakeBrowserBackend, FakeBrowserPage, FakeBrowserSession, fake_executable
from ricky.agent import AgentEvent, AgentLoop, AgentSession, PermissionRequestedEvent
from ricky.browser import BrowserService
from ricky.browser.backend import BackendTargetDescriptor
from ricky.browser.types import BrowserActionTarget
from ricky.config import RickySettings
from ricky.llm import CompletionRequest, Message, MessageDone, StreamEvent, TextPart, ToolCallPart
from ricky.permissions import PermissionResponse
from ricky.profiles import ProfileScope
from ricky.protected_values import (
    ProtectedDestinationPolicy,
    ProtectedFieldDescriptor,
    ProtectedValueBroker,
    ProtectedValueStoreError,
)
from ricky.runtime import build_capability_runtime
from ricky.tools import ToolContext

SENTINEL = "runtime-protected-sentinel-2371"


def _settings(tmp_path: Path, *, enabled: bool = True) -> RickySettings:
    return RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / "project"),
            "protected_values": {
                "enabled": enabled,
                "argon2_iterations": 1,
                "argon2_lanes": 1,
                "argon2_memory_kib": 8192,
            },
        }
    )


async def _initialize(
    settings: RickySettings,
    *,
    origin: str = "https://example.com",
) -> None:
    broker = ProtectedValueBroker(
        settings,
        scope=ProfileScope.create("personal"),
    )
    try:
        await broker.initialize("personal", SecretStr("runtime-passphrase"))
        await broker.unlock("personal", SecretStr("runtime-passphrase"))
        await broker.create(
            profile="personal",
            name="runtime-login",
            kind="credential",
            label="Runtime login",
            description="Safe descriptor available to the model.",
            fields=(
                ProtectedFieldDescriptor(
                    name="password",
                    label="Runtime password",
                    mode="stored",
                    compatible_controls=("password",),
                ),
            ),
            policy=ProtectedDestinationPolicy(mode="strict", authored_origins=(origin,)),
            values={"password": SecretStr(SENTINEL)},
        )
    finally:
        await broker.aclose()


@pytest.mark.asyncio
async def test_runtime_exposes_locked_safe_catalog_and_closes_broker(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    await _initialize(settings)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    owned: ProtectedValueBroker | None = None
    async with build_capability_runtime(
        settings,
        session=session,
        project_root=tmp_path,
    ) as runtime:
        owned = runtime.protected_values
        assert owned is not None
        assert runtime.chat_registry.get("protected_values_catalog") is not None
        result = await runtime.chat_registry.dispatch(
            "protected_values_catalog",
            {"ref": "personal/runtime-login"},
            ToolContext(cwd=tmp_path, settings=settings, session=session),
        )
        assert not result.is_error
        assert "personal/runtime-login" in result.content
        assert SENTINEL not in result.model_dump_json()
        assert runtime.full_registry.get("browser_fill_protected") is None
    assert owned is not None
    with pytest.raises(ProtectedValueStoreError, match="closed"):
        await owned.catalog()


@pytest.mark.asyncio
async def test_disabled_runtime_has_no_protected_value_surface(tmp_path: Path) -> None:
    settings = _settings(tmp_path, enabled=False)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    async with build_capability_runtime(
        settings,
        session=session,
        project_root=tmp_path,
    ) as runtime:
        assert runtime.protected_values is None
        assert runtime.chat_registry.get("protected_values_catalog") is None
        assert runtime.chat_registry.get("browser_fill_protected") is None


@pytest.mark.asyncio
async def test_browser_materializer_is_interactive_only_and_not_in_full_registry(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.browser.enabled = True
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())

    async def browser_factory(runtime_settings: RickySettings, *, scope: ProfileScope):
        return BrowserService(
            runtime_settings,
            scope=scope,
            backend=FakeBrowserBackend(),
            executable_path=fake_executable(tmp_path),
        )

    async with build_capability_runtime(
        settings,
        session=session,
        project_root=tmp_path,
        browser_factory=browser_factory,
    ) as runtime:
        tool = runtime.chat_registry.get("browser_fill_protected")
        assert tool is not None
        assert cast(Any, tool).unattended == "forbidden"
        assert runtime.full_registry.get("browser_fill_protected") is None


@pytest.mark.asyncio
async def test_agent_loop_never_projects_materialized_value_to_provider_or_session(
    tmp_path: Path,
) -> None:
    origin = "https://127.0.0.1:9443"
    settings = _settings(tmp_path)
    settings.browser.enabled = True
    settings.browser.allowed_private_origins = [origin]
    await _initialize(settings, origin=origin)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    page = FakeBrowserPage(
        url=f"{origin}/login",
        snapshot='- textbox "Account password" [ref=e1]',
        targets=(
            BackendTargetDescriptor(
                ref="e1",
                role="textbox",
                name="Account password",
                control_kind="text",
                frame_origin=origin,
                editable=True,
                protected=True,
                protected_kind="password",
            ),
        ),
    )
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(FakeBrowserSession([page]))
    service: BrowserService | None = None

    async def browser_factory(runtime_settings: RickySettings, *, scope: ProfileScope):
        nonlocal service
        service = BrowserService(
            runtime_settings,
            scope=scope,
            backend=backend,
            executable_path=fake_executable(tmp_path),
        )
        return service

    async def unlock(_request):
        return SecretStr("runtime-passphrase")

    class CapturingProvider:
        name = "capturing-provider"

        def __init__(self) -> None:
            self.requests: list[CompletionRequest] = []
            self.responses: list[MessageDone] = []

        async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
            self.requests.append(request)
            yield self.responses.pop(0)

        async def aclose(self) -> None:
            return None

    provider = CapturingProvider()
    async with build_capability_runtime(
        settings,
        session=session,
        project_root=tmp_path,
        browser_factory=browser_factory,
        unlock_responder=unlock,
    ) as runtime:
        assert service is not None
        opened = await service.open_session(headless=True)
        snapshot = await service.snapshot(opened.session_id, page_id=None)
        target = BrowserActionTarget.model_validate(
            snapshot.targets[0].model_dump(exclude={"navigation_generation"}), strict=True
        )
        provider.responses.extend(
            (
                MessageDone(
                    message=Message(
                        role="assistant",
                        content=[
                            ToolCallPart(
                                id="call_protected_fill",
                                name="browser_fill_protected",
                                args={
                                    "target": target.model_dump(mode="json"),
                                    "protected_value": "personal/runtime-login",
                                    "field": "password",
                                },
                            )
                        ],
                    ),
                    stop_reason="tool_calls",
                ),
                MessageDone(
                    message=Message(
                        role="assistant",
                        content=[TextPart(text="Protected field filled; submit remains separate.")],
                    ),
                    stop_reason="stop",
                ),
            )
        )

        async def allow(_event: PermissionRequestedEvent) -> PermissionResponse:
            return PermissionResponse(decision="allow")

        loop = AgentLoop(
            provider=provider,
            registry=runtime.chat_registry,
            settings=settings,
            permission_engine=runtime.permission_engine,
            permission_responder=allow,
            cwd=tmp_path,
        )
        events: list[AgentEvent] = [
            event async for event in loop.run_turn(session, "Fill the saved password.")
        ]

        assert len(page.protected_fills) == 1
        assert page.protected_fills[0].value.get_secret_value() == SENTINEL
        projected = "\n".join(
            [
                *(request.model_dump_json() for request in provider.requests),
                *(event.model_dump_json() for event in events),
                session.model_dump_json(),
            ]
        )
        assert SENTINEL not in projected
