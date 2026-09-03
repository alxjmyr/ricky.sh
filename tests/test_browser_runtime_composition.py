"""Interactive-only browser runtime composition contracts."""

from __future__ import annotations

from typing import cast

import pytest

from ricky.agent import AgentSession
from ricky.browser import BrowserService
from ricky.config import RickySettings
from ricky.profiles import ProfileScope
from ricky.project_scope import ProjectScope
from ricky.runtime import build_capability_runtime
from ricky.tools import ToolRegistry

_BROWSER_TOOLS = {
    "browser_resources",
    "browser_session_open",
    "browser_session_open_resource",
    "browser_session_close",
    "browser_pages",
    "browser_page_select",
    "browser_navigate",
    "browser_scroll",
    "browser_snapshot",
    "browser_visual_snapshot",
    "browser_click",
    "browser_fill",
    "browser_select",
    "browser_set_checked",
    "browser_press_key",
    "browser_commit",
    "browser_upload",
    "browser_download",
    "browser_coordinate_click",
    "browser_coordinate_commit",
    "browser_handoff",
}

_BROWSER_CAPABILITIES = {
    "builtin.browser.read",
    "builtin.browser.interact",
    "builtin.browser.commit",
    "builtin.browser.handoff",
}


class _FakeBrowserService:
    def __init__(self) -> None:
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


def _settings(tmp_path, *, enabled: bool) -> RickySettings:
    return RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user-data"),
            "project_data_dir": str(tmp_path / "project-data"),
            "browser": {"enabled": enabled},
            "memory": {"enabled": False},
            "workflow": {"enabled": False},
        }
    )


@pytest.mark.asyncio
async def test_browser_factory_is_explicit_interactive_only_and_runtime_owned(tmp_path) -> None:
    settings = _settings(tmp_path, enabled=True)
    scope = settings.resolve_profile_scope()
    session = AgentSession.create(settings, profile_scope=scope)
    service = _FakeBrowserService()
    factory_calls: list[tuple[RickySettings, ProfileScope]] = []

    async def factory(
        runtime_settings: RickySettings,
        *,
        scope: ProfileScope,
    ) -> BrowserService:
        factory_calls.append((runtime_settings, scope))
        return cast(BrowserService, service)

    async with build_capability_runtime(
        settings,
        session=session,
        project_scope=ProjectScope.disabled(),
        browser_factory=factory,
    ) as runtime:
        assert len(factory_calls) == 1
        assert factory_calls[0][0].browser.enabled
        assert factory_calls[0][1] == scope
        assert {tool.name for tool in runtime.tools} >= _BROWSER_TOOLS
        assert {tool.name for tool in runtime.chat_registry.tools()} >= _BROWSER_TOOLS
        assert _BROWSER_TOOLS.isdisjoint(tool.name for tool in runtime.full_registry.tools())
        resources: set[str] = set()
        for capability_id in _BROWSER_CAPABILITIES:
            capability = runtime.capability_registry.require(capability_id)
            resources.update(resource.id for resource in capability.resources)
            assert capability.unattended_eligible == (capability_id != "builtin.browser.handoff")
        assert resources == _BROWSER_TOOLS
        assert service.close_calls == 0

    assert service.close_calls == 1


@pytest.mark.asyncio
async def test_disabled_browser_does_not_call_explicit_factory(tmp_path) -> None:
    settings = _settings(tmp_path, enabled=False)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    called = False

    async def factory(
        runtime_settings: RickySettings,
        *,
        scope: ProfileScope,
    ) -> BrowserService:
        del runtime_settings, scope
        nonlocal called
        called = True
        raise AssertionError("disabled browser factory must not be called")

    async with build_capability_runtime(
        settings,
        session=session,
        project_scope=ProjectScope.disabled(),
        browser_factory=factory,
    ) as runtime:
        assert not called
        assert _BROWSER_TOOLS.isdisjoint(tool.name for tool in runtime.tools)
        assert all(
            runtime.capability_registry.get(capability_id) is None
            for capability_id in _BROWSER_CAPABILITIES
        )


@pytest.mark.asyncio
async def test_browser_closes_when_later_interactive_registry_construction_fails(
    tmp_path,
) -> None:
    settings = _settings(tmp_path, enabled=True)
    scope = settings.resolve_profile_scope()
    session = AgentSession.create(settings, profile_scope=scope)
    service = _FakeBrowserService()

    async def factory(
        runtime_settings: RickySettings,
        *,
        scope: ProfileScope,
    ) -> BrowserService:
        del runtime_settings, scope
        return cast(BrowserService, service)

    def registry_factory(tools) -> ToolRegistry:
        if _BROWSER_TOOLS & {tool.name for tool in tools}:
            raise RuntimeError("interactive registry failed")
        return ToolRegistry(tools)

    with pytest.raises(RuntimeError, match="interactive registry failed"):
        async with build_capability_runtime(
            settings,
            session=session,
            project_scope=ProjectScope.disabled(),
            browser_factory=factory,
            registry_factory=registry_factory,
        ):
            raise AssertionError("runtime must not yield after registry construction fails")

    assert service.close_calls == 1
