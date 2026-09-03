"""The Slack tool implementations.

Every tool holds a shared :class:`SlackClient`. Integration failures cross the
tool boundary as :class:`SlackError`; :class:`ToolRegistry` converts them to
model-readable error results. Network reads are ``read_only``; sends and local
downloads are ``mutating`` and pass through the permission gate.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from ricky.attachments import (
    TASK_ARTIFACT_ATTACHMENT_HELP,
    AttachmentArgument,
    LoadedAttachment,
    PreparedAttachmentEffect,
    attachment_source_label,
    load_attachments,
)
from ricky.config import user_data_subpath
from ricky.tools.base import (
    EffectIdentity,
    EffectReceipt,
    PreparedEffect,
    Risk,
    ToolContext,
    ToolResult,
    make_effect_identity,
)
from ricky.tools.integrations.slack.client import SlackClient, SlackError
from ricky.tools.integrations.slack.render import (
    channel_label,
    format_ts,
    render_channel,
    render_message,
    render_messages,
    render_user,
)
from ricky.tools.integrations.slack.resolve import (
    USER_ID,
    match_users,
    resolve_channel,
    resolve_user,
)
from ricky.tools.integrations.slack.types import (
    ChannelKind,
    SlackChannel,
    SlackMessage,
    SlackReadState,
    SlackUser,
)

_TS = re.compile(r"\d+\.\d+")

CHANNEL_ARG_HELP = (
    "Channel name (with or without #), conversation id (C…/G…/D…), or @user for that user's DM."
)


def _to_epoch(value: str) -> str:
    """Accept a raw Slack ts or an ISO date/datetime; return epoch seconds."""
    value = value.strip()
    if _TS.fullmatch(value):
        return value
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return f"{moment.timestamp():.6f}"


class ListChannelsParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kinds: list[ChannelKind] = Field(
        default=["public", "private"],
        description=(
            "Conversation kinds to include: public, private, im (DMs), mpim (group DMs). "
            "Defaults to channels only — pass ['im', 'mpim'] to list DMs."
        ),
    )
    name_filter: str | None = Field(
        default=None,
        description="Case-insensitive substring filter on channel or DM counterpart name.",
    )
    limit: int = Field(default=50, ge=1, le=200, description="Maximum entries to return.")


class _SlackReadTool:
    capability_id = "builtin.chat.read"
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None


class _SlackMutationTool:
    name: ClassVar[str]
    Params: ClassVar[type[BaseModel]]

    capability_id = "builtin.chat.mutate"
    effect_kind = "external"
    unattended = "allowed"
    state_guard_id = None

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        del ctx
        parsed = self.Params.model_validate(args)
        encoded = json.dumps(parsed.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        occurrence = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return make_effect_identity(
            operation=self.name,
            target="slack-workspace",
            occurrence=occurrence,
            summary=f"Run {self.name} in Slack",
        )


class SlackListChannelsTool(_SlackReadTool):
    name: ClassVar[str] = "slack_list_channels"
    description: ClassVar[str] = (
        "List Slack conversations (channels, DMs, group DMs) with their ids. "
        "Use this to find a conversation id before reading or sending. "
        "Only channels are listed by default; set kinds=['im','mpim'] for DMs. "
        "For unread or 'what is new' questions use slack_list_unread instead."
    )
    Params: ClassVar[type[BaseModel]] = ListChannelsParams
    risk: ClassVar[Risk] = "read_only"

    def __init__(self, client: SlackClient) -> None:
        self._client = client

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = ListChannelsParams.model_validate(params)
        kinds = list(dict.fromkeys(args.kinds))
        channels, truncated = await self._client.channels(kinds)
        users, _ = await self._client.users()
        label = ", ".join(kinds)

        selected = channels
        if args.name_filter:
            needle = args.name_filter.lower()
            selected = [c for c in channels if _channel_matches(c, users, needle)]
        shown = selected[: args.limit]

        lines: list[str] = []
        if not selected and args.name_filter:
            lines.append(
                f"no conversations match {args.name_filter!r} "
                f"among {len(channels)} fetched [{label}]"
            )
        elif not selected:
            lines.append(f"no {label} conversations found")
        else:
            matched = (
                f"{len(selected)} of {len(channels)} conversation(s) [{label}] "
                f"match {args.name_filter!r}"
                if args.name_filter
                else f"{len(channels)} conversation(s) [{label}]"
            )
            lines.append(f"{matched}; showing {len(shown)}:")
            lines.extend(render_channel(channel, users) for channel in shown)
            if len(selected) > len(shown):
                lines.append(f"[{len(selected) - len(shown)} more matched; raise limit or filter]")
        lines.extend(_truncation_notices(channels, truncated))
        return ToolResult(content="\n".join(lines))


class ListUnreadParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kinds: list[ChannelKind] = Field(
        default=["im", "mpim"],
        description=(
            "Conversation kinds to check: public, private, im (DMs), mpim (group DMs). "
            "Defaults to DMs and group DMs — channels are noisy."
        ),
    )
    limit: int = Field(default=20, ge=1, le=50, description="Maximum conversations to report.")
    include_preview: bool = Field(
        default=True, description="Show the newest unread message of each conversation."
    )


class SlackListUnreadTool(_SlackReadTool):
    name: ClassVar[str] = "slack_list_unread"
    description: ClassVar[str] = (
        "List Slack conversations with unread messages, newest first. This is "
        "the correct tool for 'do I have unread DMs' or 'what is new in Slack' "
        "— a search cannot tell read messages from unread ones. Checks DMs and "
        "group DMs by default; pass kinds for channels."
    )
    Params: ClassVar[type[BaseModel]] = ListUnreadParams
    risk: ClassVar[Risk] = "read_only"

    def __init__(self, client: SlackClient) -> None:
        self._client = client

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = ListUnreadParams.model_validate(params)
        kinds = list(dict.fromkeys(args.kinds))
        channels, truncated = await self._client.channels(kinds)
        users, _ = await self._client.users()
        label = ", ".join(kinds)

        # The probe costs one or two calls per conversation, so bound it and
        # spend the budget on the conversations most likely to be active.
        ordered = sorted(channels, key=lambda c: c.updated_ms, reverse=True)
        probed = ordered[: ctx.settings.slack.unread_max_conversations]

        states, failures = await self._probe(probed, ctx.settings.slack.unread_probe_limit)
        unread = [(channel, state) for channel, state in states if state.has_unread]
        unread.sort(key=lambda pair: _ts_key(pair[1].latest_ts), reverse=True)
        shown = unread[: args.limit]

        lines = [
            f"{len(unread)} conversation(s) with unread messages "
            f"out of {len(probed)} checked [{label}]"
            + (f"; showing {len(shown)}:" if shown else ".")
        ]
        for channel, state in shown:
            lines.append(_unread_line(channel, state, users))
        if args.include_preview and shown:
            lines.extend(await self._previews(shown, users))
        if len(unread) > len(shown):
            lines.append(f"[{len(unread) - len(shown)} more unread; raise limit]")
        if len(ordered) > len(probed):
            lines.append(
                f"[checked the {len(probed)} most recently active of {len(ordered)} "
                f"conversation(s); {len(ordered) - len(probed)} were not checked]"
            )
        if failures:
            lines.append(f"[could not check {len(failures)}: {', '.join(failures[:5])}]")
        lines.extend(_truncation_notices(channels, truncated))
        return ToolResult(content="\n".join(lines))

    async def _probe(
        self, channels: list[SlackChannel], probe_limit: int
    ) -> tuple[list[tuple[SlackChannel, SlackReadState]], list[str]]:
        """Fetch read state concurrently; one failure never fails the tool."""
        semaphore = asyncio.Semaphore(8)
        states: list[tuple[SlackChannel, SlackReadState]] = []
        failures: list[str] = []

        async def one(channel: SlackChannel) -> None:
            async with semaphore:
                try:
                    state = await self._client.read_state(channel.id, probe_limit=probe_limit)
                except SlackError as exc:
                    failures.append(f"{channel.id} ({exc})")
                    return
                states.append((channel, state))

        await asyncio.gather(*(one(channel) for channel in channels))
        return states, failures

    async def _previews(
        self,
        shown: list[tuple[SlackChannel, SlackReadState]],
        users: dict[str, SlackUser],
    ) -> list[str]:
        """The newest message of each reported conversation, best effort."""
        semaphore = asyncio.Semaphore(8)

        async def one(channel: SlackChannel, state: SlackReadState) -> str:
            async with semaphore:
                try:
                    payload = await self._client.call(
                        "conversations.history", {"channel": channel.id, "limit": 1}
                    )
                except SlackError:
                    return ""
            raw = payload.get("messages") or []
            if not raw:
                return ""
            message = SlackMessage.from_api(raw[0], channel_id=channel.id)
            message.channel_label = channel_label(channel, users)
            return render_message(message, users)

        rendered = await asyncio.gather(*(one(c, s) for c, s in shown))
        previews = [text for text in rendered if text]
        return ["", "Newest unread message per conversation:", *previews] if previews else []


class MarkReadParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    channel: str = Field(description=CHANNEL_ARG_HELP)
    ts: str = Field(
        default="",
        description="Mark read up to this message ts. Empty marks the whole conversation read.",
    )


class SlackMarkReadTool(_SlackMutationTool):
    name: ClassVar[str] = "slack_mark_read"
    description: ClassVar[str] = (
        "Mark a Slack conversation read up to a message, as the user. Clears "
        "the unread badge in the user's own Slack client, so confirm with them "
        "first; Ricky cannot mark a conversation unread again."
    )
    Params: ClassVar[type[BaseModel]] = MarkReadParams
    risk: ClassVar[Risk] = "mutating"

    def __init__(self, client: SlackClient) -> None:
        self._client = client

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        """Name the conversation and the cut-off before the cursor moves."""
        ts = str(args.get("ts", "") or "")
        where = str(args.get("channel", ""))
        upto = f"up to message {ts}" if ts else "up to its newest message"
        return (
            f"mark {where} read {upto}. This clears the unread badge in your Slack "
            "client and cannot be undone from Ricky."
        )

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        del ctx
        parsed = MarkReadParams.model_validate(args)
        target = parsed.channel.strip().lower()
        # An empty ts means "the newest message", which is not a fixed point;
        # name it explicitly so a replay is never mistaken for a pinned ts.
        occurrence = parsed.ts.strip() or "latest"
        encoded = json.dumps(
            {"operation": "slack.mark_read", "target": target, "ts": occurrence},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return EffectIdentity(
            operation="slack.mark_read",
            target=target,
            occurrence=occurrence,
            summary=f"Mark {parsed.channel} read up to {occurrence}",
            action_key=hashlib.sha256(encoded).hexdigest(),
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = MarkReadParams.model_validate(params)
        # Exact-only: marking the wrong conversation hides messages the user
        # has never seen, and Slack offers no way to undo it for a user token.
        channel = await resolve_channel(self._client, args.channel, exact_only=True)
        users, _ = await self._client.users()
        label = channel_label(channel, users)

        ts = args.ts.strip()
        if not ts:
            ts = await self._client.latest_ts(channel.id)
            if not ts:
                return ToolResult(content=f"{label} has no messages to mark read", is_error=True)

        await self._client.mark_read(channel.id, ts)
        return ToolResult(
            content=(
                f"Marked {label} read up to ts {ts}. "
                "Ricky cannot mark it unread again — Slack has no such API for a user token."
            ),
            effect_receipt=EffectReceipt(disposition="performed", provider_reference=ts),
        )


class FindUserParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str = Field(description="Name, @display-name, or email address to look up.")


class SlackFindUserTool(_SlackReadTool):
    name: ClassVar[str] = "slack_find_user"
    description: ClassVar[str] = (
        "Find Slack users by name or email and return their ids. Use the id "
        "(U…) for DM targets and <@U…> for mentions in outgoing messages."
    )
    Params: ClassVar[type[BaseModel]] = FindUserParams
    risk: ClassVar[Risk] = "read_only"

    def __init__(self, client: SlackClient) -> None:
        self._client = client

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = FindUserParams.model_validate(params)
        if not args.query.removeprefix("@").strip():
            return ToolResult(content="empty query", is_error=True)
        users, truncated = await self._client.users()
        matches = match_users(users.values(), args.query)
        if not matches:
            notice = (
                f"; user directory truncated at {len(users)} entries — the user may exist "
                "beyond the cap; retry with an id or email"
                if truncated
                else ""
            )
            return ToolResult(
                content=f"no users matching {args.query!r}{notice}",
                is_error=True,
            )
        lines = [render_user(user) for user in matches[:10]]
        if len(matches) > 10:
            lines.append(f"[{len(matches) - 10} more matched; refine the query]")
        if truncated:
            lines.append(
                f"[user directory truncated at {len(users)} entries; additional matches may exist]"
            )
        return ToolResult(content="\n".join(lines))


class SearchParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str = Field(
        description=(
            "Slack search syntax, e.g. 'retry fix in:#eng from:@dana after:2026-07-01'. "
            "Searches all conversations you can see."
        )
    )
    count: int = Field(default=20, ge=1, le=100, description="Maximum results.")
    sort: Literal["score", "timestamp"] = Field(
        default="timestamp", description="Order by relevance (score) or recency (timestamp)."
    )


class SlackSearchTool(_SlackReadTool):
    name: ClassVar[str] = "slack_search"
    description: ClassVar[str] = (
        "Search Slack messages workspace-wide with Slack's query syntax. Best "
        "for query-shaped asks; use slack_read_messages for 'recent messages "
        "in one place' and slack_list_unread for 'what is new' — search cannot "
        "tell read messages from unread ones. Results include ts values usable "
        "with slack_read_thread."
    )
    Params: ClassVar[type[BaseModel]] = SearchParams
    risk: ClassVar[Risk] = "read_only"

    def __init__(self, client: SlackClient) -> None:
        self._client = client

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = SearchParams.model_validate(params)
        payload = await self._client.call(
            "search.messages",
            {
                "query": args.query,
                "count": args.count,
                "sort": args.sort,
                "sort_dir": "desc",
            },
        )
        users, _ = await self._client.users()
        block = payload.get("messages") or {}
        matches = [SlackMessage.from_search_api(item) for item in block.get("matches") or []]
        total = int((block.get("total") or len(matches)) or 0)
        heading = f"{total} match(es) for {args.query!r}; showing {len(matches)}:"
        return ToolResult(content=render_messages(matches, users, heading=heading))


class ReadMessagesParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    channel: str = Field(description=CHANNEL_ARG_HELP)
    limit: int = Field(default=0, ge=0, le=200, description="Messages to fetch (0 = default).")
    oldest: str | None = Field(
        default=None, description="Only messages after this ISO date/datetime or raw ts."
    )
    latest: str | None = Field(
        default=None, description="Only messages before this ISO date/datetime or raw ts."
    )


class SlackReadMessagesTool(_SlackReadTool):
    name: ClassVar[str] = "slack_read_messages"
    description: ClassVar[str] = (
        "Read recent messages in one Slack conversation (channel, DM, or group "
        "DM), oldest first. Messages marked 'thread: N replies' have replies — "
        "fetch them with slack_read_thread using the shown ts."
    )
    Params: ClassVar[type[BaseModel]] = ReadMessagesParams
    risk: ClassVar[Risk] = "read_only"

    def __init__(self, client: SlackClient) -> None:
        self._client = client

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = ReadMessagesParams.model_validate(params)
        limit = args.limit or ctx.settings.slack.default_history_limit
        channel = await resolve_channel(self._client, args.channel)
        request: dict[str, object] = {"channel": channel.id, "limit": limit}
        try:
            if args.oldest is not None:
                request["oldest"] = _to_epoch(args.oldest)
            if args.latest is not None:
                request["latest"] = _to_epoch(args.latest)
        except ValueError as exc:
            raise SlackError(f"invalid oldest/latest value: {exc}") from exc
        payload = await self._client.call("conversations.history", request)
        users, _ = await self._client.users()
        messages = [
            SlackMessage.from_api(item, channel_id=channel.id)
            for item in payload.get("messages") or []
        ]
        label = f"#{channel.name}" if channel.name else channel.id
        return ToolResult(
            content=render_messages(
                messages,
                users,
                heading=f"Messages in {label}:",
                truncated=bool(payload.get("has_more")),
            )
        )


class ReadThreadParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    channel: str = Field(description=CHANNEL_ARG_HELP)
    thread_ts: str = Field(description="ts of the thread's root message (from a prior read).")
    limit: int = Field(default=50, ge=1, le=200, description="Maximum replies to fetch.")


class SlackReadThreadTool(_SlackReadTool):
    name: ClassVar[str] = "slack_read_thread"
    description: ClassVar[str] = (
        "Read one Slack thread: the root message plus its replies, oldest "
        "first. Get thread_ts from slack_read_messages or slack_search."
    )
    Params: ClassVar[type[BaseModel]] = ReadThreadParams
    risk: ClassVar[Risk] = "read_only"

    def __init__(self, client: SlackClient) -> None:
        self._client = client

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = ReadThreadParams.model_validate(params)
        channel = await resolve_channel(self._client, args.channel)
        payload = await self._client.call(
            "conversations.replies",
            {"channel": channel.id, "ts": args.thread_ts, "limit": args.limit},
        )
        users, _ = await self._client.users()
        messages = [
            SlackMessage.from_api(item, channel_id=channel.id)
            for item in payload.get("messages") or []
        ]
        label = f"#{channel.name}" if channel.name else channel.id
        return ToolResult(
            content=render_messages(
                messages,
                users,
                heading=f"Thread {args.thread_ts} in {label}:",
                truncated=bool(payload.get("has_more")),
            )
        )


class SendMessageParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    target: str = Field(description=f"Where to send: {CHANNEL_ARG_HELP}")
    text: str = Field(
        description=(
            "Message text (Slack mrkdwn). For real @-mentions use <@U…> ids "
            "from slack_find_user; plain @name will not notify."
        )
    )
    thread_ts: str = Field(
        default="",
        description="Reply in this thread (root message ts). Empty sends a root message.",
    )
    attachments: list[AttachmentArgument] = Field(
        default_factory=list,
        description=(
            "Absolute, home-relative, or project-relative host files, or logical "
            "durable-task artifacts, to attach. "
            f"{TASK_ARTIFACT_ATTACHMENT_HELP}"
        ),
    )
    occurrence_id: str = Field(
        default="",
        max_length=500,
        description=(
            "Stable source-item, task-revision, or job-owned occurrence id. "
            "Required only for unattended recurring sends."
        ),
    )


class SlackSendMessageTool(_SlackMutationTool):
    name: ClassVar[str] = "slack_send_message"
    description: ClassVar[str] = (
        "Send a Slack message as the user — a channel/DM root message, or a "
        "thread reply when thread_ts is set — with optional local-file attachments. "
        "Compose and confirm the text and files "
        "with the user first; sending is permission-gated and irreversible."
    )
    Params: ClassVar[type[BaseModel]] = SendMessageParams
    risk: ClassVar[Risk] = "mutating"

    def __init__(self, client: SlackClient) -> None:
        self._client = client

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        """Render the complete outgoing draft exclusively in the review gate."""
        target = str(args.get("target", ""))
        thread = str(args.get("thread_ts", "") or "")
        where = f"to {target}" + (f" in thread {thread}" if thread else "")
        lines = [where, "---", str(args.get("text", "")), "---"]
        raw_attachments = args.get("attachments")
        if isinstance(raw_attachments, list) and raw_attachments:
            lines.append("Attachments:")
            for item in raw_attachments:
                if isinstance(item, dict):
                    source = attachment_source_label(item)
                    lines.append(f"- {item.get('filename') or source} (source: {source})")
        return "\n".join(lines)

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        parsed = SendMessageParams.model_validate(args)
        attachments = tuple(_load_send_attachments(parsed, ctx))
        return self._effect_identity(parsed, attachments)

    def _effect_identity(
        self,
        parsed: SendMessageParams,
        attachments: tuple[LoadedAttachment, ...],
    ) -> EffectIdentity:
        if not parsed.text.strip() and not attachments:
            raise ValueError("refusing to send an empty message")
        occurrence = parsed.occurrence_id.strip()
        if not occurrence:
            raise ValueError("recurring Slack sends require occurrence_id")
        target = parsed.target.strip().lower()
        operation = "slack.thread_reply" if parsed.thread_ts else "slack.root_message"
        encoded = json.dumps(
            {
                "operation": operation,
                "target": target,
                "thread_ts": parsed.thread_ts,
                "occurrence": occurrence,
                "attachment_digests": [attachment.sha256 for attachment in attachments],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return EffectIdentity(
            operation=operation,
            target=target,
            occurrence=occurrence,
            summary=f"Send Slack message to {parsed.target}",
            action_key=hashlib.sha256(encoded).hexdigest(),
        )

    def _prepared_identity(
        self,
        parsed: SendMessageParams,
        attachments: tuple[LoadedAttachment, ...],
    ) -> EffectIdentity:
        """Bind foreground bytes without inventing unattended occurrence authority."""
        if not parsed.text.strip() and not attachments:
            raise ValueError("refusing to send an empty message")
        if parsed.occurrence_id.strip():
            return self._effect_identity(parsed, attachments)
        target = parsed.target.strip().lower()
        operation = "slack.thread_reply" if parsed.thread_ts else "slack.root_message"
        payload = json.dumps(
            {
                "operation": operation,
                "target": target,
                "thread_ts": parsed.thread_ts,
                "text": parsed.text,
                "attachment_digests": [attachment.sha256 for attachment in attachments],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return make_effect_identity(
            operation=operation,
            target=target,
            occurrence=f"foreground:{digest}",
            summary=f"Send Slack message to {parsed.target}",
        )

    async def prepare_effect(
        self, args: dict[str, object], ctx: ToolContext
    ) -> PreparedAttachmentEffect:
        parsed = SendMessageParams.model_validate(args)
        attachments = tuple(await asyncio.to_thread(_load_send_attachments, parsed, ctx))
        preview = self.summarize_permission(parsed.model_dump(mode="python"), ctx)
        if attachments:
            frozen = "\n".join(
                f"- {attachment.filename} "
                f"({attachment.size_bytes} bytes, sha256 {attachment.sha256})"
                for attachment in attachments
            )
            preview = f"{preview}\nPrepared attachments:\n{frozen}"
        return PreparedAttachmentEffect(
            tool_name=self.name,
            identity=self._prepared_identity(parsed, attachments),
            permission_summary=preview,
            attachments=attachments,
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = SendMessageParams.model_validate(params)
        attachments = tuple(await asyncio.to_thread(_load_send_attachments, args, ctx))
        return await self._run_with_attachments(args, attachments)

    async def run_prepared(
        self,
        params: BaseModel,
        prepared: PreparedEffect,
        ctx: ToolContext,
    ) -> ToolResult:
        args = SendMessageParams.model_validate(params)
        if not isinstance(prepared, PreparedAttachmentEffect) or prepared.tool_name != self.name:
            raise ValueError(f"prepared effect does not belong to {self.name}")
        return await self._run_with_attachments(args, prepared.attachments)

    async def _run_with_attachments(
        self,
        args: SendMessageParams,
        attachments: tuple[LoadedAttachment, ...],
    ) -> ToolResult:
        if not args.text.strip() and not attachments:
            return ToolResult(content="refusing to send an empty message", is_error=True)
        channel_id, label = await self._resolve_target(args.target)
        if attachments:
            return await self._send_with_attachments(args, attachments, channel_id, label)
        body: dict[str, object] = {"channel": channel_id, "text": args.text}
        if args.thread_ts:
            body["thread_ts"] = args.thread_ts
        payload = await self._client.call("chat.postMessage", json_body=body, retryable=False)
        ts = str(payload.get("ts") or "")
        suffix = f" in thread {args.thread_ts}" if args.thread_ts else ""
        return ToolResult(
            content=f"Sent to {label}{suffix} (ts {ts}).",
            effect_receipt=EffectReceipt(disposition="performed", provider_reference=ts or None),
        )

    async def _send_with_attachments(
        self,
        args: SendMessageParams,
        attachments: tuple[LoadedAttachment, ...],
        channel_id: str,
        label: str,
    ) -> ToolResult:
        files: list[dict[str, str]] = []
        for attachment in attachments:
            upload = await self._client.call(
                "files.getUploadURLExternal",
                {"filename": attachment.filename, "length": attachment.size_bytes},
            )
            file_id = str(upload.get("file_id") or "")
            upload_url = str(upload.get("upload_url") or "")
            if not file_id or not upload_url:
                raise SlackError("Slack did not return a usable external upload target")
            await self._client.upload_external(upload_url, attachment)
            files.append({"id": file_id, "title": attachment.filename})

        complete: dict[str, object] = {
            "files": files,
            "channel_id": channel_id,
        }
        if args.text.strip():
            complete["initial_comment"] = args.text
        if args.thread_ts:
            complete["thread_ts"] = args.thread_ts
        await self._client.call(
            "files.completeUploadExternal",
            json_body=complete,
            retryable=False,
        )
        suffix = f" in thread {args.thread_ts}" if args.thread_ts else ""
        file_ids = ",".join(item["id"] for item in files)
        return ToolResult(
            content=f"Sent {len(files)} attachment(s) to {label}{suffix}.",
            effect_receipt=EffectReceipt(
                disposition="performed",
                provider_reference=file_ids or None,
            ),
        )

    async def _resolve_target(self, target: str) -> tuple[str, str]:
        """Resolve a send target; opens the DM when targeting a user."""
        stripped = target.strip()
        if stripped.startswith("@") or USER_ID.fullmatch(stripped):
            user = await resolve_user(self._client, stripped, exact_only=True)
            payload = await self._client.call("conversations.open", {"users": user.id})
            self._client.invalidate_channel_kind("im")
            channel_id = str((payload.get("channel") or {}).get("id") or "")
            if not channel_id:
                raise SlackError(f"could not open a DM with {user.label}")
            return channel_id, f"DM with @{user.label}"
        channel = await resolve_channel(self._client, stripped, exact_only=True)
        return channel.id, f"#{channel.name}" if channel.name else channel.id


class DownloadFileParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    file_id: str = Field(
        pattern=r"^F[A-Z0-9]+$",
        description="Slack file id (F…) from a message's attachment line.",
    )


def _load_send_attachments(
    args: SendMessageParams,
    ctx: ToolContext,
) -> list[LoadedAttachment]:
    settings = ctx.settings.slack
    return load_attachments(
        args.attachments,
        cwd=ctx.cwd,
        settings=ctx.settings,
        profile_scope=ctx.session.profile_scope,
        count_limit=settings.attachment_count_limit,
        file_byte_limit=settings.attachment_file_byte_limit,
        total_byte_limit=settings.attachment_total_byte_limit,
    )


class SlackDownloadFileTool(_SlackMutationTool):
    name: ClassVar[str] = "slack_download_file"
    description: ClassVar[str] = (
        "Download a Slack attachment to the local Slack downloads directory "
        "and return its path. Get file ids from message attachment lines. "
        "The local write is permission-gated."
    )
    Params: ClassVar[type[BaseModel]] = DownloadFileParams
    risk: ClassVar[Risk] = "mutating"

    def __init__(self, client: SlackClient) -> None:
        self._client = client

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        parsed = DownloadFileParams.model_validate(args)
        user_data_subpath(ctx.settings, ctx.settings.slack.download_dir)
        return super().effect_identity(parsed.model_dump(mode="python"), ctx)

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        """Show the confined destination before any download or write occurs."""
        file_id = str(args.get("file_id", ""))
        try:
            candidate = user_data_subpath(ctx.settings, ctx.settings.slack.download_dir) / (
                f"{file_id}-<Slack filename>"
            )
            destination = str(candidate)
        except ValueError:
            destination = f"{ctx.settings.slack.download_dir} (invalid: escapes user_data_dir)"
        return f"download Slack file {file_id} to {destination}"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = DownloadFileParams.model_validate(params)
        payload = await self._client.call("files.info", {"file": args.file_id})
        info = payload.get("file") or {}
        url = str(info.get("url_private") or "")
        if not url:
            return ToolResult(
                content=f"file {args.file_id} has no downloadable content", is_error=True
            )
        if info.get("is_external") or str(info.get("mode") or "") == "external":
            raise SlackError(
                f"file {args.file_id} is externally hosted; share its external link instead"
            )
        filename = f"{args.file_id}-{_safe_name(str(info.get('name') or 'file'))}"
        try:
            path = user_data_subpath(ctx.settings, ctx.settings.slack.download_dir) / filename
        except ValueError as exc:
            raise SlackError(f"slack.download_dir escapes user_data_dir: {exc}") from exc
        content = await self._client.download(url)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        path.write_bytes(content)
        path.chmod(0o600)
        return ToolResult(
            content=f"Downloaded to {path} ({len(content)} bytes)",
            effect_receipt=EffectReceipt(disposition="performed", provider_reference=str(path)),
        )


def _truncation_notices(
    channels: list[SlackChannel], truncated: dict[ChannelKind, bool]
) -> list[str]:
    """One notice per kind that hit its page cap — never for a complete kind."""
    notices = []
    for kind, hit_cap in truncated.items():
        if not hit_cap:
            continue
        fetched = sum(1 for channel in channels if channel.kind == kind)
        notices.append(
            f"[{kind} directory truncated at {fetched} entries; "
            f"more {kind} conversations exist beyond the cap]"
        )
    return notices


def _unread_line(channel: SlackChannel, state: SlackReadState, users: dict[str, SlackUser]) -> str:
    """One conversation's unread summary: where, how many, and how recent."""
    count = f"{state.unread_count} unread"
    when = f", newest {format_ts(state.latest_ts)}" if state.latest_ts else ""
    return f"{channel.id}  {channel_label(channel, users)}  ({count}{when})"


def _ts_key(ts: str) -> float:
    try:
        return float(ts)
    except ValueError:
        return 0.0


def _channel_matches(channel: SlackChannel, users: dict[str, SlackUser], needle: str) -> bool:
    if needle in channel.name.lower():
        return True
    if channel.kind != "im":
        return False
    user = users.get(channel.user_id)
    return bool(user and match_users([user], needle))


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "file"
    return cleaned[:120]
