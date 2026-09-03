"""Slack toolpack tests: client, resolution, rendering, tools, and loop flow.

All Slack traffic goes through ``httpx.MockTransport`` — no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from ricky.agent import AgentLoop, AgentSession
from ricky.agent.events import PermissionRequestedEvent, ToolCallFinishedEvent
from ricky.attachments import AttachmentInput, PreparedAttachmentEffect
from ricky.config import RickySettings, SlackSettings
from ricky.llm import Message, MessageDone, TextPart, ToolCallPart, Usage
from ricky.permissions import PermissionResponse
from ricky.tools import ToolContext, ToolRegistry, builtin_tools
from ricky.tools.integrations.slack import SlackToolset, slack_toolset
from ricky.tools.integrations.slack.client import (
    DIRECTORY_PAGE_CAP,
    SlackApiError,
    SlackClient,
    SlackError,
    SlackSendUnknownError,
    SlackTransportError,
)
from ricky.tools.integrations.slack.render import render_message, unescape_text
from ricky.tools.integrations.slack.resolve import (
    ResolutionError,
    resolve_channel,
    resolve_user,
)
from ricky.tools.integrations.slack.tools import (
    DownloadFileParams,
    FindUserParams,
    ListChannelsParams,
    ListUnreadParams,
    MarkReadParams,
    ReadMessagesParams,
    ReadThreadParams,
    SearchParams,
    SendMessageParams,
    SlackDownloadFileTool,
    SlackFindUserTool,
    SlackListChannelsTool,
    SlackListUnreadTool,
    SlackMarkReadTool,
    SlackReadMessagesTool,
    SlackReadThreadTool,
    SlackSearchTool,
    SlackSendMessageTool,
)
from ricky.tools.integrations.slack.types import SlackMessage, SlackUser

BASE = "https://slack.test/api"

USERS = [
    {
        "id": "U1",
        "name": "dana",
        "real_name": "Dana Doe",
        "profile": {"display_name": "dana", "email": "dana@example.com"},
        "tz": "America/New_York",
    },
    {"id": "U2", "name": "alex", "profile": {"display_name": "alex"}},
    {"id": "U3", "name": "dan", "profile": {"display_name": "dan"}},
    {"id": "UB", "name": "botsy", "is_bot": True, "profile": {}},
]

CHANNELS = [
    {"id": "C1", "name": "eng", "is_member": True, "topic": {"value": "engineering"}},
    {"id": "C2", "name": "eng-updates", "is_member": False},
    {"id": "G1", "name": "leads", "is_private": True, "is_member": True},
    {"id": "D1", "is_im": True, "user": "U1"},
]


def ok(**payload: Any) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, **payload})


def fail(error: str) -> httpx.Response:
    return httpx.Response(200, json={"ok": False, "error": error})


class FakeSlack:
    """Routes Web API methods to canned responses and records requests."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.routes: dict[str, Any] = {
            "users.list": ok(members=USERS),
            "conversations.list": ok(channels=CHANNELS),
            **(routes or {}),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        args = _request_args(request)
        self.calls.append((method, args))
        route = self.routes.get(method)
        if route is None:
            return fail(f"unrouted_method_{method}")
        if callable(route):
            return cast(httpx.Response, route(args))
        if isinstance(route, list):
            return route.pop(0)
        return route

    def client(self) -> SlackClient:
        return SlackClient(
            token=SecretStr("xoxp-test"),
            base_url=BASE,
            timeout_seconds=5,
            transport=httpx.MockTransport(self.handler),
        )

    def count(self, method: str) -> int:
        return sum(1 for name, _ in self.calls if name == method)


def _request_args(request: httpx.Request) -> dict[str, Any]:
    body = request.content.decode()
    if request.headers.get("content-type", "").startswith("application/json"):
        return json.loads(body) if body else {}
    return {key: values[0] for key, values in parse_qs(body).items()}


def _ctx(tmp_path: Path, **slack_overrides: Any) -> ToolContext:
    settings = RickySettings(slack=SlackSettings(api_base_url=BASE, **slack_overrides))
    return ToolContext(
        cwd=tmp_path,
        settings=settings,
        session=AgentSession.create(settings, profile_scope=settings.resolve_profile_scope()),
    )


# --- client -----------------------------------------------------------------


