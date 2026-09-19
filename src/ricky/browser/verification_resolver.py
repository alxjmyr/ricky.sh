"""Bounded autonomous retrieval with ordinary-agent interpretation as a fallback."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr

from ricky.browser.challenges import (
    ChallengeError,
    ChallengeResponse,
    ChallengeSource,
    LiveBrowserChallenge,
)
from ricky.browser.types import BrowserModel
from ricky.browser.verification import (
    VerificationCeiling,
    VerificationMessage,
    VerificationMessageReader,
    VerificationQuery,
    VerificationUnavailable,
    eligible_message,
)
from ricky.browser.verification_store import VerificationClaimStore, VerificationSourceClaim


class VerificationResolution(BrowserModel):
    state: Literal["answered", "interpretation", "unavailable", "ambiguous"]
    reason: str = Field(max_length=500)
    messages: tuple[VerificationMessage, ...] = Field(default=(), max_length=20)


class VerificationAnswer(BrowserModel):
    challenge_id: str = Field(pattern=r"^browser_challenge_[0-9a-f]{32}$")
    message_id: str = Field(min_length=1, max_length=200)
    token_index: int | None = Field(default=None, ge=0, lt=512)


def candidate_tokens(message: VerificationMessage) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            re.findall(
                r"(?<![\w-])[A-Za-z0-9-]{4,128}(?![\w-])",
                message.subject + "\n" + message.text,
            )
        )
    )[:512]


def matches_service(message: VerificationMessage, origin: str) -> bool:
    """Conservative generic service association; it does not authenticate mail content."""
    host = (urlsplit(origin).hostname or "").removeprefix("www.")
    address = parseaddr(message.sender)[1].casefold()
    domain = address.rpartition("@")[2]
    return bool(host and domain and (domain == host or domain.endswith("." + host)))


def extract_simple_code(message: VerificationMessage) -> SecretStr | None:
    text = message.subject + "\n" + message.text
    if not re.search(
        r"\b(verification|one[- ]time|security|authentication|login|sign[- ]in)\b", text, re.I
    ):
        return None
    codes = set(re.findall(r"\bcode\s*(?:is\s*|:\s*|=\s*)?(\d{4,10})(?![\w-])", text, re.I))
    return SecretStr(next(iter(codes))) if len(codes) == 1 else None


class BrowserVerificationResolver:
    """Never widens access, calls an LLM, or dispatches a browser action."""

    def __init__(
        self,
        ceiling: VerificationCeiling,
        reader: VerificationMessageReader,
        claims: VerificationClaimStore,
        validate: Callable[[], Awaitable[None]],
    ) -> None:
        self.ceiling = ceiling
        self.reader = reader
        self.claims = claims
        self.validate = validate
        self._candidates: dict[str, tuple[VerificationQuery, VerificationMessage]] = {}

    async def resolve(
        self,
        owner: LiveBrowserChallenge,
        *,
        recipient: str | None,
        account: str | None = None,
        not_before: datetime | None = None,
    ) -> VerificationResolution:
        record = owner.record
        origin = record.binding.top_level_origin
        if self.ceiling.allowed_origins and origin not in self.ceiling.allowed_origins:
            return VerificationResolution(
                state="unavailable", reason="This website is outside verification policy."
            )
        sources = [
            source
            for source in self.ceiling.sources
            if account is None or source.account.qualified == account
        ]
        if recipient is not None:
            recipient = recipient.strip().casefold()
            sources = [source for source in sources if recipient in source.recipients]
        if len(sources) != 1:
            return VerificationResolution(
                state="ambiguous" if sources else "unavailable",
                reason="No unique authorized verification account matches this recipient.",
            )
        source = sources[0]
        query = VerificationQuery(
            challenge_id=record.id,
            source=source,
            recipient=recipient or source.primary_email,
            origin=origin,
            issued_at=record.created_at,
            earliest_at=max(
                record.created_at - timedelta(seconds=self.ceiling.lookback_seconds),
                min(not_before, record.created_at)
                if not_before
                else record.created_at - timedelta(seconds=self.ceiling.lookback_seconds),
            ),
            expires_at=record.expires_at,
            limit=self.ceiling.max_messages,
        )
        allowance = min(
            self.ceiling.poll_timeout_seconds,
            (record.expires_at - datetime.now(UTC)).total_seconds(),
        )
        try:
            async with asyncio.timeout(max(0.0, allowance)):
                while True:
                    await self.validate()
                    messages = await self.reader.read(query)
                    await self.validate()
                    candidates = []
                    for message in messages[: query.limit]:
                        if (
                            eligible_message(message, query, datetime.now(UTC))
                            and matches_service(message, origin)
                            and not await self.claims.claimed(
                                message.account,
                                message.message_id,
                                scope=record.binding.profile_scope,
                            )
                        ):
                            candidates.append(message)
                    if len(candidates) > 1:
                        return VerificationResolution(
                            state="ambiguous",
                            reason="Multiple verification messages match; no code was selected.",
                        )
                    if candidates:
                        message = candidates[0]
                        self._candidates[record.id] = (query, message)
                        code = extract_simple_code(message)
                        if code is None:
                            return VerificationResolution(
                                state="interpretation",
                                reason="Interpret this untrusted message in the agent loop.",
                                messages=(message,),
                            )
                        if await self.accept(owner, message.message_id, code):
                            return VerificationResolution(
                                state="answered",
                                reason="A matching email code was claimed but not submitted.",
                            )
                    await asyncio.sleep(self.ceiling.poll_interval_seconds)
        except TimeoutError:
            return VerificationResolution(
                state="unavailable",
                reason="No eligible verification email arrived within the bounded wait.",
            )
        except VerificationUnavailable as exc:
            return VerificationResolution(state="unavailable", reason=str(exc))

    async def accept(self, owner: LiveBrowserChallenge, message_id: str, code: SecretStr) -> bool:
        await self.validate()
        selected = self._candidates.get(owner.record.id)
        if selected is None:
            raise ChallengeError("no eligible verification message belongs to this challenge")
        query, message = selected
        if (
            message.message_id != message_id
            or not eligible_message(message, query, datetime.now(UTC))
            or owner.record.state != "waiting_for_user"
            or datetime.now(UTC) >= owner.record.expires_at
            or owner.record.source is not None
        ):
            raise ChallengeError("verification candidate is stale or belongs to another challenge")
        # Compare wrapped candidate tokens; only browser dispatch unwraps the selected code.
        tokens = re.findall(
            r"(?<![\w-])[A-Za-z0-9-]{4,128}(?![\w-])", message.subject + "\n" + message.text
        )
        if not any(code == SecretStr(token) for token in tokens):
            raise ChallengeError("the proposed code is not present in the eligible message")
        claim = VerificationSourceClaim(
            profile_scope=owner.record.binding.profile_scope,
            account=message.account,
            message_id=message.message_id,
            challenge_id=owner.record.id,
            claimed_at=datetime.now(UTC),
        )
        if not await self.claims.claim(claim, scope=owner.record.binding.profile_scope):
            self.discard(owner.record.id)
            return False
        source = ChallengeSource(
            principal_id="gmail:" + message.account.qualified,
            conversation_id=owner.record.binding.owner_id,
            prompt_message_id=message.message_id,
        )
        await owner.bind_source(source)
        await owner.respond(ChallengeResponse(code=code), source=source)
        self._candidates.pop(owner.record.id, None)
        return True

    def discard(self, challenge_id: str) -> None:
        self._candidates.pop(challenge_id, None)

    async def answer(self, owner: LiveBrowserChallenge, answer: VerificationAnswer) -> bool:
        selected = self._candidates.get(answer.challenge_id)
        if selected is None or owner.record.id != answer.challenge_id:
            raise ChallengeError("verification interpretation belongs to another challenge")
        tokens = candidate_tokens(selected[1])
        if answer.token_index is None or answer.token_index >= len(tokens):
            raise ChallengeError("verification token selection is unavailable")
        return await self.accept(owner, answer.message_id, SecretStr(tokens[answer.token_index]))
