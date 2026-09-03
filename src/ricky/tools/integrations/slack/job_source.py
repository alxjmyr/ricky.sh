"""Slack channel message/thread stream adapter for recurring jobs."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ricky.jobs.sources import CollectedBatch, SourceItem
from ricky.tools.integrations.slack.client import SlackClient
from ricky.tools.integrations.slack.types import SlackMessage


class SlackChannelSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(pattern=r"^[CDG][A-Z0-9]+$", max_length=100)
    initial_lookback_hours: float = Field(gt=0, le=8_760)
    item_limit: int = Field(ge=1, le=500)


class SlackChannelJobSource:
    """Collect a stable exclusive-oldest/inclusive-upper Slack channel window."""

    name = "slack_channel"
    Config = SlackChannelSourceConfig

    def __init__(self, client: SlackClient, *, text_limit: int = 12_000) -> None:
        self._client = client
        self._text_limit = text_limit

    async def collect(
        self,
        config: BaseModel,
        *,
        cursor: JsonValue,
        upper_bound: datetime,
        limit: int,
    ) -> CollectedBatch:
        args = SlackChannelSourceConfig.model_validate(config)
        oldest = _cursor_ts(cursor)
        latest = f"{upper_bound.timestamp():.6f}"
        remaining = min(limit, args.item_limit)
        messages: list[dict[str, object]] = []
        page_cursor = ""
        complete = True
        while remaining > 0:
            request: dict[str, object] = {
                "channel": args.channel_id,
                "oldest": oldest,
                "latest": latest,
                "inclusive": "false",
                "limit": min(remaining, 200),
            }
            if page_cursor:
                request["cursor"] = page_cursor
            payload = await self._client.call("conversations.history", request)
            page = list(payload.get("messages") or [])
            messages.extend(page[:remaining])
            remaining -= min(len(page), remaining)
            page_cursor = str((payload.get("response_metadata") or {}).get("next_cursor") or "")
            has_more = bool(payload.get("has_more") or page_cursor)
            if not has_more:
                break
            if remaining == 0:
                complete = False
                break

        items = [self._item(args.channel_id, raw) for raw in messages]
        items.sort(key=lambda item: (item.occurred_at, item.id))
        return CollectedBatch(
            items=items,
            input_cursor=cursor,
            next_cursor={"ts": latest},
            upper_bound=upper_bound,
            complete=complete,
        )

    def _item(self, channel_id: str, raw: dict[str, object]) -> SourceItem:
        message = SlackMessage.from_api(raw, channel_id=channel_id)
        text = message.text.strip() or "[Slack message has no text]"
        if len(text) > self._text_limit:
            text = f"{text[: self._text_limit - 12]}\n[truncated]"
        occurred = datetime.fromtimestamp(float(message.ts), UTC)
        identity = f"{channel_id}:{message.thread_ts or message.ts}:{message.ts}"
        return SourceItem(
            id=identity,
            text=text,
            occurred_at=occurred,
            data={
                "channel_id": channel_id,
                "ts": message.ts,
                "thread_ts": message.thread_ts or None,
                "user_id": message.user_id,
                "reply_count": message.reply_count,
            },
        )


def _cursor_ts(cursor: JsonValue) -> str:
    if not isinstance(cursor, dict):
        raise ValueError("Slack stream cursor must be an object with string ts")
    value = cursor.get("ts")
    if not isinstance(value, str):
        raise ValueError("Slack stream cursor must be an object with string ts")
    try:
        float(value)
    except ValueError as exc:
        raise ValueError("Slack stream cursor ts must be numeric") from exc
    return value
