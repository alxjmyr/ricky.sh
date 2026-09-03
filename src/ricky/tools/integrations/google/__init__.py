"""Shared Google OAuth and transport infrastructure."""

from ricky.tools.integrations.google.auth import (
    IDENTITY_SCOPES,
    GoogleAuth,
    GoogleAuthError,
    GoogleAuthStatus,
    GoogleTokenRecord,
)
from ricky.tools.integrations.google.client import GoogleApiClient
from ricky.tools.integrations.google.scopes import (
    ALL_SERVICE_SCOPES,
    GCAL_SCOPES,
    GMAIL_SCOPES,
    SERVICE_SCOPES,
)
from ricky.tools.integrations.google.toolset import (
    GoogleServiceToolset,
    google_accounts_available,
)
from ricky.tools.integrations.google.types import GoogleAccountId

__all__ = [
    "ALL_SERVICE_SCOPES",
    "GCAL_SCOPES",
    "GMAIL_SCOPES",
    "SERVICE_SCOPES",
    "GoogleApiClient",
    "GoogleAccountId",
    "GoogleAuth",
    "GoogleAuthError",
    "GoogleAuthStatus",
    "GoogleServiceToolset",
    "GoogleTokenRecord",
    "IDENTITY_SCOPES",
    "google_accounts_available",
]
