"""Interface-neutral browser challenge records and single-response ownership.

Records contain safe metadata only. A response belongs to one resident owner;
durable records are never sufficient to reconstruct or replay a submission.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, Field, SecretStr, model_validator

from ricky.browser.policy import canonical_origin
from ricky.browser.types import BrowserModel
from ricky.profiles import ProfileResourceRef, ProfileScope

ChallengeState = Literal[
    "waiting_for_user",
    "responded",
    "submitting",
    "submitted",
    "resolved",
    "expired",
    "cancelled",
    "invalidated",
    "blocked",
    "in_doubt",
]
_TERMINAL = frozenset({"resolved", "expired", "cancelled", "invalidated", "blocked", "in_doubt"})
_TRANSITIONS: dict[ChallengeState, frozenset[ChallengeState]] = {
    "waiting_for_user": frozenset({"responded", "expired", "cancelled", "invalidated", "blocked"}),
    "responded": frozenset(
        {"submitting", "resolved", "expired", "cancelled", "invalidated", "blocked"}
    ),
    "submitting": frozenset({"submitted", "blocked", "in_doubt"}),
    "submitted": frozenset({"resolved", "blocked", "invalidated", "expired", "cancelled"}),
}


class ChallengeBinding(BrowserModel):
    """Trusted live occurrence facts; never inferred from a reply's contents."""

    owner_id: str = Field(min_length=1, max_length=200)
    profile_scope: ProfileScope
    resource: ProfileResourceRef | None = None
    resource_configuration_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    session_id: str = Field(pattern=r"^browser_session_[0-9a-f]{32}$")
    page_id: str = Field(pattern=r"^browser_page_[0-9a-f]{32}$")
    page_generation: int = Field(ge=0)
    top_level_origin: str = Field(min_length=1, max_length=2000)
    frame_origin: str = Field(min_length=1, max_length=2000)
    occurrence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    purpose: Literal["authentication", "transaction", "verification"]
    prior_transaction_id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _validate_binding(self) -> ChallengeBinding:
        for origin in (self.top_level_origin, self.frame_origin):
            if not origin.startswith("https://") or canonical_origin(origin) != origin:
                raise ValueError("challenge requires exact HTTPS origins")
        if self.resource is not None and self.resource.profile not in self.profile_scope.profiles:
            raise ValueError("challenge resource is outside the issued scope")
        if (self.resource is None) != (self.resource_configuration_digest is None):
            raise ValueError("configured challenge resource requires its configuration digest")
        return self


class ChallengeSource(BrowserModel):
    """Authenticated responder identity, including the exact delivered prompt."""

    principal_id: str = Field(min_length=1, max_length=500)
    conversation_id: str = Field(min_length=1, max_length=500)
    prompt_message_id: str = Field(min_length=1, max_length=500)


class BrowserChallenge(BrowserModel):
    """Versioned safe record; OTP values cannot be stored in this type."""

    version: Literal[1] = 1
    id: str = Field(pattern=r"^browser_challenge_[0-9a-f]{32}$")
    binding: ChallengeBinding
    kind: Literal["otp", "manual"]
    instruction: str = Field(min_length=1, max_length=1000)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    source: ChallengeSource | None = None
    state: ChallengeState = "waiting_for_user"
    revision: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _valid_expiry(self) -> BrowserChallenge:
        if self.expires_at <= self.created_at:
            raise ValueError("challenge expiry must follow creation")
        return self

    def transition(self, state: ChallengeState) -> BrowserChallenge:
        if state not in _TRANSITIONS.get(self.state, frozenset()):
            raise ChallengeError("challenge transition is not permitted")
        return self.model_copy(update={"state": state, "revision": self.revision + 1})


class ChallengeError(RuntimeError):
    """Safe, value-free failure suitable for interface reporting."""


@dataclass(frozen=True)
class ChallengeResponse:
    """Resident input only, intentionally separate from durable record models."""

    code: SecretStr | None = None