async def test_call_sends_bearer_form_post_and_returns_payload() -> None:
    fake = FakeSlack({"auth.test": ok(user="alex", team="lyric")})
    seen: dict[str, str] = {}

    original = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        seen["content_type"] = request.headers.get("content-type", "")
        return original(request)

    client = SlackClient(
        token=SecretStr("xoxp-test"),
        base_url=BASE,
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    payload = await client.call("auth.test", {"probe": "1"})
    await client.aclose()

    assert payload["user"] == "alex"
    assert seen["auth"] == "Bearer xoxp-test"
    assert seen["content_type"].startswith("application/x-www-form-urlencoded")


async def test_envelope_error_maps_to_typed_error_with_hint() -> None:
    fake = FakeSlack({"conversations.history": fail("missing_scope")})
    client = fake.client()
    with pytest.raises(SlackApiError) as excinfo:
        await client.call("conversations.history", {"channel": "C1"})
    await client.aclose()

    assert excinfo.value.code == "missing_scope"
    assert "slack-app-manifest" in str(excinfo.value)
    assert "xoxp-test" not in str(excinfo.value)


async def test_rate_limited_read_retries_then_succeeds() -> None:
    responses = [
        httpx.Response(429, headers={"Retry-After": "0"}, json={"ok": False}),
        ok(members=USERS),
    ]
    fake = FakeSlack({"users.list": responses})
    client = fake.client()
    payload = await client.call("users.list", {"limit": "200"})
    await client.aclose()

    assert payload["members"] == USERS
    assert fake.count("users.list") == 2


async def test_rate_limited_read_gives_up_after_bounded_retries() -> None:
    limited = httpx.Response(
        429, headers={"Retry-After": "0"}, json={"ok": False, "error": "ratelimited"}
    )
    fake = FakeSlack({"users.list": lambda _args: limited})
    client = fake.client()
    with pytest.raises(SlackApiError, match="ratelimited"):
        await client.call("users.list", {})
    await client.aclose()

    assert fake.count("users.list") == 3  # initial + 2 bounded retries


async def test_send_is_never_retried_and_maps_to_unknown_status() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("wire dropped", request=request)

    client = SlackClient(
        token=SecretStr("xoxp-test"),
        base_url=BASE,
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SlackSendUnknownError, match="Check Slack"):
        await client.call("chat.postMessage", json_body={"channel": "C1"}, retryable=False)
    await client.aclose()

    assert calls["n"] == 1


@pytest.mark.parametrize(
    ("retryable", "error_type"),
    [(False, SlackSendUnknownError), (True, SlackTransportError)],
)
async def test_non_json_gateway_response_preserves_send_idempotency_boundary(
    retryable: bool, error_type: type[SlackError]
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(502, text="<html>bad gateway</html>")

    client = SlackClient(
        token=SecretStr("xoxp-test"),
        base_url=BASE,
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(error_type):
        await client.call(
            "chat.postMessage" if not retryable else "users.list",
            json_body={"channel": "C1"} if not retryable else None,
            retryable=retryable,
        )
    await client.aclose()

    assert calls == 1


async def test_download_sends_auth_only_to_slack_hosts() -> None:
    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization", ""))
        return httpx.Response(200, content=b"file")

    client = SlackClient(
        token=SecretStr("xoxp-test"),
        base_url=BASE,
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    assert await client.download("https://files.slack.com/files-pri/T1-F1/file.txt") == b"file"
    with pytest.raises(SlackError, match="non-Slack host.*drive.example"):
        await client.download("https://drive.example/shared/file.txt")
    await client.aclose()

    assert seen_auth == ["Bearer xoxp-test"]


async def test_invalid_api_base_url_is_a_safe_slack_error() -> None:
    client = SlackClient(
        token=SecretStr("xoxp-test"),
        base_url="https://slack.com:bad/api",
        timeout_seconds=5,
    )
    with pytest.raises(SlackError, match="slack.api_base_url"):
        await client.call("auth.test")


async def test_pagination_follows_cursor_and_reports_page_cap() -> None:
    pages = [
        ok(items=[{"n": 1}], response_metadata={"next_cursor": "c2"}),
        ok(items=[{"n": 2}], response_metadata={"next_cursor": ""}),
    ]
    fake = FakeSlack({"probe.list": pages})
    client = fake.client()
    items, truncated = await client.call_paginated("probe.list", {}, items_key="items")
    assert [item["n"] for item in items] == [1, 2]
    assert truncated is False

    fake2 = FakeSlack(
        {"probe.list": lambda _args: ok(items=[{"n": 9}], response_metadata={"next_cursor": "x"})}
    )
    client2 = fake2.client()
    items2, truncated2 = await client2.call_paginated(
        "probe.list", {}, items_key="items", page_cap=2
    )
    await client.aclose()
    await client2.aclose()

    assert len(items2) == 2
    assert truncated2 is True


# --- resolution --------------------------------------------------------------


async def test_channel_resolution_ids_names_and_ambiguity() -> None:
    fake = FakeSlack()
    client = fake.client()

    assert (await resolve_channel(client, "C2")).id == "C2"
    assert (await resolve_channel(client, "#eng")).id == "C1"
    assert (await resolve_channel(client, "leads")).id == "G1"
    assert (await resolve_channel(client, "@dana")).id == "D1"

    with pytest.raises(ResolutionError, match="ambiguous.*C1.*C2"):
        await resolve_channel(client, "en")
    with pytest.raises(ResolutionError, match="no channel matching"):
        await resolve_channel(client, "#nope")
    with pytest.raises(ResolutionError, match="no existing DM"):
        await resolve_channel(client, "@alex")
    await client.aclose()


async def test_user_resolution_exact_email_and_ambiguity() -> None:
    fake = FakeSlack()
    client = fake.client()

    assert (await resolve_user(client, "U9XXXXX")).id == "U9XXXXX"  # id passthrough
    assert (await resolve_user(client, "@dan")).id == "U3"  # exact beats substring
    assert (await resolve_user(client, "dana@example.com")).id == "U1"
    assert (await resolve_user(client, "Dana Doe")).id == "U1"

    with pytest.raises(ResolutionError, match="ambiguous.*U1.*U3"):
        await resolve_user(client, "da")
    await client.aclose()


async def test_all_caps_names_fall_through_id_shape_before_stub() -> None:
    fake = FakeSlack(
        {
            "users.list": ok(
                members=[{"id": "U4", "name": "will", "profile": {"display_name": "will"}}]
            ),
            "conversations.list": ok(channels=[{"id": "C4", "name": "general"}]),
        }
    )
    client = fake.client()

    assert (await resolve_user(client, "WILL")).id == "U4"
    assert (await resolve_channel(client, "GENERAL")).id == "C4"
    assert (await resolve_user(client, "U0UNKNOWN")).id == "U0UNKNOWN"
    assert (await resolve_channel(client, "C0UNKNOWN")).id == "C0UNKNOWN"


async def test_deleted_users_do_not_resolve_as_live_targets(tmp_path: Path) -> None:
    members = [
        {
            "id": "UDEAD",
            "name": "dana",
            "deleted": True,
            "profile": {"display_name": "dana"},
        },
        {
            "id": "ULIVE",
            "name": "dana-doe",
            "real_name": "Dana Doe",
            "profile": {"display_name": ""},
        },
    ]
    fake = FakeSlack({"users.list": ok(members=members)})
    client = fake.client()

    assert (await resolve_user(client, "dana")).id == "ULIVE"
    result = await SlackFindUserTool(client).run(
        FindUserProbe(query="dana"),
        _ctx(tmp_path),
    )
    assert "UDEAD" in result.content
    assert "(deactivated)" in result.content


async def test_truncated_directories_never_return_definitive_name_misses(
    tmp_path: Path,
) -> None:
    fake = FakeSlack(
        {
            "users.list": lambda _args: ok(
                members=[{"id": "U1", "name": "alex"}],
                response_metadata={"next_cursor": "more"},
            ),
            "conversations.list": lambda _args: ok(
                channels=[{"id": "C1", "name": "eng"}],
                response_metadata={"next_cursor": "more"},
            ),
        }
    )
    client = fake.client()

    find_result = await SlackFindUserTool(client).run(
        FindUserProbe(query="nobody"),
        _ctx(tmp_path),
    )
    assert "directory truncated at 1 entries" in find_result.content
    with pytest.raises(ResolutionError, match="directory truncated at 1 entries"):
        await resolve_user(client, "ale")
    # One channel per page, cursor always present: the fetched size is exactly
    # the page budget. Stated via the constant so the intent (a truncated
    # directory never yields a definitive miss) survives cap changes.
    with pytest.raises(
        ResolutionError, match=f"directory truncated at {DIRECTORY_PAGE_CAP} entries"
    ):
        await resolve_channel(client, "#missing")


# --- rendering ---------------------------------------------------------------


def test_unescape_resolves_mentions_channels_links_and_entities() -> None:
    users = {"U1": SlackUser(id="U1", display_name="dana")}
    text = "<@U1> see <#C1|eng> and <https://x.test/doc|the doc> or <https://y.test> &amp; more"
    rendered = unescape_text(text, users)
    assert rendered == "@dana see #eng and the doc (https://x.test/doc) or https://y.test & more"


def test_render_message_header_files_and_thread_marker() -> None:
    users = {"U1": SlackUser(id="U1", display_name="dana")}
    message = SlackMessage(
        channel_id="C1",
        ts="1752831240.001200",
        user_id="U1",
        text="shipped the fix",
        reply_count=3,
        files=[
            {"id": "F1", "name": "plan.pdf", "mimetype": "application/pdf", "size": 2202009}  # type: ignore[list-item]
        ],
    )
    rendered = render_message(message, users)
    assert "[2025-07-18" in rendered
    assert "@dana" in rendered
    assert "(ts 1752831240.001200, thread: 3 replies)" in rendered
    assert "[file: plan.pdf · application/pdf · 2.1MB · id F1]" in rendered


# --- tools -------------------------------------------------------------------


async def test_list_channels_filters_and_marks_membership(tmp_path: Path) -> None:
    fake = FakeSlack()
    tool = SlackListChannelsTool(fake.client())
    result = await tool.run(ListChannelsProbe(kinds=["public"], name_filter="eng"), _ctx(tmp_path))
    assert not result.is_error
    assert "C1  #eng  (public)" in result.content
    assert "C2  #eng-updates  (public, not a member)" in result.content
    assert "leads" not in result.content


async def test_list_channels_filters_dms_by_counterpart_name(tmp_path: Path) -> None:
    fake = FakeSlack()
    tool = SlackListChannelsTool(fake.client())
    result = await tool.run(
        ListChannelsProbe(kinds=["im"], name_filter="dana"),
        _ctx(tmp_path),
    )

    assert not result.is_error
    assert "D1  DM with @dana" in result.content


def _by_kind(**per_kind: Any) -> Any:
    """A ``conversations.list`` route that honours the requested ``types``.

    Each value is either a channel list or ``(channel_list, always_paginate)``.
    This mirrors the real API, where ``types`` selects what is fetched — the
    old shared fetch ignored it and let public channels crowd out DMs.
    """

    def route(args: dict[str, Any]) -> httpx.Response:
        entry = per_kind.get(args.get("types", ""), [])
        channels, paginate = entry if isinstance(entry, tuple) else (entry, False)
        if paginate:
            return ok(channels=channels, response_metadata={"next_cursor": "more"})
        return ok(channels=channels)

    return route


async def test_list_channels_returns_dms_when_public_directory_overflows(
    tmp_path: Path,
) -> None:
    """Regression: a crowded public directory must not starve the DM fetch."""
    crowd = [{"id": f"C{n}", "name": f"chan-{n}", "is_member": False} for n in range(200)]
    dms = [{"id": f"D{n}", "is_im": True, "user": "U1"} for n in range(5)]
    fake = FakeSlack({"conversations.list": _by_kind(public_channel=(crowd, True), im=dms)})
    tool = SlackListChannelsTool(fake.client())

    result = await tool.run(ListChannelsProbe(kinds=["im"]), _ctx(tmp_path))

    assert not result.is_error
    assert "5 conversation(s) [im]" in result.content
    assert "D0  DM with @dana" in result.content
    assert "truncated" not in result.content  # the im fetch completed


async def test_channels_fetches_and_caches_each_kind_separately() -> None:
    fake = FakeSlack(
        {
            "conversations.list": _by_kind(
                public_channel=[CHANNELS[0]], im=[CHANNELS[3]], mpim=[], private_channel=[]
            )
        }
    )
    client = fake.client()

    first, _ = await client.channels(["im"])
    assert [c.id for c in first] == ["D1"]
    types_asked = [args["types"] for method, args in fake.calls if method == "conversations.list"]
    assert types_asked == ["im"]

    await client.channels(["im"])  # cached: no new request
    await client.channels(["im", "public"])  # only the new kind is fetched
    await client.aclose()

    types_asked = [args["types"] for method, args in fake.calls if method == "conversations.list"]
    assert types_asked == ["im", "public_channel"]


async def test_list_channels_reports_truncation_only_for_the_affected_kind(
    tmp_path: Path,
) -> None:
    fake = FakeSlack(
        {"conversations.list": _by_kind(public_channel=([CHANNELS[0]], True), im=[CHANNELS[3]])}
    )
    client = fake.client()

    dms = await SlackListChannelsTool(client).run(ListChannelsProbe(kinds=["im"]), _ctx(tmp_path))
    assert "truncated" not in dms.content

    public = await SlackListChannelsTool(client).run(
        ListChannelsProbe(kinds=["public"]), _ctx(tmp_path)
    )
    assert f"[public directory truncated at {DIRECTORY_PAGE_CAP} entries" in public.content
    assert "im directory truncated" not in public.content


async def test_list_channels_distinguishes_no_match_from_truncation(tmp_path: Path) -> None:
    fake = FakeSlack(
        {"conversations.list": _by_kind(public_channel=([CHANNELS[0]], True), im=[CHANNELS[3]])}
    )
    tool = SlackListChannelsTool(fake.client())

    miss = await tool.run(ListChannelsProbe(kinds=["im"], name_filter="zzznomatch"), _ctx(tmp_path))
    assert "no conversations match 'zzznomatch'" in miss.content
    assert "truncated" not in miss.content


async def test_opening_a_dm_invalidates_only_the_dm_directory(tmp_path: Path) -> None:
    fake = FakeSlack(
        {
            "conversations.list": _by_kind(
                public_channel=[CHANNELS[0]], private_channel=[], im=[CHANNELS[3]], mpim=[]
            ),
            "conversations.open": ok(channel={"id": "D1"}),
            "chat.postMessage": ok(ts="99.1", channel="D1"),
        }
    )
    client = fake.client()
    await client.channels()  # warm every kind
    before = fake.count("conversations.list")

    await SlackSendMessageTool(client).run(SendProbe(target="@dana", text="hi"), _ctx(tmp_path))
    await client.channels()
    await client.aclose()

    refetched = [
        args["types"] for method, args in fake.calls[before:] if method == "conversations.list"
    ]
    assert refetched == ["im"]


async def test_resolution_fetches_only_the_kinds_a_reference_can_match() -> None:
    routes = _by_kind(
        public_channel=[CHANNELS[0]], private_channel=[CHANNELS[2]], im=[CHANNELS[3]], mpim=[]
    )
    fake = FakeSlack({"conversations.list": routes})
    client = fake.client()

    await resolve_channel(client, "@dana")
    assert [a["types"] for m, a in fake.calls if m == "conversations.list"] == ["im"]

    await resolve_channel(client, "#eng")
    await client.aclose()
    assert sorted(a["types"] for m, a in fake.calls if m == "conversations.list") == [
        "im",
        "mpim",
        "private_channel",
        "public_channel",
    ]


async def test_dm_resolution_failure_ignores_unrelated_public_truncation(
    tmp_path: Path,
) -> None:
    """A crowded public directory cannot hide a DM — do not blame it."""
    fake = FakeSlack({"conversations.list": _by_kind(public_channel=([CHANNELS[0]], True), im=[])})
    client = fake.client()

    with pytest.raises(ResolutionError, match="sending a message will open one"):
        await resolve_channel(client, "@dana")
    await client.aclose()


async def test_find_user_matches_and_errors(tmp_path: Path) -> None:
    fake = FakeSlack()
    tool = SlackFindUserTool(fake.client())
    hit = await tool.run(FindUserProbe(query="dana@example.com"), _ctx(tmp_path))
    assert "U1" in hit.content and "dana@example.com" in hit.content

    miss = await tool.run(FindUserProbe(query="nobody"), _ctx(tmp_path))
    assert miss.is_error
    assert "directory truncated" not in miss.content


async def test_search_renders_hits_with_channel_and_permalink(tmp_path: Path) -> None:
    match = {
        "ts": "1752831240.000100",
        "text": "the retry fix",
        "user": "U1",
        "channel": {"id": "C1", "name": "eng"},
        "permalink": "https://ws.slack.com/archives/C1/p1",
    }
    fake = FakeSlack({"search.messages": ok(messages={"matches": [match], "total": 1})})
    tool = SlackSearchTool(fake.client())
    result = await tool.run(SearchProbe(query="retry fix in:#eng"), _ctx(tmp_path))
    assert "1 match(es)" in result.content
    assert "in #eng" in result.content
    assert "https://ws.slack.com/archives/C1/p1" in result.content
    assert fake.calls[0][1]["sort"] == "timestamp"


async def test_search_labels_dm_and_group_dm_hits_by_kind(tmp_path: Path) -> None:
    """A DM hit must not render as ``#name``, and must never render locationless."""
    matches = [
        {
            "ts": "1.0",
            "text": "dm hit",
            "user": "U1",
            "channel": {"id": "D1", "name": "dana", "is_im": True},
        },
        {
            "ts": "2.0",
            "text": "unnamed dm hit",
            "user": "U1",
            "channel": {"id": "D9", "is_im": True},
        },
        {
            "ts": "3.0",
            "text": "group hit",
            "user": "U1",
            "channel": {"id": "G9", "name": "mpdm-alex--dana-1", "is_mpim": True},
        },
    ]
    fake = FakeSlack({"search.messages": ok(messages={"matches": matches, "total": 3})})
    tool = SlackSearchTool(fake.client())
    result = await tool.run(SearchProbe(query="is:dm"), _ctx(tmp_path))

    assert "in DM with @dana" in result.content
    assert "in DM D9" in result.content  # no name from Slack: fall back to the id
    assert "in group DM mpdm-alex--dana-1" in result.content
    assert "in #dana" not in result.content


async def test_search_dm_hit_named_by_user_id_resolves_to_a_handle(tmp_path: Path) -> None:
    """Live Slack names an IM search hit by the counterpart's id, not a handle."""
    match = {
        "ts": "1.0",
        "text": "dm hit",
        "user": "U1",
        "channel": {"id": "D1", "name": "U1", "is_im": True},
    }
    fake = FakeSlack({"search.messages": ok(messages={"matches": [match], "total": 1})})
    result = await SlackSearchTool(fake.client()).run(SearchProbe(query="is:dm"), _ctx(tmp_path))

    assert "in DM with @dana" in result.content
    assert "@U1" not in result.content


async def test_read_messages_resolves_channel_converts_dates_and_orders(tmp_path: Path) -> None:
    history = [
        {"ts": "1752831300.000200", "user": "U2", "text": "later"},
        {"ts": "1752831240.000100", "user": "U1", "text": "earlier", "reply_count": 2},
    ]
    fake = FakeSlack({"conversations.history": ok(messages=history, has_more=True)})
    tool = SlackReadMessagesTool(fake.client())
    result = await tool.run(ReadMessagesProbe(channel="#eng", oldest="2026-07-18"), _ctx(tmp_path))

    request = next(args for method, args in fake.calls if method == "conversations.history")
    assert request["channel"] == "C1"
    assert float(request["oldest"]) == pytest.approx(1784332800.0, abs=86400)
    assert request["limit"] == "30"  # settings default_history_limit
    assert result.content.index("earlier") < result.content.index("later")
    assert "thread: 2 replies" in result.content
    assert "[result truncated" in result.content


async def test_read_thread_renders_root_and_replies(tmp_path: Path) -> None:
    thread = [
        {"ts": "1.0", "user": "U1", "text": "root", "reply_count": 1, "thread_ts": "1.0"},
        {"ts": "2.0", "user": "U2", "text": "reply", "thread_ts": "1.0"},
    ]
    fake = FakeSlack({"conversations.replies": ok(messages=thread)})
    tool = SlackReadThreadTool(fake.client())
    result = await tool.run(ReadThreadProbe(channel="C1", thread_ts="1.0"), _ctx(tmp_path))
    assert "Thread 1.0" in result.content
    assert result.content.index("root") < result.content.index("reply")


async def test_send_posts_json_root_and_thread(tmp_path: Path) -> None:
    fake = FakeSlack({"chat.postMessage": ok(ts="99.1", channel="C1")})
    tool = SlackSendMessageTool(fake.client())

    root = await tool.run(SendProbe(target="#eng", text="hi <@U1>"), _ctx(tmp_path))
    assert root.content == "Sent to #eng (ts 99.1)."
    method, body = next(call for call in fake.calls if call[0] == "chat.postMessage")
    assert body == {"channel": "C1", "text": "hi <@U1>"}

    reply = await tool.run(
        SendProbe(target="C1", text="follow-up", thread_ts="1.0"), _ctx(tmp_path)
    )
    assert "in thread 1.0" in reply.content
    assert fake.calls[-1][1]["thread_ts"] == "1.0"


async def test_send_to_user_opens_dm_first(tmp_path: Path) -> None:
    fake = FakeSlack(
        {
            "conversations.open": ok(channel={"id": "D9"}),
            "chat.postMessage": ok(ts="7.7", channel="D9"),
        }
    )
    tool = SlackSendMessageTool(fake.client())
    result = await tool.run(SendProbe(target="@alex", text="ping"), _ctx(tmp_path))
    assert result.content == "Sent to DM with @alex (ts 7.7)."
    assert fake.count("conversations.open") == 1
    assert fake.calls[-1][1]["channel"] == "D9"


async def test_send_uploads_attachments_then_completes_once_without_bearer(
    tmp_path: Path,
) -> None:
    attachment = tmp_path / "report.txt"
    attachment.write_text("attached report")
    upload_auth: list[str] = []

    def upload_target(args: dict[str, Any]) -> httpx.Response:
        assert args["filename"] == "report.txt"
        assert args["length"] == "15"
        return ok(upload_url="https://files.slack.com/upload/v1", file_id="FNEW")

    fake = FakeSlack(
        {
            "files.getUploadURLExternal": upload_target,
            "v1": httpx.Response(200, text="OK"),
            "files.completeUploadExternal": ok(files=[{"id": "FNEW"}]),
        }
    )
    original = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "files.slack.com":
            upload_auth.append(request.headers.get("authorization", ""))
            assert b"attached report" in request.content
        return original(request)

    client = SlackClient(
        token=SecretStr("xoxp-test"),
        base_url=BASE,
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    result = await SlackSendMessageTool(client).run(
        SendMessageParams(
            target="#eng",
            text="See attached.",
            attachments=[AttachmentInput(path="report.txt")],
        ),
        _ctx(tmp_path),
    )

    assert not result.is_error
    assert upload_auth == [""]
    assert fake.count("files.getUploadURLExternal") == 1
    assert fake.count("files.completeUploadExternal") == 1
    assert fake.count("chat.postMessage") == 0
    complete = next(args for method, args in fake.calls if method == "files.completeUploadExternal")
    assert complete == {
        "files": [{"id": "FNEW", "title": "report.txt"}],
        "channel_id": "C1",
        "initial_comment": "See attached.",
    }


async def test_prepared_slack_attachment_uses_exact_preflight_bytes_after_source_swap(
    tmp_path: Path,
) -> None:
    source = tmp_path / "report.bin"
    source.write_bytes(b"frozen-before-dispatch")
    uploaded: list[bytes] = []

    fake = FakeSlack(
        {
            "files.getUploadURLExternal": ok(
                upload_url="https://files.slack.com/upload/v1",
                file_id="FNEW",
            ),
            "v1": httpx.Response(200, text="OK"),
            "files.completeUploadExternal": ok(files=[{"id": "FNEW"}]),
        }
    )
    original = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "files.slack.com":
            uploaded.append(request.content)
        return original(request)

    client = SlackClient(
        token=SecretStr("xoxp-test"),
        base_url=BASE,
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    ctx = _ctx(tmp_path)
    tool = SlackSendMessageTool(client)
    args = SendMessageParams(
        target="#eng",
        text="Attached.",
        occurrence_id="task-1-revision-2",
        attachments=[AttachmentInput(path="report.bin")],
    )
    prepared = await tool.prepare_effect(args.model_dump(mode="python"), ctx)
    restored = PreparedAttachmentEffect.model_validate_json(prepared.model_dump_json())

    source.write_bytes(b"changed-after-preflight")
    changed_identity = tool.effect_identity(args.model_dump(mode="python"), ctx)
    result = await tool.run_prepared(args, restored, ctx)

    assert not result.is_error
    assert len(uploaded) == 1
    assert b"frozen-before-dispatch" in uploaded[0]
    assert b"changed-after-preflight" not in uploaded[0]
    assert restored.identity.action_key != changed_identity.action_key


async def test_send_rejects_empty_text_without_network(tmp_path: Path) -> None:
    fake = FakeSlack()
    tool = SlackSendMessageTool(fake.client())
    result = await tool.run(SendProbe(target="#eng", text="   "), _ctx(tmp_path))
    assert result.is_error
    assert fake.calls == []


def test_send_effect_preflight_rejects_empty_text_without_network(tmp_path: Path) -> None:
    fake = FakeSlack()
    tool = SlackSendMessageTool(fake.client())

    with pytest.raises(ValueError, match="empty message"):
        tool.effect_identity(
            {
                "target": "#eng",
                "text": "   ",
                "occurrence_id": "task-1",
            },
            _ctx(tmp_path),
        )

    assert fake.calls == []


async def test_download_writes_confined_collision_safe_file(tmp_path: Path) -> None:
    content = bytes(range(256))
    info = {
        "id": "F1",
        "name": "Q3 plan!.pdf",
        "url_private": "https://files.slack.com/files-pri/T1-F1/Q3-plan.pdf",
    }
    seen_auth = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_auth
        if request.url.path.endswith("files.info"):
            return ok(file=info)
        seen_auth = request.headers.get("authorization", "")
        return httpx.Response(200, content=content)

    client = SlackClient(
        token=SecretStr("xoxp-test"),
        base_url=BASE,
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    tool = SlackDownloadFileTool(client)
    result = await tool.run(DownloadProbe(file_id="F1"), _ctx(tmp_path))
    assert not result.is_error

    saved = tmp_path / "user-data" / "downloads" / "slack" / "F1-Q3_plan_.pdf"
    assert saved.read_bytes() == content
    assert str(saved) in result.content
    assert saved.parent.stat().st_mode & 0o777 == 0o700
    assert saved.stat().st_mode & 0o777 == 0o600
    assert seen_auth == "Bearer xoxp-test"


async def test_download_dir_escaping_user_data_is_refused(tmp_path: Path) -> None:
    fake = FakeSlack(
        {"files.info": ok(file={"id": "F1", "url_private": "https://files.slack.com/files/F1"})}
    )
    registry = ToolRegistry([SlackDownloadFileTool(fake.client())])
    ctx = _ctx(tmp_path)
    ctx.settings.slack.download_dir = "../outside"
    result = await registry.dispatch(
        "slack_download_file",
        {"file_id": "F1"},
        ctx,
    )
    assert result.is_error
    assert result.content.startswith("slack_download_file failed:")
    assert "escapes user_data_dir" in result.content
    assert not (tmp_path / "outside").exists()


async def test_download_rejects_invalid_file_id_before_network_or_write(tmp_path: Path) -> None:
    fake = FakeSlack()
    registry = ToolRegistry([SlackDownloadFileTool(fake.client())])

    result = await registry.dispatch(
        "slack_download_file",
        {"file_id": "../F1"},
        _ctx(tmp_path),
    )

    assert result.is_error
    assert result.content.startswith("Invalid arguments for slack_download_file:")
    assert fake.calls == []
    assert not (tmp_path / "user-data" / "downloads").exists()


async def test_registry_owns_slack_error_conversion(tmp_path: Path) -> None:
    fake = FakeSlack({"search.messages": fail("missing_scope")})
    registry = ToolRegistry([SlackSearchTool(fake.client())])

    result = await registry.dispatch(
        "slack_search",
        {"query": "probe"},
        _ctx(tmp_path),
    )

    assert result.is_error
    assert result.content.startswith("slack_search failed: Slack search.messages failed:")


# --- read state ---------------------------------------------------------------

# Three DMs and one group DM. ``updated`` orders the unread probe.
UNREAD_CHANNELS = [
    {"id": "D1", "is_im": True, "user": "U1", "updated": 3000},
    {"id": "D2", "is_im": True, "user": "U3", "updated": 2000},
    {"id": "D3", "is_im": True, "user": "U2", "updated": 1000},
]


def _info_by_channel(**per_channel: Any) -> Any:
    """A ``conversations.info`` route keyed on the requested channel id."""

    def route(args: dict[str, Any]) -> httpx.Response:
        entry = per_channel.get(args.get("channel", ""))
        if entry is None:
            return fail("channel_not_found")
        if isinstance(entry, httpx.Response):
            return entry
        return ok(channel=entry)

    return route


def _unread_fake(**per_channel: Any) -> FakeSlack:
    """A workspace whose DM directory is ``UNREAD_CHANNELS``."""
    return FakeSlack(
        {
            "conversations.list": _by_kind(im=UNREAD_CHANNELS),
            "conversations.info": _info_by_channel(**per_channel),
            "auth.test": ok(user_id="U2", user="alex", team="t"),
        }
    )


async def test_read_state_reads_the_count_directly_for_dms() -> None:
    """Path A: Slack reports unread_count_display for DMs, so trust it."""
    fake = FakeSlack(
        {
            "conversations.info": ok(
                channel={
                    "id": "D1",
                    "is_im": True,
                    "last_read": "100.0",
                    "unread_count_display": 3,
                    "latest": {"ts": "140.0"},
                }
            )
        }
    )
    client = fake.client()

    state = await client.read_state("D1", probe_limit=50)

    assert (state.last_read, state.unread_count, state.latest_ts) == ("100.0", 3, "140.0")
    assert state.has_unread
    # Path A must not spend a history call.
    assert fake.count("conversations.history") == 0
    await client.aclose()


async def test_read_state_counts_history_when_slack_omits_the_count() -> None:
    """Path B: channels carry only last_read, and own messages are not unread."""
    fake = FakeSlack(
        {
            "conversations.info": ok(channel={"id": "C1", "name": "eng", "last_read": "100.0"}),
            "conversations.history": ok(
                messages=[
                    {"ts": "130.0", "user": "U1", "text": "newest"},
                    {"ts": "120.0", "user": "U2", "text": "mine, already seen by me"},
                    {"ts": "110.0", "user": "U3", "text": "theirs"},
                    {"ts": "105.0", "user": "U1", "subtype": "channel_join", "text": "joined"},
                    {"ts": "100.0", "user": "U1", "text": "the last_read boundary itself"},
                ]
            ),
            "auth.test": ok(user_id="U2", user="alex", team="t"),
        }
    )
    client = fake.client()

    state = await client.read_state("C1", probe_limit=50)

    # 130 and 110 only: 120 is the user's own, 105 is a join notice, and 100 is
    # the boundary, which the user has by definition already read.
    assert state.unread_count == 2
    assert state.latest_ts == "130.0"
    history = [args for method, args in fake.calls if method == "conversations.history"]
    assert history[0]["oldest"] == "100.0"
    await client.aclose()


async def test_list_unread_omits_conversations_with_nothing_new(tmp_path: Path) -> None:
    fake = _unread_fake(
        D1={"id": "D1", "last_read": "1.0", "unread_count_display": 0, "latest": {"ts": "9.0"}},
        D2={"id": "D2", "last_read": "1.0", "unread_count_display": 2, "latest": {"ts": "8.0"}},
        D3={"id": "D3", "last_read": "1.0", "unread_count_display": 0, "latest": {"ts": "7.0"}},
    )
    tool = SlackListUnreadTool(fake.client())

    result = await tool.run(UnreadProbe(kinds=["im"], include_preview=False), _ctx(tmp_path))

    assert not result.is_error
    assert "1 conversation(s) with unread messages out of 3 checked [im]" in result.content
    assert "D2  DM with @dan  (2 unread" in result.content
    assert "D1" not in result.content
    assert "D3" not in result.content


async def test_list_unread_orders_by_newest_activity(tmp_path: Path) -> None:
    """The most recent unread conversation must lead the list."""
    fake = _unread_fake(
        D1={"id": "D1", "last_read": "1.0", "unread_count_display": 1, "latest": {"ts": "50.0"}},
        D2={"id": "D2", "last_read": "1.0", "unread_count_display": 1, "latest": {"ts": "90.0"}},
        D3={"id": "D3", "last_read": "1.0", "unread_count_display": 1, "latest": {"ts": "70.0"}},
    )
    tool = SlackListUnreadTool(fake.client())

    result = await tool.run(UnreadProbe(kinds=["im"], include_preview=False), _ctx(tmp_path))

    reported = [line.split()[0] for line in result.content.splitlines() if line.startswith("D")]
    assert reported == ["D2", "D3", "D1"]
    assert "3 conversation(s) with unread messages out of 3 checked" in result.content


async def test_list_unread_survives_one_unreadable_conversation(tmp_path: Path) -> None:
    """A single conversations.info failure must not lose the other results."""
    fake = _unread_fake(
        D1={"id": "D1", "last_read": "1.0", "unread_count_display": 4, "latest": {"ts": "50.0"}},
        D2=fail("channel_not_found"),
        D3={"id": "D3", "last_read": "1.0", "unread_count_display": 0, "latest": {"ts": "7.0"}},
    )
    tool = SlackListUnreadTool(fake.client())

    result = await tool.run(UnreadProbe(kinds=["im"], include_preview=False), _ctx(tmp_path))

    assert not result.is_error
    assert "D1  DM with @dana  (4 unread" in result.content
    assert "[could not check 1: D2" in result.content


async def test_list_unread_reports_directory_truncation(tmp_path: Path) -> None:
    """An incomplete conversation directory is never reported as complete."""
    fake = FakeSlack(
        {
            "conversations.list": _by_kind(im=(UNREAD_CHANNELS, True)),
            "conversations.info": _info_by_channel(
                **{
                    c["id"]: {"id": c["id"], "last_read": "1.0", "unread_count_display": 0}
                    for c in UNREAD_CHANNELS
                }
            ),
            "auth.test": ok(user_id="U2", user="alex", team="t"),
        }
    )
    tool = SlackListUnreadTool(fake.client())

    result = await tool.run(UnreadProbe(kinds=["im"], include_preview=False), _ctx(tmp_path))

    assert "im directory truncated" in result.content


async def test_list_unread_bounds_the_probe_and_says_what_it_skipped(tmp_path: Path) -> None:
    """The fan-out is capped, and a capped run must never look exhaustive."""
    fake = _unread_fake(
        D1={"id": "D1", "last_read": "1.0", "unread_count_display": 1, "latest": {"ts": "50.0"}},
        D2={"id": "D2", "last_read": "1.0", "unread_count_display": 1, "latest": {"ts": "90.0"}},
        D3={"id": "D3", "last_read": "1.0", "unread_count_display": 1, "latest": {"ts": "70.0"}},
    )
    tool = SlackListUnreadTool(fake.client())

    result = await tool.run(
        UnreadProbe(kinds=["im"], include_preview=False),
        _ctx(tmp_path, unread_max_conversations=1),
    )

    # D1 has the highest `updated`, so the single unit of budget goes to it.
    assert fake.count("conversations.info") == 1
    assert "out of 1 checked" in result.content
    assert "[checked the 1 most recently active of 3 conversation(s); 2 were not checked]" in (
        result.content
    )


async def test_mark_read_resolves_the_newest_message_when_ts_is_empty(tmp_path: Path) -> None:
    fake = FakeSlack(
        {
            "conversations.list": _by_kind(public_channel=[CHANNELS[0]]),
            "conversations.history": ok(messages=[{"ts": "42.5", "user": "U1", "text": "last"}]),
            "conversations.mark": ok(),
        }
    )
    tool = SlackMarkReadTool(fake.client())

    result = await tool.run(MarkReadProbe(channel="#eng"), _ctx(tmp_path))

    assert not result.is_error
    marks = [args for method, args in fake.calls if method == "conversations.mark"]
    assert marks == [{"channel": "C1", "ts": "42.5"}]
    assert "Marked #eng read up to ts 42.5" in result.content
    assert "cannot mark it unread" in result.content
    assert result.effect_receipt is not None
    assert result.effect_receipt.provider_reference == "42.5"


async def test_mark_read_refuses_a_substring_only_target(tmp_path: Path) -> None:
    """Marking the wrong conversation hides messages, so require an exact name."""
    fake = FakeSlack({"conversations.list": _by_kind(public_channel=CHANNELS[:2])})
    tool = SlackMarkReadTool(fake.client())

    with pytest.raises(ResolutionError):
        await tool.run(MarkReadProbe(channel="#eng-"), _ctx(tmp_path))

    assert fake.count("conversations.mark") == 0


async def test_mark_read_permission_summary_names_the_target_and_cutoff(tmp_path: Path) -> None:
    tool = SlackMarkReadTool(FakeSlack().client())
    ctx = _ctx(tmp_path)

    newest = tool.summarize_permission({"channel": "#eng", "ts": ""}, ctx)
    pinned = tool.summarize_permission({"channel": "#eng", "ts": "42.5"}, ctx)

    assert "mark #eng read up to its newest message" in newest
    assert "cannot be undone" in newest
    assert "mark #eng read up to message 42.5" in pinned


def test_mark_read_effect_identity_keys_on_the_conversation_and_ts(tmp_path: Path) -> None:
    tool = SlackMarkReadTool(FakeSlack().client())
    ctx = _ctx(tmp_path)

    first = tool.effect_identity({"channel": "#eng", "ts": "1.0"}, ctx)
    same = tool.effect_identity({"channel": "#ENG", "ts": "1.0"}, ctx)
    other_ts = tool.effect_identity({"channel": "#eng", "ts": "2.0"}, ctx)
    newest = tool.effect_identity({"channel": "#eng", "ts": ""}, ctx)

    assert first.operation == "slack.mark_read"
    assert first.action_key == same.action_key
    assert first.action_key != other_ts.action_key
    # "newest" is not a fixed point and must never collide with a pinned ts.
    assert newest.occurrence == "latest"
    assert newest.action_key not in {first.action_key, other_ts.action_key}


async def test_mark_read_missing_scope_tells_the_user_to_reinstall() -> None:
    fake = FakeSlack({"conversations.mark": fail("missing_scope")})
    client = fake.client()

    with pytest.raises(SlackApiError) as caught:
        await client.mark_read("C1", "1.0")

    message = str(caught.value)
    assert "im:write" in message
    assert "reinstall the app" in message
    await client.aclose()


async def test_mark_read_reaches_the_permission_gate_as_a_mutating_tool(tmp_path: Path) -> None:
    fake = FakeSlack(
        {
            "conversations.list": _by_kind(public_channel=[CHANNELS[0]]),
            "conversations.mark": ok(),
        }
    )
    settings = RickySettings(slack=SlackSettings(api_base_url=BASE))
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    provider = _FakeProvider(
        [
            [_tool_call("slack_mark_read", {"channel": "#eng", "ts": "42.5"})],
            [_final("done")],
        ]
    )
    asked: list[PermissionRequestedEvent] = []

    async def responder(event: PermissionRequestedEvent) -> PermissionResponse:
        asked.append(event)
        return PermissionResponse(decision="deny")

    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([SlackMarkReadTool(fake.client())]),
        settings=settings,
        permission_responder=responder,
        cwd=tmp_path,
    )
    [event async for event in loop.run_turn(session, "clear that channel")]

    assert SlackMarkReadTool.risk == "mutating"
    assert [event.tool_name for event in asked] == ["slack_mark_read"]
    assert "mark #eng read up to message 42.5" in (asked[0].summary or "")
    assert fake.count("conversations.mark") == 0  # denied, so nothing moved


# --- toolset & registry -------------------------------------------------------


def test_toolset_absent_without_token_and_composes_with_builtins() -> None:
    assert slack_toolset(RickySettings(slack_user_token=None)) is None

    toolset = slack_toolset(RickySettings(slack_user_token=SecretStr("xoxp-test")))
    assert isinstance(toolset, SlackToolset)
    names = [tool.name for tool in toolset.tools]
    assert names == [
        "slack_list_channels",
        "slack_list_unread",
        "slack_find_user",
        "slack_search",
        "slack_read_messages",
        "slack_read_thread",
        "slack_send_message",
        "slack_mark_read",
        "slack_download_file",
    ]
    registry = ToolRegistry([*builtin_tools(), *toolset.tools])
    assert registry.get("slack_send_message") is not None


# --- loop-level permission flow ----------------------------------------------


class _FakeProvider:
    name = "fake"

    def __init__(self, scripts: list[list[Any]]) -> None:
        self.scripts = scripts

    async def stream(self, request: Any):  # noqa: ANN401
        for event in self.scripts.pop(0):
            yield event

    async def aclose(self) -> None:
        pass


def _tool_call(name: str, args: dict[str, object]) -> MessageDone:
    return MessageDone(
        message=Message(role="assistant", content=[ToolCallPart(id="c1", name=name, args=args)]),
        usage=Usage(prompt_tokens=1, completion_tokens=1),
        stop_reason="tool_calls",
    )


def _final(text: str) -> MessageDone:
    return MessageDone(
        message=Message(role="assistant", content=[TextPart(text=text)]),
        usage=Usage(prompt_tokens=1, completion_tokens=1),
        stop_reason="stop",
    )


@pytest.mark.parametrize("decision", ["deny", "allow"])
async def test_loop_gates_slack_send_behind_permission(tmp_path: Path, decision: str) -> None:
    fake = FakeSlack({"chat.postMessage": ok(ts="5.5", channel="C1")})
    settings = RickySettings(slack=SlackSettings(api_base_url=BASE))
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    send_args: dict[str, object] = {"target": "#eng", "text": "status update"}
    provider = _FakeProvider([[_tool_call("slack_send_message", send_args)], [_final("done")]])
    asked: list[PermissionRequestedEvent] = []

    async def responder(event: PermissionRequestedEvent) -> PermissionResponse:
        asked.append(event)
        return PermissionResponse(decision=decision)  # type: ignore[arg-type]

    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([SlackSendMessageTool(fake.client())]),
        settings=settings,
        permission_responder=responder,
        cwd=tmp_path,
    )
    events = [event async for event in loop.run_turn(session, "send the update")]

    assert len(asked) == 1
    assert asked[0].tool_name == "slack_send_message"
    assert asked[0].summary == "to #eng\n---\nstatus update\n---"
    sends = fake.count("chat.postMessage")
    if decision == "deny":
        assert sends == 0
        assert any("den" in str(event).lower() for event in events)
    else:
        assert sends == 1


async def test_loop_never_sends_to_a_substring_only_target(tmp_path: Path) -> None:
    fake = FakeSlack({"chat.postMessage": ok(ts="5.5", channel="C2")})
    settings = RickySettings(slack=SlackSettings(api_base_url=BASE))
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    provider = _FakeProvider(
        [
            [_tool_call("slack_send_message", {"target": "eng-u", "text": "status"})],
            [_final("understood")],
        ]
    )

    async def allow(_event: PermissionRequestedEvent) -> PermissionResponse:
        return PermissionResponse(decision="allow")

    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([SlackSendMessageTool(fake.client())]),
        settings=settings,
        permission_responder=allow,
        cwd=tmp_path,
    )
    events = [event async for event in loop.run_turn(session, "send it")]
    finished = [event for event in events if isinstance(event, ToolCallFinishedEvent)]

    assert fake.count("chat.postMessage") == 0
    assert finished[0].is_error
    assert "did you mean #eng-updates (C2)" in (finished[0].content or "")


async def test_loop_denies_download_before_network_or_local_write(tmp_path: Path) -> None:
    fake = FakeSlack()
    settings = RickySettings(slack=SlackSettings(api_base_url=BASE))
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    provider = _FakeProvider(
        [
            [_tool_call("slack_download_file", {"file_id": "F1"})],
            [_final("not downloaded")],
        ]
    )
    asked: list[PermissionRequestedEvent] = []

    async def deny(event: PermissionRequestedEvent) -> PermissionResponse:
        asked.append(event)
        return PermissionResponse(decision="deny")

    tool = SlackDownloadFileTool(fake.client())
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([tool]),
        settings=settings,
        permission_responder=deny,
        cwd=tmp_path,
    )
    events = [event async for event in loop.run_turn(session, "download it")]

    assert tool.risk == "mutating"
    assert len(asked) == 1
    assert asked[0].summary is not None
    expected = tmp_path / "user-data" / "downloads" / "slack" / "F1-<Slack filename>"
    assert str(expected) in asked[0].summary
    assert fake.calls == []
    assert not (tmp_path / "user-data" / "downloads").exists()
    assert any("denied" in str(event).lower() for event in events)


# Param-model aliases: tests build args exactly as the registry validates them.
DownloadProbe = DownloadFileParams
FindUserProbe = FindUserParams
ListChannelsProbe = ListChannelsParams
ReadMessagesProbe = ReadMessagesParams
ReadThreadProbe = ReadThreadParams
SearchProbe = SearchParams
SendProbe = SendMessageParams
UnreadProbe = ListUnreadParams
MarkReadProbe = MarkReadParams
