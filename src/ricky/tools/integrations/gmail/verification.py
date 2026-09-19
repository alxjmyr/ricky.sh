"""Bounded read-only Gmail adapter for live browser verification challenges."""

import re
from datetime import UTC, datetime
from email.utils import getaddresses
from urllib.parse import quote

from ricky.browser.verification import (
    VerificationMessage,
    VerificationQuery,
    VerificationUnavailable,
    eligible_message,
)
from ricky.tools.integrations.gmail.client import GmailClient, GmailError
from ricky.tools.integrations.gmail.types import GmailMessage
from ricky.tools.integrations.google.auth import GoogleAuthError


class GmailVerificationReader:
    """Uses a runtime-owned client; does not expose an arbitrary Gmail query tool."""

    def __init__(self, client: GmailClient) -> None:
        self._client = client

    async def read(self, query: VerificationQuery) -> tuple[VerificationMessage, ...]:
        try:
            return await self._read(query)
        except (GmailError, GoogleAuthError, ValueError, TypeError, AttributeError):
            raise VerificationUnavailable(
                "The authorized Gmail account is unavailable; check its connection and permissions."
            ) from None

    async def _read(self, query: VerificationQuery) -> tuple[VerificationMessage, ...]:
        account = query.source.account.qualified
        if self._client.account_email(account).strip().casefold() != query.source.primary_email:
            raise GmailError("verification account configuration changed")
        profile = await self._client.call(account, "GET", "profile")
        if str(profile.get("emailAddress", "")).casefold() != query.source.primary_email:
            raise GmailError("authenticated Gmail identity differs from the verification account")
        # Recipient filtering below is authoritative. Restrict only time here;
        # arbitrary page/model text never becomes Gmail search syntax.
        hits = await self._client.call(
            account,
            "GET",
            "messages",
            params={
                "q": f"after:{int(query.earliest_at.timestamp())}",
                "maxResults": query.limit,
                "includeSpamTrash": False,
            },
        )
        result = []
        seen = set()
        for hit in (hits.get("messages") or [])[: query.limit]:
            if not isinstance(hit, dict):
                continue
            message_id = hit.get("id")
            if not isinstance(message_id, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,200}", message_id
            ):
                continue
            if message_id in seen:
                continue
            seen.add(message_id)
            payload = await self._client.call(
                account, "GET", f"messages/{quote(message_id, safe='')}", params={"format": "full"}
            )
            if payload.get("id") != message_id:
                continue
            # Gmail internalDate, not attacker-controlled Date headers, bounds freshness.
            try:
                received = datetime.fromtimestamp(int(str(payload["internalDate"])) / 1000, tz=UTC)
            except (KeyError, ValueError, TypeError, OverflowError, OSError):
                continue
            parsed = GmailMessage.from_api(payload, body_char_limit=8000)
            recipients = set(address.strip().casefold() for address in parsed.to)
            headers = (payload.get("payload") or {}).get("headers") or []
            for header in headers:
                if (
                    isinstance(header, dict)
                    and str(header.get("name", "")).casefold() == "delivered-to"
                ):
                    recipients.update(
                        address.casefold()
                        for _, address in getaddresses([str(header.get("value", ""))])
                    )
            message = VerificationMessage(
                account=query.source.account,
                message_id=message_id,
                received_at=received,
                sender=parsed.from_addr[:500],
                recipients=tuple(sorted(recipients))[:100],
                subject=parsed.subject[:1000],
                text=parsed.body_text[:8000],
            )
            if eligible_message(message, query, datetime.now(UTC)):
                result.append(message)
        return tuple(result)
