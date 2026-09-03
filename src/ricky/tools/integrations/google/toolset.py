"""Shared toolset lifecycle for Google service toolpacks."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

from ricky.config import RickySettings
from ricky.tools.integrations.google.auth import GoogleAuth


def google_accounts_available(settings: RickySettings) -> bool:
    """Whether any configured Google account has matching OAuth credentials."""
    return any(account in settings.google_oauth_clients for account in settings.google.accounts)


class GoogleServiceToolset:
    """Owns the shared GoogleAuth lifecycle for one Google service toolpack.

    Subclasses build their service client from ``self._auth`` and close it in
    ``aclose`` before delegating to ``super().aclose()``.
    """

    def __init__(
        self,
        settings: RickySettings,
        *,
        scopes: Collection[str],
        root: Path | None = None,
        auth: GoogleAuth | None = None,
    ) -> None:
        self._owns_auth = auth is None
        self._auth = auth or GoogleAuth(settings, scopes=scopes, root=root)

    async def aclose(self) -> None:
        """Close the owned shared-auth client (API clients close first)."""
        if self._owns_auth:
            await self._auth.aclose()


__all__ = ["GoogleServiceToolset", "google_accounts_available"]
