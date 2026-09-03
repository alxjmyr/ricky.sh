"""Loop-level Gmail permission and denial tests."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
from typing import Any

import httpx
import pytest

from ricky.agent import AgentLoop, AgentSession
from ricky.agent.events import PermissionRequestedEvent
from ricky.config import GmailSettings, GoogleAccountSettings, RickySettings
from ricky.llm import Message, MessageDone, TextPart, ToolCallPart, Usage
from ricky.permissions import PermissionResponse
from ricky.tools import ToolRegistry
from ricky.tools.integrations.gmail.client import GmailClient
from ricky.tools.integrations.gmail.tools import (
    GmailDownloadAttachmentTool,
    GmailSendMessageTool,
    GmailTrashTool,
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
    return RickySettings(gmail=GmailSettings(api_base_url="https://gmail.test"))


@pytest.mark.parametrize("decision", ["deny", "allow"])
async def test_loop_gates_send_with_complete_review_and_zero_requests_on_denial(
    tmp_path: Path,
    decision: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "sent-1", "threadId": "thread-1"})

    client = GmailClient(
        auth=FakeAuth(),
        base_url="https://gmail.test",
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    settings = _settings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    args: dict[str, object] = {
        "account": "work",
        "to": ["dana@example.com"],
        "cc": ["sam@example.com"],
        "subject": "Status",
        "body": "Complete body\nwith a second line.",
    }
    provider = FakeProvider(
        [
            [_tool_call("c1", "gmail_send_message", args)],
            [_final("done")],
        ]
    )
    asked: list[PermissionRequestedEvent] = []

    async def responder(event: PermissionRequestedEvent) -> PermissionResponse:
        asked.append(event)
        return PermissionResponse(decision=decision)  # type: ignore[arg-type]

    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([GmailSendMessageTool(client)]),
        settings=settings,
        permission_responder=responder,
        cwd=tmp_path,
    )

    events = [event async for event in loop.run_turn(session, "send the email")]

    assert len(asked) == 1
    preview = asked[0].summary or ""
    assert asked[0].tool_name == "gmail_send_message"
    assert "account: work" in preview
    assert "To: dana@example.com" in preview
    assert "Cc: sam@example.com" in preview
    assert "Subject: Status" in preview
    assert "Complete body\nwith a second line." in preview
    sends = [request for request in requests if request.url.path.endswith("/messages/send")]
    if decision == "deny":
        assert sends == []
        assert any("permission denied" in str(message) for message in session.history)
        assert any("deny" in str(event).lower() for event in events)
    else:
        assert len(sends) == 1
    await client.aclose()


async def test_send_is_not_grantable_so_every_send_re_asks(tmp_path: Path) -> None:
    # Send is the final review gate: it declares no grant scope,
    # so the loop offers nothing to remember and each identical send re-asks even
    # when the user tries to widen it.
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"id": f"sent-{len(requests)}", "threadId": "thread-1"},
        )

    client = GmailClient(
        auth=FakeAuth(),
        base_url="https://gmail.test",
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    settings = _settings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    args: dict[str, object] = {
        "account": "work",
        "to": ["dana@example.com"],
        "body": "Same reviewed body",
    }
    provider = FakeProvider(
        [
            [_tool_call("c1", "gmail_send_message", args)],
            [_tool_call("c2", "gmail_send_message", args)],
            [_final("done")],
        ]
    )
    asked: list[PermissionRequestedEvent] = []

    async def remember(event: PermissionRequestedEvent) -> PermissionResponse:
        asked.append(event)
        return PermissionResponse(decision="allow", grant="tool")  # attempt to widen

    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([GmailSendMessageTool(client)]),
        settings=settings,
        permission_responder=remember,
        cwd=tmp_path,
    )

    await _collect(loop, session)

    assert len(asked) == 2
    assert asked[0].offered_grants == []
    assert len(requests) == 2
    assert session.permission_grants == []
    await client.aclose()


async def test_scoped_trash_grant_generalizes_across_message_id(tmp_path: Path) -> None:
    # The reported case: trashing several emails should prompt once. gmail_trash
    # declares a {account} scope, so a remembered "scoped" grant covers later
    # trashes on the same account regardless of message id — but a trash on a
    # different account still asks.
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "ok"})

    client = GmailClient(
        auth=FakeAuth(),
        base_url="https://gmail.test",
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    settings = _settings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    provider = FakeProvider(
        [
            [_tool_call("t1", "gmail_trash", {"account": "work", "message_id": "m1"})],
            [_tool_call("t2", "gmail_trash", {"account": "work", "message_id": "m2"})],
            [_tool_call("t3", "gmail_trash", {"account": "personal", "message_id": "m3"})],
            [_final("done")],
        ]
    )
    asked: list[PermissionRequestedEvent] = []

    async def responder(event: PermissionRequestedEvent) -> PermissionResponse:
        asked.append(event)
        if len(asked) == 1:
            return PermissionResponse(decision="allow", grant="scoped")
        # The cross-account trash is denied so the unknown "personal" account is
        # never actually dispatched; we only care that it prompted again.
        return PermissionResponse(decision="deny")

    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([GmailTrashTool(client)]),
        settings=settings,
        permission_responder=responder,
        cwd=tmp_path,
    )

    await _collect(loop, session)

    # First work trash asks; second work trash is auto-allowed by the grant;
    # the personal trash asks again (different account).
    assert len(asked) == 2
    assert asked[0].offered_grants[0].id == "scoped"
    assert session.permission_grants[0].tool_name == "gmail_trash"
    assert session.permission_grants[0].params_equal == {"account": "work"}
    trashes = [request for request in requests if request.url.path.endswith("/trash")]
    assert len(trashes) == 2
    await client.aclose()


async def test_denied_attachment_download_does_no_network_or_write(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500, json={"error": {"status": "UNAVAILABLE"}})

    client = GmailClient(
        auth=FakeAuth(),
        base_url="https://gmail.test",
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    settings = _settings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    provider = FakeProvider(
        [
            [
                _tool_call(
                    "c1",
                    "gmail_download_attachment",
                    {
                        "account": "personal/personal",
                        "message_id": "m1",
                        "attachment_id": "att-1",
                    },
                )
            ],
            [_final("not downloaded")],
        ]
    )
    asked: list[PermissionRequestedEvent] = []

    async def deny(event: PermissionRequestedEvent) -> PermissionResponse:
        asked.append(event)
        return PermissionResponse(decision="deny")

    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([GmailDownloadAttachmentTool(client)]),
        settings=settings,
        permission_responder=deny,
        cwd=tmp_path,
    )

    await _collect(loop, session)

    assert requests == []
    assert len(asked) == 1
    assert "m1-<Gmail filename>" in (asked[0].summary or "")
    assert not (tmp_path / "user-data" / "profiles" / "personal" / "downloads").exists()
    await client.aclose()


async def _collect(loop: AgentLoop, session: AgentSession) -> list[object]:
    return [event async for event in loop.run_turn(session, "perform the action")]
