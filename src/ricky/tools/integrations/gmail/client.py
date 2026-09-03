"""Async Gmail REST client on the shared Google transport core."""

from __future__ import annotations

import difflib
from typing import ClassVar

import httpx

from ricky.tools.integrations.gmail.types import GmailLabel
from ricky.tools.integrations.google.client import (
    PAGE_CAP,
    GoogleApiClient,
    GoogleAuthLike,
)
from ricky.tools.integrations.google.scopes import GMAIL_SCOPES


class GmailError(Exception):
    """Base Gmail integration failure safe to show the model."""


class GmailApiError(GmailError):
    """A structured Gmail API error response."""

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
            f"Gmail request for account {account!r} failed "
            f"(HTTP {status_code}: {shown_reason}){detail}"
        )


class GmailTransportError(GmailError):
    """A read failed at the transport boundary."""


class GmailMutationUnknownError(GmailError):
    """A mutation may have reached Gmail and cannot be replayed blindly."""

    def __init__(self, *, account: str, action: str, check: str) -> None:
        super().__init__(
            f"{action} status unknown for Gmail account {account!r}: the request failed "
            f"after it may have reached Gmail. Check {check} before retrying."
        )


class GmailClient(GoogleApiClient):
    """Thin authenticated wrapper around users/me Gmail REST endpoints."""

    service_label: ClassVar[str] = "Gmail"
    config_key: ClassVar[str] = "gmail.api_base_url"
    default_mutation_check: ClassVar[str] = "Gmail"

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
            required_scopes=GMAIL_SCOPES,
        )
        self._label_cache: dict[str, list[GmailLabel]] = {}

    def _endpoint(self, path: str) -> str:
        return f"/gmail/v1/users/me/{path.lstrip('/')}"

    def _service_error(self, message: str) -> Exception:
        return GmailError(message)

    def _api_error(self, *, account: str, status_code: int, reason: str, message: str) -> Exception:
        return GmailApiError(
            account=account, status_code=status_code, reason=reason, message=message
        )

    def _transport_error(self, message: str) -> Exception:
        return GmailTransportError(message)

    def _mutation_unknown_error(self, *, account: str, action: str, check: str) -> Exception:
        return GmailMutationUnknownError(account=account, action=action, check=check)

    def account_email(self, account: str) -> str:
        """Configured expected identity used as the outbound From address."""
        identity = self._auth.validate_account(account)
        email = getattr(identity, "email", None)
        if not isinstance(email, str) or not email:
            raise GmailError(f"Google account {account!r} has no configured email identity")
        return email

    async def labels(self, account: str) -> list[GmailLabel]:
        """Return the per-account label directory, cached for this toolset."""
        self.validate_account(account)
        if account not in self._label_cache:
            payload = await self.call(account, "GET", "labels")
            self._label_cache[account] = [
                GmailLabel.from_api(item)
                for item in payload.get("labels") or []
                if isinstance(item, dict)
            ]
        return list(self._label_cache[account])

    async def label_map(self, account: str) -> dict[str, str]:
        """Map label ids to human-readable names."""
        return {label.id: label.name for label in await self.labels(account)}

    async def resolve_label(self, account: str, value: str) -> GmailLabel:
        """Resolve an exact case-insensitive label name or exact id."""
        labels = await self.labels(account)
        stripped = value.strip()
        for label in labels:
            if stripped == label.id or stripped.casefold() == label.name.casefold():
                return label

        names = [label.name for label in labels]
        folded = {name.casefold(): name for name in names}
        suggestions = [
            folded[item]
            for item in difflib.get_close_matches(
                stripped.casefold(), list(folded), n=5, cutoff=0.35
            )
        ]
        candidate_text = f"; close matches: {', '.join(suggestions)}" if suggestions else ""
        raise GmailError(f"unknown Gmail label {value!r}{candidate_text}; use gmail_list_labels")

    def invalidate_labels(self, account: str) -> None:
        """Drop one account's label directory after label creation."""
        self._label_cache.pop(account, None)


def _error_hint(status_code: int, reason: str, account: str) -> str:
    if status_code == 401:
        return f"authorization rejected; run ricky config google auth {account}"
    if status_code == 404:
        return "the Gmail message, thread, draft, label, or attachment id may be stale"
    if status_code == 429 or reason in {"rateLimitExceeded", "userRateLimitExceeded"}:
        return "Gmail rate limit exceeded; try again shortly"
    if status_code == 403 and reason in {
        "insufficientPermissions",
        "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
    }:
        return f"required Gmail scope is missing; run ricky config google auth {account}"
    return ""


__all__ = [
    "GmailApiError",
    "GmailClient",
    "GmailError",
    "GmailMutationUnknownError",
    "GmailTransportError",
    "PAGE_CAP",
]
