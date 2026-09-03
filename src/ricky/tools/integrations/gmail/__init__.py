"""Gmail integration toolpack over the shared Google transport."""

from __future__ import annotations

from pathlib import Path

from ricky.config import RickySettings
from ricky.tools.base import Tool
from ricky.tools.integrations.gmail.client import (
    GmailApiError,
    GmailClient,
    GmailError,
    GmailMutationUnknownError,
    GmailTransportError,
)
from ricky.tools.integrations.gmail.tools import (
    GmailCreateDraftTool,
    GmailCreateLabelTool,
    GmailDownloadAttachmentTool,
    GmailListDraftsTool,
    GmailListLabelsTool,
    GmailModifyLabelsTool,
    GmailReadMessageTool,
    GmailReadThreadTool,
    GmailSearchTool,
    GmailSendMessageTool,
    GmailTrashTool,
)
from ricky.tools.integrations.google import GoogleAuth
from ricky.tools.integrations.google.scopes import GMAIL_SCOPES
from ricky.tools.integrations.google.toolset import (
    GoogleServiceToolset,
    google_accounts_available,
)


class GmailToolset(GoogleServiceToolset):
    """The Gmail tools plus shared client/auth lifecycle."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        root: Path | None = None,
        auth: GoogleAuth | None = None,
    ) -> None:
        super().__init__(settings, scopes=GMAIL_SCOPES, root=root, auth=auth)
        self._client = GmailClient(
            auth=self._auth,
            base_url=settings.gmail.api_base_url,
            timeout_seconds=settings.request_timeout_seconds,
        )
        self.tools: list[Tool] = [
            GmailSearchTool(self._client),
            GmailReadMessageTool(self._client),
            GmailReadThreadTool(self._client),
            GmailListLabelsTool(self._client),
            GmailListDraftsTool(self._client),
            GmailCreateDraftTool(self._client),
            GmailSendMessageTool(self._client),
            GmailCreateLabelTool(self._client),
            GmailModifyLabelsTool(self._client),
            GmailTrashTool(self._client),
            GmailDownloadAttachmentTool(self._client),
        ]

    async def check_account(self, account: str) -> tuple[str, int]:
        """Return authenticated email and total message count from users/me/profile."""
        payload = await self._client.call(account, "GET", "profile")
        return (
            str(payload.get("emailAddress") or ""),
            int(payload.get("messagesTotal") or 0),
        )

    async def aclose(self) -> None:
        """Close Gmail and owned shared-auth clients."""
        await self._client.aclose()
        await super().aclose()


def gmail_toolset(
    settings: RickySettings,
    *,
    root: Path | None = None,
    auth: GoogleAuth | None = None,
) -> GmailToolset | None:
    """Build Gmail tools when at least one account has matching OAuth credentials."""
    if not google_accounts_available(settings):
        return None
    return GmailToolset(settings, root=root, auth=auth)


__all__ = [
    "GMAIL_SCOPES",
    "GmailApiError",
    "GmailCreateDraftTool",
    "GmailCreateLabelTool",
    "GmailDownloadAttachmentTool",
    "GmailError",
    "GmailListDraftsTool",
    "GmailListLabelsTool",
    "GmailModifyLabelsTool",
    "GmailMutationUnknownError",
    "GmailReadMessageTool",
    "GmailReadThreadTool",
    "GmailSearchTool",
    "GmailSendMessageTool",
    "GmailToolset",
    "GmailTransportError",
    "GmailTrashTool",
    "gmail_toolset",
]
