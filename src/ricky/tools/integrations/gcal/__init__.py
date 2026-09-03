"""Google Calendar toolpack over the shared Google transport."""

from __future__ import annotations

from pathlib import Path

from ricky.config import RickySettings
from ricky.tools.base import Tool
from ricky.tools.integrations.gcal.client import (
    GcalApiError,
    GcalClient,
    GcalError,
    GcalMutationUnknownError,
    GcalTransportError,
)
from ricky.tools.integrations.gcal.tools import (
    GcalCheckAvailabilityTool,
    GcalCreateEventTool,
    GcalDeleteEventTool,
    GcalGetEventTool,
    GcalListCalendarsTool,
    GcalListEventsTool,
    GcalRespondToEventTool,
    GcalUpdateEventTool,
)
from ricky.tools.integrations.google import GoogleAuth
from ricky.tools.integrations.google.scopes import GCAL_SCOPES
from ricky.tools.integrations.google.toolset import (
    GoogleServiceToolset,
    google_accounts_available,
)


class GcalToolset(GoogleServiceToolset):
    """The Calendar tools plus service-local client/auth lifecycle."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        root: Path | None = None,
        auth: GoogleAuth | None = None,
    ) -> None:
        super().__init__(settings, scopes=GCAL_SCOPES, root=root, auth=auth)
        self._client = GcalClient(
            auth=self._auth,
            base_url=settings.gcal.api_base_url,
            timeout_seconds=settings.request_timeout_seconds,
        )
        self.tools: list[Tool] = [
            GcalListCalendarsTool(self._client),
            GcalListEventsTool(self._client),
            GcalGetEventTool(self._client),
            GcalCheckAvailabilityTool(self._client),
            GcalCreateEventTool(self._client),
            GcalUpdateEventTool(self._client),
            GcalRespondToEventTool(self._client),
            GcalDeleteEventTool(self._client),
        ]

    async def check_account(self, account: str) -> tuple[str, str]:
        """Return primary calendar summary and account Calendar timezone."""
        payload = await self._client.call(account, "GET", "calendars/primary")
        return (
            str(payload.get("summary") or "primary"),
            await self._client.timezone(account),
        )

    async def aclose(self) -> None:
        """Close Calendar and owned shared-auth clients."""
        await self._client.aclose()
        await super().aclose()


def gcal_toolset(
    settings: RickySettings,
    *,
    root: Path | None = None,
    auth: GoogleAuth | None = None,
) -> GcalToolset | None:
    """Build Calendar tools when an account has matching OAuth credentials."""
    if not google_accounts_available(settings):
        return None
    return GcalToolset(settings, root=root, auth=auth)


__all__ = [
    "GCAL_SCOPES",
    "GcalApiError",
    "GcalCheckAvailabilityTool",
    "GcalCreateEventTool",
    "GcalDeleteEventTool",
    "GcalError",
    "GcalGetEventTool",
    "GcalListCalendarsTool",
    "GcalListEventsTool",
    "GcalMutationUnknownError",
    "GcalRespondToEventTool",
    "GcalToolset",
    "GcalTransportError",
    "GcalUpdateEventTool",
    "gcal_toolset",
]
