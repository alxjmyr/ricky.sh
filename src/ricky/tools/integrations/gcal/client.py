"""Async Google Calendar REST client on the shared Google transport core."""

from __future__ import annotations

from typing import ClassVar

import httpx

from ricky.tools.integrations.google.client import (
    PAGE_CAP,
    GoogleApiClient,
    GoogleAuthLike,
)
from ricky.tools.integrations.google.scopes import GCAL_SCOPES


class GcalError(Exception):
    """Base Calendar integration failure safe to show the model."""


class GcalApiError(GcalError):
    """A structured Calendar API error response."""

    def __init__(
        self,
        *,
        account: str,
        status_code: int,
        reason: str,
        message: str = "",
    ) -> None:
        self.account = account
        self.status_code = status_code
        self.reason = reason
        hint = _error_hint(status_code, reason, account)
        shown_reason = reason or message or "unknown_error"
        detail = f" ({hint})" if hint else ""
        super().__init__(
            f"Calendar request for account {account!r} failed "
            f"(HTTP {status_code}: {shown_reason}){detail}"
        )


class GcalTransportError(GcalError):
    """A Calendar read failed at the transport boundary."""


class GcalMutationUnknownError(GcalError):
    """A Calendar mutation may have happened and cannot be replayed blindly."""

    def __init__(self, *, account: str, action: str) -> None:
        super().__init__(
            f"{action} status unknown for Calendar account {account!r}: the request "
            "failed after it may have reached Google. Check Google Calendar before retrying."
        )


class GcalClient(GoogleApiClient):
    """Thin authenticated wrapper around Calendar v3 endpoints."""

    service_label: ClassVar[str] = "Calendar"
    config_key: ClassVar[str] = "gcal.api_base_url"
    default_mutation_check: ClassVar[str] = "Google Calendar"

    def __init__(
        self,
        *,
        auth: GoogleAuthLike,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            auth=auth,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            transport=transport,
            required_scopes=GCAL_SCOPES,
        )
        self._timezone_cache: dict[str, str] = {}

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        return base_url.rstrip("/") + "/"

    def _service_error(self, message: str) -> Exception:
        return GcalError(message)

    def _api_error(self, *, account: str, status_code: int, reason: str, message: str) -> Exception:
        return GcalApiError(
            account=account, status_code=status_code, reason=reason, message=message
        )

    def _transport_error(self, message: str) -> Exception:
        return GcalTransportError(message)

    def _mutation_unknown_error(self, *, account: str, action: str, check: str) -> Exception:
        del check  # the Calendar wording is fixed
        return GcalMutationUnknownError(account=account, action=action)

    async def timezone(self, account: str) -> str:
        """Return the account Calendar timezone, cached for this toolset."""
        self.validate_account(account)
        if account not in self._timezone_cache:
            payload = await self.call(account, "GET", "users/me/settings/timezone")
            value = str(payload.get("value") or "")
            if not value:
                raise GcalError(f"Calendar timezone setting is missing for account {account!r}")
            self._timezone_cache[account] = value
        return self._timezone_cache[account]


def _error_hint(status_code: int, reason: str, account: str) -> str:
    if status_code == 401:
        return f"authorization rejected; run ricky config google auth {account}"
    if status_code == 404:
        return "the Calendar event or calendar id may be stale"
    if status_code == 412:
        return "the event changed while this update was in flight; re-read it and retry"
    if status_code == 429 or reason in {"rateLimitExceeded", "userRateLimitExceeded"}:
        return "Calendar rate limit exceeded; try again shortly"
    if status_code == 403 and reason in {
        "insufficientPermissions",
        "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
    }:
        return f"required Calendar scopes are missing; run ricky config google auth {account}"
    if status_code == 403 and reason in {
        "forbiddenForNonOrganizer",
        "cannotChangeOrganizer",
    }:
        return (
            "you are an attendee, not the organizer; use gcal_respond_to_event. "
            "The API cannot propose a new time"
        )
    return ""


__all__ = [
    "GcalApiError",
    "GcalClient",
    "GcalError",
    "GcalMutationUnknownError",
    "GcalTransportError",
    "PAGE_CAP",
]
