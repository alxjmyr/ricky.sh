"""Browser handle mistakes are repairable, scoped, and bounded before dispatch."""

import json
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest

from browser_support import FakeBrowserBackend, FakeBrowserPage, FakeBrowserSession, fake_executable
from ricky.agent import AgentLoop, AgentSession
from ricky.agent.events import AgentErrorEvent, TurnFinishedEvent
from ricky.browser.recovery import BrowserReferenceError, SessionReferences
from ricky.browser.service import BrowserService
from ricky.browser.tools import (
    BrowserNavigateTool,
    BrowserSessionOpenResourceTool,
    BrowserSessionOpenTool,
    BrowserSnapshotTool,
)
from ricky.browser.types import BrowserError, BrowserFailure
from ricky.config import RickySettings
from ricky.llm import StreamEvent
from ricky.tools import Tool, ToolContext, ToolRegistry
from test_agent_loop import FakeProvider, _final_message, _tool_message

BAD_SESSION = "browser_session_" + "f" * 32
BAD_PAGE = "browser_page_" + "f" * 32
URL = "http://127.0.0.1:8765/fixture"


def _runtime(tmp_path: Path, *, max_sessions: int = 1):
    tmp_path.mkdir(parents=True, exist_ok=True)
    settings = RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / "project"),
            "browser": {
                "enabled": True,
                "max_sessions": max_sessions,
                "allowed_private_origins": ["http://127.0.0.1:8765"],
            },
            "profile_configs": {
                "personal": {
                    "browser": {
                        "resources": {
                            "personal-browser": {
                                "kind": "persistent",
                                "headless": True,
                                "description": "Isolated browser fixture",
                            }
                        }
                    }
                }
            },
        }
    )
    page = FakeBrowserPage(title="PRIVATE TITLE", url="about:blank")
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(FakeBrowserSession([page]))
    service = BrowserService(
        settings,
        scope=settings.resolve_profile_scope(),
        backend=backend,
        executable_path=fake_executable(tmp_path),
    )
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    return service, page, settings, ToolContext(settings=settings, session=session, cwd=tmp_path)


async def test_reference_feedback_is_scoped_and_corrected_call_uses_existing_session(
    tmp_path: Path,
):
    service, page, settings, ctx = _runtime(tmp_path)
    other, _, _, _ = _runtime(tmp_path / "other")
    try:
        opened = await service.open_resource("personal/personal-browser")
        hidden = await other.open_session()
        tool = BrowserNavigateTool(service)
        args = {"session_id": BAD_SESSION, "url": URL}
        rejected = await tool.run(tool.Params.model_validate(args), ctx)
        assert rejected.is_error and rejected.runtime_failure is not None
        assert opened.session_id in rejected.content
        assert opened.selected_page_id in rejected.content
        assert hidden.session_id not in rejected.content
        assert "PRIVATE TITLE" not in rejected.content
        assert str(tmp_path) not in rejected.content
        assert not page.navigations
        repeated = await tool.run(tool.Params.model_validate(args), ctx)
        assert repeated.runtime_failure == rejected.runtime_failure
        # Copy from actual recovery feedback, not a separately scripted correct ID.
        row = json.loads(
            rejected.content.split("Current runtime browser references:\n")[1].splitlines()[0]
        )
        result = await tool.run(
            tool.Params.model_validate(
                {
                    "session_id": row["session_id"],
                    "page_id": row["selected_page_id"],
                    "url": URL,
                }
            ),
            ctx,
        )
        assert not result.is_error
        assert page.navigations == [URL]
        assert not (tmp_path / "project").exists()
        wrong_page = await tool.run(
            tool.Params.model_validate(
                {
                    "session_id": opened.session_id,
                    "page_id": BAD_PAGE,
                    "url": URL,
                }
            ),
            ctx,
        )
        assert wrong_page.runtime_failure is not None
        assert opened.selected_page_id in wrong_page.content
        assert page.navigations == [URL]
    finally:
        await service.aclose()
        await other.aclose()


