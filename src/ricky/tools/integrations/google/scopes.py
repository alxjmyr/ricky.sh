"""Single source of truth for Google service OAuth scopes.

Service packages import their scope sets from here so the consent union
requested by ``ricky config google auth`` can never drift from what the
service clients enforce at runtime.
"""

from __future__ import annotations

GMAIL_SCOPES = frozenset({"https://www.googleapis.com/auth/gmail.modify"})

GCAL_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.events",
    }
)

# Human-facing service name -> scope set, used for per-service readiness
# reporting; the consent flow requests the union of every enabled service.
SERVICE_SCOPES: dict[str, frozenset[str]] = {
    "Gmail": GMAIL_SCOPES,
    "Calendar": GCAL_SCOPES,
}

ALL_SERVICE_SCOPES = frozenset().union(*SERVICE_SCOPES.values())

__all__ = [
    "ALL_SERVICE_SCOPES",
    "GCAL_SCOPES",
    "GMAIL_SCOPES",
    "SERVICE_SCOPES",
]
