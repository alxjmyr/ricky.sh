"""Provider-free CLI proof for persistent session inspection and archive."""

from __future__ import annotations

import asyncio
from pathlib import Path

from typer.testing import CliRunner

from ricky.agent import AgentSession
from ricky.config import RickySettings
from ricky.interfaces.cli.app import app
from ricky.sessions import SessionStore

runner = CliRunner()


def test_list_show_and_archive_do_not_construct_provider(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    settings = RickySettings(user_data_dir=str(tmp_path / "user-data"))
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())

    async def seed() -> None:
        store = SessionStore(settings)
        await store.initialize()
        await store.create(session, scope=session.profile_scope)

    asyncio.run(seed())

    def forbidden_provider(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("provider construction is forbidden for inspection commands")

    monkeypatch.setattr(  # type: ignore[attr-defined]
        "ricky.runtime.composition.create_provider",
        forbidden_provider,
    )

    listed = runner.invoke(app, ["session", "list"])
    assert listed.exit_code == 0
    assert session.id in listed.stdout
    assert "active" in listed.stdout

    shown = runner.invoke(app, ["session", "show", session.id])
    assert shown.exit_code == 0
    assert "history messages: 0" in shown.stdout
    assert "recent turns:" in shown.stdout

    archived = runner.invoke(app, ["session", "archive", session.id])
    assert archived.exit_code == 0
    assert "Archived" in archived.stdout

    archived_list = runner.invoke(app, ["session", "list", "--status", "archived"])
    assert archived_list.exit_code == 0
    assert session.id in archived_list.stdout


def test_session_help_exposes_plan_commands() -> None:
    result = runner.invoke(app, ["session", "--help"])
    assert result.exit_code == 0
    for command in ("list", "show", "resume", "archive"):
        assert command in result.stdout
