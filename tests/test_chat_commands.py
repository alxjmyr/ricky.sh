"""Tests for chat REPL slash commands."""

from __future__ import annotations

import asyncio
from io import BytesIO, StringIO
from pathlib import Path

import pytest
from PIL import Image
from rich.console import Console

from ricky.agent import AgentSession, PermissionGrant
from ricky.agent.artifacts import SessionArtifactStore
from ricky.config import RickySettings
from ricky.interfaces.cli.chat import ChatController
from ricky.interfaces.cli.render import CliRenderer
from ricky.media import SessionMediaError, SessionMediaStore
from ricky.profiles import ProfileLabel


def _controller(
    session: AgentSession,
    *,
    settings: RickySettings | None = None,
    session_artifacts: SessionArtifactStore | None = None,
    session_media: SessionMediaStore | None = None,
) -> tuple[ChatController, StringIO]:
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=100)
    renderer = CliRenderer(console=console)
    controller = ChatController(
        agent_loop=None,  # type: ignore[arg-type]  # unused by /permissions
        session=session,
        settings=settings or RickySettings(),
        renderer=renderer,
        skill_registry=None,  # type: ignore[arg-type]  # unused by /permissions
        session_artifacts=session_artifacts,
        session_media=session_media,
    )
    return controller, output


async def test_permissions_command_lists_active_grants() -> None:
    settings = RickySettings()
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
    )
    session.permission_grants.append(
        PermissionGrant(
            tool_name="gmail_trash",
            params_equal={"account": "personal"},
            label="gmail_trash on personal",
        )
    )
    controller, output = _controller(session)

    handled = await controller._handle_slash_command("/permissions")

    assert handled is True
    assert "gmail_trash on personal" in output.getvalue()


async def test_permissions_command_reports_empty_state() -> None:
    settings = RickySettings()
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
    )
    controller, output = _controller(session)

    await controller._handle_slash_command("/permissions")

    assert "No active permission grants." in output.getvalue()


async def test_permissions_clear_revokes_all_grants() -> None:
    settings = RickySettings()
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
    )
    session.permission_grants.append(
        PermissionGrant(tool_name="gmail_trash", params_equal={"account": "personal"})
    )
    controller, output = _controller(session)

    handled = await controller._handle_slash_command("/permissions clear")

    assert handled is True
    assert session.permission_grants == []
    assert "Cleared 1" in output.getvalue()


async def test_clear_preserves_the_exact_profile_scope() -> None:
    settings = RickySettings()
    scope = settings.resolve_profile_scope("personal", access_profiles=["work"])
    session = AgentSession.create(settings, profile_scope=scope)
    original_id = session.id
    controller, output = _controller(session)

    handled = await controller._handle_slash_command("/clear")

    assert handled is True
    assert controller.session.id != original_id
    assert controller.session.profile_scope == scope
    assert "Started a fresh session." in output.getvalue()


async def test_clear_rebinds_artifacts_to_the_fresh_session(tmp_path) -> None:
    settings = RickySettings(
        user_data_dir=str(tmp_path / "user"),
        project_data_dir=str(tmp_path / "project"),
    )
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    artifacts = SessionArtifactStore.create(settings, session.id)
    await artifacts.offload(
        session,
        call_id="call_before_clear",
        tool_name="browser_snapshot",
        content="before clear",
        excerpt_chars=4,
    )
    old_root = artifacts.root
    controller, _output = _controller(
        session,
        settings=settings,
        session_artifacts=artifacts,
    )

    handled = await controller._handle_slash_command("/clear")

    assert handled is True
    assert not old_root.exists()
    assert artifacts.root.parent.name == controller.session.id
    record = await artifacts.offload(
        controller.session,
        call_id="call_after_clear",
        tool_name="browser_snapshot",
        content="after clear",
        excerpt_chars=4,
    )
    assert record.id.startswith("artifact_")
    assert artifacts.root.is_dir()


async def test_clear_invalidates_media_and_preserves_bound_resolver_for_fresh_session(
    tmp_path: Path,
) -> None:
    settings = RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / "project"),
            "profile_configs": {
                "personal": {"browser": {"screenshot_allowed_providers": ["openrouter"]}}
            },
        }
    )
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
    )
    media = SessionMediaStore.create(settings, session.id)
    output = BytesIO()
    Image.new("RGB", (2, 1), (2, 4, 8)).save(output, format="PNG")
    png = output.getvalue()
    old = await media.admit_png(
        session,
        content=png,
        source_label=ProfileLabel.owned_by("personal"),
        source_owner="personal",
        provenance="browser_screenshot",
        disclosure_class="browser_screenshot",
        admitted_provider="openrouter",
    )
    old_root = media.root
    bound_resolver = media.resolver(
        session,
        provider="openrouter",
        profile_scope=session.profile_scope,
    )
    controller, _output = _controller(
        session,
        settings=settings,
        session_media=media,
    )

    handled = await controller._handle_slash_command("/clear")

    assert handled is True
    assert controller.session is session
    assert not old_root.exists()
    assert session.media == []
    with pytest.raises(SessionMediaError, match="unknown media artifact"):
        await bound_resolver.resolve(old.reference())

    fresh = await media.admit_png(
        session,
        content=png,
        source_label=ProfileLabel.owned_by("personal"),
        source_owner="personal",
        provenance="browser_screenshot",
        disclosure_class="browser_screenshot",
        admitted_provider="openrouter",
    )
    assert (await bound_resolver.resolve(fresh.reference())).content == png


async def test_cancelled_clear_joins_media_reset_and_finishes_session_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / "project"),
        }
    )
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
    )
    original_id = session.id
    media = SessionMediaStore.create(settings, session.id)
    output = BytesIO()
    Image.new("RGB", (2, 1), (2, 4, 8)).save(output, format="PNG")
    await media.admit_png(
        session,
        content=output.getvalue(),
        source_label=ProfileLabel.owned_by("personal"),
        source_owner="personal",
        provenance="synthetic_fixture",
        disclosure_class="explicit_provider",
        admitted_provider="openrouter",
    )
    started = asyncio.Event()
    release = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_delete = media._remove_all_sync

    def delayed_delete() -> None:
        loop.call_soon_threadsafe(started.set)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result()
        original_delete()

    monkeypatch.setattr(media, "_remove_all_sync", delayed_delete)
    controller, rendered = _controller(
        session,
        settings=settings,
        session_media=media,
    )
    task = asyncio.create_task(controller._handle_slash_command("/clear"))
    await started.wait()

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert controller.session is session
    assert controller.session.id != original_id
    assert controller.session.media == []
    assert media.root.parent.name == controller.session.id
    assert "Started a fresh session." in rendered.getvalue()
