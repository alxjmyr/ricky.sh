"""Provider-neutral contracts for scoped browser verification message retrieval."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, field_validator, model_validator

from ricky.browser.policy import canonical_origin
from ricky.browser.types import BrowserModel
from ricky.config import RickySettings
from ricky.profiles import ProfileResourceRef, ProfileScope


class VerificationSource(BrowserModel):
    kind: Literal["gmail"] = "gmail"
    account: ProfileResourceRef
    primary_email: str = Field(min_length=3, max_length=320)
    recipients: tuple[str, ...] = Field(min_length=1, max_length=31)

    @field_validator("recipients")
    @classmethod
    def _recipients(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not value or "@" not in value or value != value.strip().casefold() for value in values
        ):
            raise ValueError("verification recipients must be normalized email identities")
        if len(values) != len(set(values)):
            raise ValueError("verification recipients must be unique")
        return values

    @model_validator(mode="after")
    def _primary(self) -> VerificationSource:
        if self.primary_email not in self.recipients:
            raise ValueError("primary verification account identity must be an eligible recipient")
        return self


class VerificationCeiling(BrowserModel):
    """Pinned optional access; an absent ceiling never inherits new mailbox access."""

    version: Literal[1] = 1
    sources: tuple[VerificationSource, ...] = Field(min_length=1, max_length=30)
    allowed_origins: tuple[str, ...] = Field(default=(), max_length=100)
    poll_timeout_seconds: float = Field(gt=0, le=120)
    poll_interval_seconds: float = Field(ge=0.1, le=30)
    lookback_seconds: int = Field(ge=0, le=600)
    max_messages: int = Field(ge=1, le=20)
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _identities(self) -> VerificationCeiling:
        names = [source.account.qualified for source in self.sources]
        if len(names) != len(set(names)):
            raise ValueError("verification source identities must be unique")
        for origin in self.allowed_origins:
            if not origin.startswith("https://") or canonical_origin(origin) != origin:
                raise ValueError("verification requires exact HTTPS origins")
        return self


class VerificationQuery(BrowserModel):
    challenge_id: str = Field(pattern=r"^browser_challenge_[0-9a-f]{32}$")
    source: VerificationSource
    recipient: str = Field(min_length=3, max_length=320)
    origin: str = Field(min_length=8, max_length=2000)
    issued_at: AwareDatetime
    earliest_at: AwareDatetime
    expires_at: AwareDatetime
    limit: int = Field(ge=1, le=20)

    @model_validator(mode="after")
    def _scope(self) -> VerificationQuery:
        if self.recipient not in self.source.recipients:
            raise ValueError("verification recipient is outside the selected account")
        if not self.origin.startswith("https://") or canonical_origin(self.origin) != self.origin:
            raise ValueError("verification requires an exact HTTPS origin")
        if not self.earliest_at <= self.issued_at < self.expires_at:
            raise ValueError("verification message window is invalid")
        return self


class VerificationMessage(BrowserModel):
    """Untrusted bounded content; source identity comes from the connector."""

    account: ProfileResourceRef
    message_id: str = Field(min_length=1, max_length=200)
    received_at: AwareDatetime
    sender: str = Field(max_length=500)
    recipients: tuple[str, ...] = Field(max_length=100)
    subject: str = Field(max_length=1000)
    text: str = Field(max_length=8000)


class VerificationMessageReader(Protocol):
    async def read(self, query: VerificationQuery) -> tuple[VerificationMessage, ...]: ...


class VerificationUnavailable(RuntimeError):
    """Value-free connector failure that permits the user-assisted fallback."""


def compile_verification_ceiling(
    settings: RickySettings, scope: ProfileScope, *, background: bool
) -> VerificationCeiling | None:
    policy = settings.browser.verification
    if not policy.enabled or (background and not policy.allow_background):
        return None
    scoped = settings.resolve_profile_runtime_settings(scope)
    sources = []
    for name in sorted(policy.gmail_accounts):
        ref = ProfileResourceRef.from_qualified(name)
        if ref.profile not in scope.profiles:
            continue
        account = scoped.google.accounts.get(name)
        if account is None:
            continue
        recipients = tuple(
            sorted({account.email.strip().casefold(), *account.verification_aliases})
        )
        sources.append(
            VerificationSource(
                account=ref,
                primary_email=account.email.strip().casefold(),
                recipients=recipients,
            )
        )
    if not sources:
        return None
    payload = {
        "policy": policy.model_dump(mode="json"),
        "sources": [source.model_dump(mode="json") for source in sources],
        "scope": scope.model_dump(mode="json"),
        "background": background,
    }
    return VerificationCeiling(
        sources=tuple(sources),
        allowed_origins=tuple(policy.allowed_origins),
        poll_timeout_seconds=policy.poll_timeout_seconds,
        poll_interval_seconds=policy.poll_interval_seconds,
        lookback_seconds=policy.lookback_seconds,
        max_messages=policy.max_messages,
        policy_digest=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
    )


def eligible_message(message: VerificationMessage, query: VerificationQuery, now: datetime) -> bool:
    """Recipient, connector account and server receipt time are independent checks."""
    return (
        message.account == query.source.account
        and query.recipient in message.recipients
        and query.earliest_at <= message.received_at <= now < query.expires_at
    )
