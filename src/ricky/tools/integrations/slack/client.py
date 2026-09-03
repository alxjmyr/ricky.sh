"""Async Slack Web API client — the only module in the package that touches the wire.

Conventions implemented here:

- ``Authorization: Bearer <user token>``; the ``SecretStr`` is unwrapped in
  exactly one place (`_http`) and must never reach results, events, or errors.
- Slack signals failure as HTTP 200 with ``{"ok": false, "error": code}``;
  that envelope maps to :class:`SlackApiError` with a model-readable hint.
- HTTP 429 honors ``Retry-After`` with a bounded retry — for read methods
  only. A send (``retryable=False``) is never retried once the request has
  been sent: a timeout after send is ambiguous (the message may have posted),
  which is the idempotency boundary.
- Cursor pagination follows ``response_metadata.next_cursor`` up to a hard
  page cap; hitting the cap is reported, never silent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import SecretStr

from ricky.attachments import LoadedAttachment
from ricky.tools.integrations.slack.types import (
    KIND_API_TYPES,
    ChannelKind,
    SlackChannel,
    SlackReadState,
    SlackUser,
)

ALL_CHANNEL_KINDS: tuple[ChannelKind, ...] = ("public", "private", "im", "mpim")

READ_RETRY_LIMIT = 2
MAX_RETRY_AFTER_SECONDS = 30.0
PAGE_CAP = 5
PAGE_SIZE = 200
# Slack treats ``limit`` as a maximum, not a target: single-type
# ``conversations.list`` pages measure 150-200 items, but a multi-type query
# collapses to 13-60. The directory therefore needs a page budget, not an item
# budget, and each kind gets its own. Extra pages cost nothing when a directory
# is small — pagination stops at the last page.
DIRECTORY_PAGE_CAP = 25
# Slack's "never read" sentinel on conversations.info.
NEVER_READ = "0000000000.000000"
# Message subtypes that are workspace noise, not something to catch up on.
_UNREAD_IGNORED_SUBTYPES = frozenset(
    {
        "channel_join",
        "channel_leave",
        "group_join",
        "group_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "channel_archive",
        "channel_unarchive",
    }
)

_ERROR_HINTS = {
    "invalid_auth": ("token rejected; check slack_user_token in the owning profile"),
    "token_revoked": ("token revoked; reinstall the app and update the owning profile"),
    "missing_scope": (
        "the token lacks a required user scope; see .designs/assets/slack-app-manifest.yaml, "
        "add the scope under User Token Scopes, and reinstall the app"
    ),
    "channel_not_found": "no such conversation id, or you are not a member",
    "not_in_channel": "you are not a member of this conversation",
    "thread_not_found": "no thread with that thread_ts in this conversation",
    "ratelimited": "rate limited by Slack; try again shortly",
}

# Method-specific overrides. The generic ``missing_scope`` hint cannot name the
# scope that is actually absent; for conversations.mark it can.
_METHOD_ERROR_HINTS = {
    ("conversations.mark", "missing_scope"): (
        "marking a conversation read needs the channels:write, groups:write, im:write, "
        "and mpim:write user scopes; add them from .designs/assets/slack-app-manifest.yaml, "
        "reinstall the app, and put the new token in the owning profile's .secrets.toml"
    ),
}


class SlackError(Exception):
    """Base Slack integration failure; the message is safe to show the model."""


class SlackApiError(SlackError):
    """Slack API envelope failure (``ok: false``)."""

    def __init__(self, method: str, code: str) -> None:
        self.method = method
        self.code = code
        hint = _METHOD_ERROR_HINTS.get((method, code)) or _ERROR_HINTS.get(code)
        detail = f" ({hint})" if hint else ""
        super().__init__(f"Slack {method} failed: {code}{detail}")


class SlackTransportError(SlackError):
    """Network-level failure on a read call."""


class SlackSendUnknownError(SlackError):
    """A send failed mid-flight; delivery is unknown and retry is not safe."""

    def __init__(self) -> None:
        super().__init__(
            "send status unknown: the request failed after it may have reached Slack. "
            "Check Slack for the message before retrying."
        )


class SlackClient:
    """Thin async wrapper over the Slack Web API with directory caches."""

    def __init__(
        self,
        *,
        token: SecretStr,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport  # tests inject httpx.MockTransport here
        self._client: httpx.AsyncClient | None = None
        self._users_cache: dict[str, SlackUser] | None = None
        self._users_truncated = False
        self._channels_cache: dict[ChannelKind, list[SlackChannel]] = {}
        self._channels_truncated: dict[ChannelKind, bool] = {}
        self._own_user_id: str | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            try:
                self._client = httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=self._timeout,
                    headers={"Authorization": f"Bearer {self._token.get_secret_value()}"},
                    transport=self._transport,
                )
            except httpx.InvalidURL as exc:
                raise SlackError(f"invalid slack.api_base_url {self._base_url!r}: {exc}") from exc
        return self._client

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        json_body: dict[str, Any] | None = None,
        retryable: bool = True,
    ) -> dict[str, Any]:
        """POST one Web API method and return the ``ok: true`` payload."""
        attempts = 0
        while True:
            try:
                if json_body is not None:
                    response = await self._http().post(f"/{method}", json=json_body)
                else:
                    response = await self._http().post(f"/{method}", data=params or {})
            except httpx.HTTPError as exc:
                if not retryable:
                    raise SlackSendUnknownError() from exc
                raise SlackTransportError(f"Slack {method} failed: {exc}") from exc

            if response.status_code == 429 and retryable and attempts < READ_RETRY_LIMIT:
                attempts += 1
                delay = _retry_after_seconds(response)
                await asyncio.sleep(delay)
                continue

            if not retryable and response.status_code >= 500:
                raise SlackSendUnknownError()

            try:
                payload = response.json()
            except ValueError as exc:
                if not retryable:
                    raise SlackSendUnknownError() from exc
                raise SlackTransportError(
                    f"Slack {method} returned a non-JSON response (HTTP {response.status_code})"
                ) from exc
            if not payload.get("ok"):
                raise SlackApiError(method, str(payload.get("error") or "unknown_error"))
            return payload

    async def call_paginated(
        self,
        method: str,
        params: dict[str, Any],
        *,
        items_key: str,
        page_cap: int = PAGE_CAP,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Follow cursor pagination; returns (items, truncated_by_page_cap)."""
        items: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(page_cap):
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            payload = await self.call(method, page_params)
            page_items = payload.get(items_key) or []
            items.extend(page_items)
            metadata = payload.get("response_metadata") or {}
            cursor = str(metadata.get("next_cursor") or "")
            if not cursor:
                return items, False
        return items, True

    async def download(self, url_private: str) -> bytes:
        """Fetch an attachment via its authenticated ``url_private`` URL."""
        hostname = (urlparse(url_private).hostname or "").lower()
        if hostname != "slack.com" and not hostname.endswith(".slack.com"):
            shown_host = hostname or "[missing host]"
            raise SlackError(
                f"refusing Slack file download from non-Slack host {shown_host!r}; "
                "share the external file link instead"
            )
        try:
            response = await self._http().get(url_private)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SlackTransportError(f"Slack file download failed: {exc}") from exc
        return response.content

    async def upload_external(self, url: str, attachment: LoadedAttachment) -> None:
        """Upload bytes to a Slack-issued URL without leaking the user token."""

        hostname = (urlparse(url).hostname or "").lower()
        if hostname != "slack.com" and not hostname.endswith(".slack.com"):
            shown_host = hostname or "[missing host]"
            raise SlackError(f"refusing Slack file upload to non-Slack host {shown_host!r}")
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    url,
                    files={
                        "file": (
                            attachment.filename,
                            attachment.content,
                            attachment.media_type,
                        )
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SlackTransportError(f"Slack file upload failed: {exc}") from exc

    # --- Directory caches (toolset lifetime) ------------------------------

    async def users(self) -> tuple[dict[str, SlackUser], bool]:
        """The workspace user directory and its truncation flag (cached)."""
        if self._users_cache is None:
            raw, truncated = await self.call_paginated(
                "users.list", {"limit": PAGE_SIZE}, items_key="members", page_cap=PAGE_CAP * 2
            )
            users = (SlackUser.from_api(member) for member in raw)
            self._users_cache = {user.id: user for user in users if user.id}
            self._users_truncated = truncated
        return self._users_cache, self._users_truncated

    async def channels(
        self, kinds: Sequence[ChannelKind] | None = None
    ) -> tuple[list[SlackChannel], dict[ChannelKind, bool]]:
        """Conversations of the requested kinds, plus a per-kind truncation map.

        Each kind is fetched and cached separately so that one crowded kind
        cannot consume another's page budget: in a workspace with more public
        channels than the cap allows, a shared fetch returns zero DMs.
        """
        requested: Sequence[ChannelKind] = kinds if kinds is not None else ALL_CHANNEL_KINDS
        wanted: list[ChannelKind] = list(dict.fromkeys(requested))
        missing: list[ChannelKind] = [kind for kind in wanted if kind not in self._channels_cache]
        if missing:
            fetched = await asyncio.gather(*(self._fetch_kind(kind) for kind in missing))
            for kind, (items, truncated) in zip(missing, fetched, strict=True):
                self._channels_cache[kind] = items
                self._channels_truncated[kind] = truncated
        channels = [channel for kind in wanted for channel in self._channels_cache[kind]]
        return channels, {kind: self._channels_truncated[kind] for kind in wanted}

    async def _fetch_kind(self, kind: ChannelKind) -> tuple[list[SlackChannel], bool]:
        """One ``conversations.list`` pass over a single conversation kind."""
        raw, truncated = await self.call_paginated(
            "conversations.list",
            {
                "types": KIND_API_TYPES[kind],
                "exclude_archived": "true",
                "limit": PAGE_SIZE,
            },
            items_key="channels",
            page_cap=DIRECTORY_PAGE_CAP,
        )
        parsed = (SlackChannel.from_api(item) for item in raw)
        return [channel for channel in parsed if channel.id and channel.kind == kind], truncated

    # --- Read state (never cached) ----------------------------------------

    async def whoami(self) -> str:
        """The authenticated user's id, cached for the toolset's lifetime.

        Needed to exclude the user's own messages from an unread count: Slack
        does not do that for us when we count from history.
        """
        if self._own_user_id is None:
            payload = await self.call("auth.test")
            self._own_user_id = str(payload.get("user_id") or "")
        return self._own_user_id

    async def read_state(self, channel_id: str, *, probe_limit: int) -> SlackReadState:
        """One conversation's read position.

        Slack is inconsistent here, so this method covers both documented
        shapes. ``conversations.info`` returns ``unread_count_display`` for
        DMs, which is authoritative and free. For every other kind it returns
        only ``last_read``, so the count comes from one bounded
        ``conversations.history`` pass. Nothing is cached: a read position
        that is one call stale is worse than no read position at all.
        """
        payload = await self.call("conversations.info", {"channel": channel_id})
        info = payload.get("channel") or {}
        last_read = str(info.get("last_read") or "")
        latest = info.get("latest")
        latest_ts = str(latest.get("ts") or "") if isinstance(latest, dict) else ""

        if "unread_count_display" in info:
            return SlackReadState(
                channel_id=channel_id,
                last_read=last_read,
                unread_count=int(info.get("unread_count_display") or 0),
                latest_ts=latest_ts,
            )

        return await self._count_unread(channel_id, last_read, probe_limit)

    async def _count_unread(
        self, channel_id: str, last_read: str, probe_limit: int
    ) -> SlackReadState:
        """Path B: count messages after ``last_read`` from history."""
        request: dict[str, Any] = {"channel": channel_id, "limit": probe_limit}
        if last_read and last_read != NEVER_READ:
            # ``oldest`` is exclusive of nothing, so last_read itself comes
            # back; it is filtered out below by the strict ts comparison.
            request["oldest"] = last_read
        payload = await self.call("conversations.history", request)
        messages = payload.get("messages") or []

        own_id = await self.whoami()
        boundary = _ts_value(last_read)
        unread = 0
        newest = 0.0
        newest_ts = ""
        for message in messages:
            ts = str(message.get("ts") or "")
            value = _ts_value(ts)
            if value > newest:
                newest, newest_ts = value, ts
            if value <= boundary:
                continue
            if str(message.get("user") or "") == own_id:
                continue
            if str(message.get("subtype") or "") in _UNREAD_IGNORED_SUBTYPES:
                continue
            unread += 1
        return SlackReadState(
            channel_id=channel_id,
            last_read=last_read,
            unread_count=unread,
            latest_ts=newest_ts,
        )

    async def latest_ts(self, channel_id: str) -> str:
        """The newest message's ts, or "" when the conversation is empty."""
        payload = await self.call("conversations.history", {"channel": channel_id, "limit": 1})
        messages = payload.get("messages") or []
        return str(messages[0].get("ts") or "") if messages else ""

    async def mark_read(self, channel_id: str, ts: str) -> None:
        """Move the read cursor to ``ts``.

        Retryable despite being a mutation: ``conversations.mark`` is
        idempotent and monotonic, so the same ``(channel, ts)`` pair always
        produces the same end state.
        """
        await self.call("conversations.mark", {"channel": channel_id, "ts": ts})

    def invalidate_channels(self) -> None:
        """Drop every conversations cache."""
        self._channels_cache.clear()
        self._channels_truncated.clear()

    def invalidate_channel_kind(self, kind: ChannelKind) -> None:
        """Drop one kind's cache (e.g. ``im`` after opening a new DM)."""
        self._channels_cache.pop(kind, None)
        self._channels_truncated.pop(kind, None)


def _ts_value(ts: str) -> float:
    """A Slack ts as a float; an unparsable or absent ts sorts oldest."""
    try:
        return float(ts)
    except ValueError:
        return 0.0


def _retry_after_seconds(response: httpx.Response) -> float:
    try:
        delay = float(response.headers.get("Retry-After", "1"))
    except ValueError:
        delay = 1.0
    return max(0.0, min(delay, MAX_RETRY_AFTER_SECONDS))