ChallengeWriter = Callable[[BrowserChallenge, int], Awaitable[None]]


class LiveBrowserChallenge:
    """One live owner's bounded wait and atomic response claim.

    The injected writer persists a record with compare-and-swap against the expected
    revision. The caller owns browser locking, effect dispatch, and verification;
    neither a received code nor an input receipt marks a challenge resolved.
    """

    def __init__(self, record: BrowserChallenge, *, writer: ChallengeWriter) -> None:
        if record.state != "waiting_for_user" or record.revision != 1:
            raise ChallengeError("cannot resume a challenge from persisted state")
        self.record = record
        self._writer = writer
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()
        self._response: ChallengeResponse | None = None
        self.assistance_reason: str | None = None

    async def _publish(self, record: BrowserChallenge) -> None:
        # Cancellation cannot leave the resident revision behind a committed CAS.
        async def publish() -> None:
            await self._writer(record, self.record.revision)
            self.record = record
            self._changed.set()

        task = asyncio.create_task(publish())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def bind_source(self, source: ChallengeSource) -> None:
        async with self._lock:
            if self.record.state != "waiting_for_user" or self.record.source is not None:
                raise ChallengeError("challenge prompt is already bound or no longer pending")
            await self._publish(
                self.record.model_copy(
                    update={
                        "source": source,
                        "revision": self.record.revision + 1,
                    }
                )
            )

    async def respond(
        self,
        response: ChallengeResponse,
        *,
        source: ChallengeSource,
        now: datetime | None = None,
    ) -> None:
        async with self._lock:
            if self.record.source != source:
                raise ChallengeError("reply does not match this challenge's owner and prompt")
            if self.record.state != "waiting_for_user":
                raise ChallengeError("challenge already answered or no longer available")
            if (now or datetime.now(UTC)) >= self.record.expires_at:
                await self._publish(self.record.transition("expired"))
                raise ChallengeError("challenge expired; no response was submitted")
            if (response.code is not None) != (self.record.kind == "otp"):
                raise ChallengeError("response kind does not match the challenge")
            # Keep SecretStr wrapped; the destination boundary owns validation of
            # the actual code. No values enter records, exceptions, or evidence.
            self._response = response
            try:
                await self._publish(self.record.transition("responded"))
            except BaseException:
                if self.record.state != "responded":
                    self._response = None
                raise

    async def wait(self, *, consume: bool = True) -> ChallengeResponse:
        try:
            async with asyncio.timeout(
                max(0, (self.record.expires_at - datetime.now(UTC)).total_seconds())
            ):
                while True:
                    async with self._lock:
                        if self.record.state == "responded" and self._response is not None:
                            if not consume:
                                return self._response
                            response, self._response = self._response, None
                            return response
                        if self.record.state != "waiting_for_user":
                            raise ChallengeError("challenge no longer has an available response")
                        self._changed.clear()
                    await self._changed.wait()
        except TimeoutError:
            await self.finish("expired")
            raise ChallengeError("challenge expired; no response was submitted") from None
        except asyncio.CancelledError:
            await self.finish("cancelled")
            raise

    async def finish(self, state: ChallengeState) -> None:
        async with self._lock:
            if self.record.state in _TERMINAL:
                return
            if self.record.state == "submitting" and state in {
                "expired",
                "cancelled",
                "invalidated",
            }:
                state = "in_doubt"
            await self._publish(self.record.transition(state))
            if state in _TERMINAL:
                self._response = None

    async def begin_submission(self, binding: ChallengeBinding) -> None:
        async with self._lock:
            if self.record.state != "responded":
                raise ChallengeError("challenge response has not been claimed")
            if datetime.now(UTC) >= self.record.expires_at:
                await self._publish(self.record.transition("expired"))
                raise ChallengeError("challenge expired before submission")
            if binding != self.record.binding:
                await self._publish(self.record.transition("invalidated"))
                raise ChallengeError("live browser challenge changed before submission")
            await self._publish(self.record.transition("submitting"))
