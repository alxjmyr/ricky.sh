"""Slack integration toolpack.

The registered tools are the only exported surface; ``client``/``types``/
``resolve`` stay package-internal; see the integration boundary in
``.designs/architecture.md``.
"""

from __future__ import annotations

from ricky.config import RickySettings
from ricky.tools.base import Tool
from ricky.tools.integrations.slack.client import SlackClient, SlackError
from ricky.tools.integrations.slack.job_source import SlackChannelJobSource
from ricky.tools.integrations.slack.tools import (
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


class SlackToolset:
    """The Slack tools plus the shared client's lifecycle."""

    def __init__(self, settings: RickySettings) -> None:
        token = settings.slack_user_token
        if token is None:
            raise ValueError(
                "slack_user_token is not configured in the owning profile or environment"
            )
        self._client = SlackClient(
            token=token,
            base_url=settings.slack.api_base_url,
            timeout_seconds=settings.request_timeout_seconds,
        )
        self.tools: list[Tool] = [
            SlackListChannelsTool(self._client),
            SlackListUnreadTool(self._client),
            SlackFindUserTool(self._client),
            SlackSearchTool(self._client),
            SlackReadMessagesTool(self._client),
            SlackReadThreadTool(self._client),
            SlackSendMessageTool(self._client),
            SlackMarkReadTool(self._client),
            SlackDownloadFileTool(self._client),
        ]
        self.job_stream_adapters = [
            SlackChannelJobSource(self._client, text_limit=settings.jobs.source_item_text_limit)
        ]

    async def aclose(self) -> None:
        """Close the shared HTTP client."""
        await self._client.aclose()

    async def check_auth(self) -> tuple[str, str]:
        """Verify the token via ``auth.test``; returns (user, team)."""
        payload = await self._client.call("auth.test")
        return str(payload.get("user") or ""), str(payload.get("team") or "")


def slack_toolset(settings: RickySettings) -> SlackToolset | None:
    """Build the toolset, or None when no user token is configured."""
    if settings.slack_user_token is None:
        return None
    return SlackToolset(settings)


__all__ = ["SlackError", "SlackToolset", "slack_toolset"]
