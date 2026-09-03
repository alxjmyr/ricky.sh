"""Loop-level Calendar permission and denial tests."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
from typing import Any

import httpx
import pytest

from ricky.agent import AgentLoop, AgentSession
from ricky.agent.events import PermissionRequestedEvent
from ricky.config import GcalSettings, GoogleAccountSettings, RickySettings
from ricky.llm import Message, MessageDone, TextPart, ToolCallPart, Usage
from ricky.permissions import PermissionResponse
from ricky.tools import ToolRegistry
from ricky.tools.integrations.gcal.client import GcalClient
from ricky.tools.integrations.gcal.tools import (
    GcalCreateEventTool,
    GcalDeleteEventTool,
)


class FakeAuth:
    def validate_account(self, account: str) -> GoogleAccountSettings:
        if account != "work":
            raise ValueError(f"unknown account {account}")
        return GoogleAccountSettings(email="alex@company.example")

    async def get_access_token(
        self,
        account: str,
        *,
        force_refresh: bool = False,
        required_scopes: Collection[str] | None = None,
    ) -> str:
        del account, force_refresh
        return "access-token"


class FakeProvider:
    name = "fake"

    def __init__(self, scripts: list[list[Any]]) -> None:
        self.scripts = scripts

    async def stream(self, request: Any):  # noqa: ANN401
        del request
        for event in self.scripts.pop(0):
            yield event

    async def aclose(self) -> None:
        pass


def _tool_call(call_id: str, name: str, args: dict[str, object]) -> MessageDone:
    return MessageDone(
        message=Message(
            role="assistant",
            content=[ToolCallPart(id=call_id, name=name, args=args)],
        ),
        usage=Usage(prompt_tokens=1, completion_tokens=1),
        stop_reason="tool_calls",
    )


def _final(text: str) -> MessageDone:
    return MessageDone(
        message=Message(role="assistant", content=[TextPart(text=text)]),
        usage=Usage(prompt_tokens=1, completion_tokens=1),
        stop_reason="stop",
    )


def _settings() -> RickySettings:
    return RickySettings(gcal=GcalSettings(api_base_url="https://calendar.test/calendar/v3"))


@pytest.mark.parametrize("decision", ["deny", "allow"])
async def test_loop_gates_create_with_complete_preview_and_zero_requests_on_denial(
    tmp_path: Path,
    decision: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/users/me/settings/timezone"):
            return httpx.Response(200, json={"value": "America/Chicago"})
        return httpx.Response(
            200,
            json={
                "id": "event-1",
                "start": {"dateTime": "2026-07-24T10:00:00-05:00"},
                "end": {"dateTime": "2026-07-24T10:30:00-05:00"},
            },
        )

    client = GcalClient(
        auth=FakeAuth(),
        base_url="https://calendar.test/calendar/v3",
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    settings = _settings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    args: dict[str, object] = {
        "account": "work",
        "summary": "Ricky Calendar smoke test",
        "start": "2026-07-24T10:00:00-05:00",
        "end": "2026-07-24T10:30:00-05:00",
        "attendees": ["alex.personal@example.com"],
        "description": "Complete description\nsecond line.",
    }
    provider = FakeProvider(
        [
            [_tool_call("c1", "gcal_create_event", args)],
            [_final("done")],
        ]
    )
    asked: list[PermissionRequestedEvent] = []

    async def responder(event: PermissionRequestedEvent) -> PermissionResponse:
        asked.append(event)
        return PermissionResponse(decision=decision)  # type: ignore[arg-type]

    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([GcalCreateEventTool(client)]),
        settings=settings,
        permission_responder=responder,
        cwd=tmp_path,
    )

    events = [event async for event in loop.run_turn(session, "create the invite")]

    assert len(asked) == 1
    preview = asked[0].summary or ""
    assert "account: work" in preview
    assert "Ricky Calendar smoke test" in preview
    assert "2026-07-24T10:00:00-05:00" in preview
    assert "alex.personal@example.com" in preview
    assert "Complete description\nsecond line." in preview
    assert "attendees will be notified" in preview
    inserts = [request for request in requests if request.method == "POST"]
    if decision == "deny":
        assert requests == []
        assert any("permission denied" in str(message) for message in session.history)
        assert any("deny" in str(event).lower() for event in events)
    else:
        assert len(inserts) == 1
    await client.aclose()


def test_delete_tool_defaults_to_ask_as_destructive(tmp_path: Path) -> None:
    client = GcalClient(
        auth=FakeAuth(),
        base_url="https://calendar.test/calendar/v3",
        timeout_seconds=5,
        transport=httpx.MockTransport(lambda _request: httpx.Response(204)),
    )
    registry = ToolRegistry([GcalDeleteEventTool(client)])
    settings = _settings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())

    from ricky.permissions import PermissionEngine

    tool = registry.get("gcal_delete_event")
    assert tool is not None
    decision = PermissionEngine().decide(
        session,
        tool_name=tool.name,
        risk=tool.risk,
        params={"account": "work", "event_id": "event-1"},
    )
    assert decision.decision == "ask"
