"""Memory REPL command tests."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from ricky.agent import AgentSession
from ricky.config import RickySettings
from ricky.interfaces.cli.chat import MEMORY_REFLECTION_PROMPT, ChatController
from ricky.interfaces.cli.render import CliRenderer
from ricky.memory import MemoryStore
from ricky.skills.registry import SkillRegistry


def _controller(tmp_path: Path, *, enabled: bool = True) -> ChatController:
    settings = RickySettings(user_data_dir=str(tmp_path / "global"))
    profile_scope = settings.resolve_profile_scope("personal")
    session = AgentSession.create(settings, profile_scope=profile_scope)
    renderer = CliRenderer(
        console=Console(
            file=StringIO(),
            force_terminal=False,
            color_system=None,
            width=100,
        )
    )
    return ChatController(
        agent_loop=None,  # type: ignore[arg-type] - command tests replace _run_turn
        session=session,
        settings=settings,
        renderer=renderer,
        skill_registry=SkillRegistry([]),
        memory=MemoryStore.create(settings, scope=profile_scope) if enabled else None,
    )


@pytest.mark.asyncio
async def test_remember_argument_starts_inline_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path)
    prompts: list[str] = []

    async def capture(prompt: str) -> None:
        prompts.append(prompt)

    monkeypatch.setattr(controller, "_run_turn", capture)

    handled = await controller._handle_slash_command("/remember I prefer terse answers")

    assert handled is True
    assert prompts == ["Remember this: I prefer terse answers"]


@pytest.mark.asyncio
async def test_bare_remember_starts_proposal_only_reflection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path)
    prompts: list[str] = []

    async def capture(prompt: str) -> None:
        prompts.append(prompt)

    monkeypatch.setattr(controller, "_run_turn", capture)

    await controller._handle_slash_command("/remember")

    assert prompts == [MEMORY_REFLECTION_PROMPT]
    assert "Do not call remember in this turn" in prompts[0]
    assert "Wait for" in prompts[0]


@pytest.mark.asyncio
async def test_remember_reports_disabled_memory(tmp_path: Path) -> None:
    controller = _controller(tmp_path, enabled=False)

    handled = await controller._handle_slash_command("/remember something")

    assert handled is True