@pytest.mark.parametrize("configured", [False, True])
async def test_session_limit_feedback_reuses_handles_and_changes_after_close(
    tmp_path: Path, configured: bool
):
    service, page, _, ctx = _runtime(tmp_path)
    try:
        opened = await service.open_resource("personal/personal-browser")
        tool = (
            BrowserSessionOpenResourceTool(service)
            if configured
            else BrowserSessionOpenTool(service)
        )
        params = tool.Params.model_validate(
            {"resource": "personal/personal-browser"} if configured else {}
        )
        failure = await cast(Tool, tool).run(params, ctx)
        assert failure.is_error and failure.runtime_failure is not None
        assert "Reuse an existing session" in failure.content
        assert opened.session_id in failure.content
        assert not page.closed
        navigate = BrowserNavigateTool(service)
        wrong_args = navigate.Params.model_validate({"session_id": BAD_SESSION, "url": URL})
        before_close = await navigate.run(wrong_args, ctx)
        await service.close_session(opened.session_id)
        after_close = await navigate.run(wrong_args, ctx)
        assert before_close.runtime_failure is not None and after_close.runtime_failure is not None
        assert (
            before_close.runtime_failure.state_fingerprint
            != after_close.runtime_failure.state_fingerprint
        )
        stale = await navigate.run(
            navigate.Params.model_validate({"session_id": opened.session_id, "url": URL}), ctx
        )
        assert stale.runtime_failure is not None
        assert opened.session_id not in stale.content
        assert stale.runtime_failure.state_fingerprint != failure.runtime_failure.state_fingerprint
    finally:
        await service.aclose()


@pytest.mark.parametrize("repair", [True, False])
@pytest.mark.parametrize("failure_kind", ["unknown_session", "session_limit"])
async def test_real_browser_errors_repair_or_stop_agent_loop(
    tmp_path: Path, repair: bool, failure_kind: str
):
    service, page, settings, ctx = _runtime(tmp_path)
    try:
        opened = await service.open_session()
        navigate = BrowserNavigateTool(service)
        opening = BrowserSessionOpenTool(service)
        failed_name = navigate.name if failure_kind == "unknown_session" else opening.name
        failed_args: dict[str, object] = (
            {"session_id": BAD_SESSION, "url": URL} if failure_kind == "unknown_session" else {}
        )
        scripts: list[list[StreamEvent | BaseException]] = [
            [_tool_message(str(i), failed_name, failed_args)] for i in range(2 if repair else 3)
        ]
        if repair:
            scripts.append(
                [
                    _tool_message(
                        "fixed", navigate.name, {"session_id": opened.session_id, "url": URL}
                    )
                ]
            )
            scripts.append([_final_message("recovered")])
        provider = FakeProvider(scripts)
        loop = AgentLoop(
            provider=provider,
            registry=ToolRegistry([cast(Tool, navigate), cast(Tool, opening)]),
            settings=settings,
            cwd=tmp_path,
        )
        events = [event async for event in loop.run_turn(ctx.session, "Use the browser")]
        assert (
            "Another identical failure will stop this turn"
            in provider.requests[2].model_dump_json()
        )
        if repair:
            assert isinstance(events[-1], TurnFinishedEvent)
            assert events[-1].error is None
            assert page.navigations == [URL]
        else:
            assert isinstance(events[-2], AgentErrorEvent)
            assert events[-2].error_type == "ToolRuntimeRepairLimit"
            assert len(provider.requests) == 3
            assert not page.navigations
        assert opened.session_id in provider.requests[1].model_dump_json()
    finally:
        await service.aclose()


def test_recovery_is_bounded_and_fingerprint_covers_omitted_references():
    refs = tuple(
        SessionReferences(
            session_id=f"browser_session_{i:032x}",
            resource="personal/browser",
            selected_page_id=f"browser_page_{i:032x}",
            page_ids=tuple(f"browser_page_{j:032x}" for j in range(20)),
        )
        for i in range(100)
    )
    first = BrowserReferenceError("unknown_session", refs, session_limit=100)
    changed = BrowserReferenceError("unknown_session", refs[:-1], session_limit=100)
    assert len(first.failure.message) <= 2000
    assert "omitted" in first.failure.message
    assert first.runtime_failure.state_fingerprint != changed.runtime_failure.state_fingerprint


async def test_transient_browser_failure_is_not_classified_as_reference_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, _, _, ctx = _runtime(tmp_path)
    try:
        opened = await service.open_session()
        monkeypatch.setattr(
            service,
            "snapshot",
            AsyncMock(
                side_effect=BrowserError(
                    BrowserFailure(
                        code="operation_timeout", message="snapshot timed out", retryable=True
                    )
                )
            ),
        )
        tool = BrowserSnapshotTool(service)
        result = await tool.run(tool.Params.model_validate({"session_id": opened.session_id}), ctx)
        assert result.is_error
        assert result.runtime_failure is None
        assert result.content == "snapshot timed out"
    finally:
        await service.aclose()
