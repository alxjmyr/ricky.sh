"""Automatic retrieval, interpretation, ambiguity and source replay boundaries."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr

from browser_challenge_support import configured, record
from ricky.browser.challenge_store import BrowserChallengeStore
from ricky.browser.challenges import ChallengeError, LiveBrowserChallenge
from ricky.browser.verification import VerificationMessage, compile_verification_ceiling
from ricky.browser.verification_resolver import (
    BrowserVerificationResolver,
    VerificationAnswer,
    extract_simple_code,
)
from ricky.browser.verification_store import VerificationClaimStore


@pytest.mark.parametrize(
    "scenario",
    ["automatic", "delayed", "interpretation", "ambiguous", "foreign_service", "replaced"],
)
async def test_resolver_prefers_eligible_mail_and_never_guesses(scenario, tmp_path):
    settings = configured().model_copy(update={"user_data_dir": str(tmp_path / "user")})
    initial = record()
    scope = initial.binding.profile_scope
    store = BrowserChallengeStore(settings)
    await store.create(initial, scope=scope)
    owner = LiveBrowserChallenge(
        initial, writer=lambda updated, revision: store.update(updated, revision, scope=scope)
    )
    ceiling = compile_verification_ceiling(settings, scope, background=True)
    assert ceiling is not None
    ceiling = ceiling.model_copy(update={"poll_interval_seconds": 0.1, "poll_timeout_seconds": 0.4})
    message = VerificationMessage(
        account=ceiling.sources[0].account,
        message_id="message-1",
        received_at=datetime.now(UTC) - timedelta(seconds=1)
        if scenario == "replaced"
        else datetime.now(UTC),
        sender="verify@unrelated.example"
        if scenario == "foreign_service"
        else "verify@merchant.example",
        recipients=("owner@example.com",),
        subject="Verify your login",
        text="Use AB12-CD34 to continue"
        if scenario == "interpretation"
        else "Your verification code is 123456",
    )

    class Reader:
        calls = 0

        async def read(self, query):
            self.calls += 1
            if scenario == "delayed" and self.calls == 1:
                return ()
            if scenario == "ambiguous":
                return (message, message.model_copy(update={"message_id": "message-2"}))
            return (message,)

    reader = Reader()
    checks = []

    async def validate():
        checks.append(True)

    claims = VerificationClaimStore(settings)
    resolver = BrowserVerificationResolver(ceiling, reader, claims, validate)
    result = await resolver.resolve(
        owner,
        recipient="owner@example.com",
        not_before=initial.created_at if scenario == "replaced" else None,
    )
    assert checks
    if scenario in {"automatic", "delayed"}:
        assert result.state == "answered"
        assert (await owner.wait()).code == SecretStr("123456")
        assert await claims.claimed(message.account, message.message_id, scope=scope)
        if scenario == "delayed":
            assert reader.calls == 2
    elif scenario == "interpretation":
        assert result.state == "interpretation" and result.messages == (message,)
        with pytest.raises(ChallengeError, match="not present"):
            await resolver.accept(owner, message.message_id, SecretStr("guessed"))
        assert not await claims.claimed(message.account, message.message_id, scope=scope)
        assert await resolver.accept(owner, message.message_id, SecretStr("AB12-CD34"))
        assert (await owner.wait()).code == SecretStr("AB12-CD34")
        with pytest.raises(ChallengeError, match="no eligible"):
            await resolver.accept(owner, message.message_id, SecretStr("AB12-CD34"))
    else:
        assert result.state == ("ambiguous" if scenario == "ambiguous" else "unavailable")
        assert owner.record.state == "waiting_for_user"
        assert not await claims.claimed(message.account, message.message_id, scope=scope)


@pytest.mark.parametrize("failure", ["revoked", "expired", "cancelled", "concurrent"])
async def test_interpretation_rechecks_authority_and_single_consumption(failure, tmp_path):
    settings = configured().model_copy(update={"user_data_dir": str(tmp_path / "user")})
    scope = settings.resolve_profile_scope()
    store = BrowserChallengeStore(settings)

    async def create_owner():
        initial = record().model_copy(update={"id": "browser_challenge_" + uuid4().hex})
        await store.create(initial, scope=scope)
        return LiveBrowserChallenge(
            initial, writer=lambda value, revision: store.update(value, revision, scope=scope)
        )

    owner = await create_owner()
    ceiling = compile_verification_ceiling(settings, scope, background=True)
    assert ceiling is not None
    message = VerificationMessage(
        account=ceiling.sources[0].account,
        message_id="one-message",
        received_at=datetime.now(UTC),
        sender="verify@merchant.example",
        recipients=("owner@example.com",),
        subject="Verification",
        text="AB12CD",
    )

    class Reader:
        async def read(self, query):
            return (message,)

    revoked = False

    async def validate():
        if revoked:
            raise ChallengeError("verification permission revoked")

    claims = VerificationClaimStore(settings)
    resolver = BrowserVerificationResolver(ceiling, Reader(), claims, validate)
    assert (await resolver.resolve(owner, recipient="owner@example.com")).state == "interpretation"
    answer = VerificationAnswer(
        challenge_id=owner.record.id, message_id=message.message_id, token_index=1
    )
    if failure == "concurrent":
        other = await create_owner()
        assert (
            await resolver.resolve(other, recipient="owner@example.com")
        ).state == "interpretation"
        outcomes = await asyncio.gather(
            resolver.answer(owner, answer),
            resolver.answer(other, answer.model_copy(update={"challenge_id": other.record.id})),
        )
        assert sorted(outcomes) == [False, True]
        assert sorted([owner.record.state, other.record.state]) == ["responded", "waiting_for_user"]
    else:
        if failure == "revoked":
            revoked = True
        elif failure == "cancelled":
            await owner.finish("cancelled")
        else:
            owner.record = owner.record.model_copy(
                update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
            )
        with pytest.raises(ChallengeError):
            await resolver.answer(owner, answer)
        assert not await claims.claimed(message.account, message.message_id, scope=scope)


def test_unrelated_numbers_are_not_automatically_interpreted_as_codes():
    from ricky.profiles import ProfileResourceRef

    message = VerificationMessage(
        account=ProfileResourceRef(profile="personal", name="mail"),
        message_id="newsletter",
        received_at=datetime.now(UTC),
        sender="security@merchant.example",
        recipients=("owner@example.com",),
        subject="Security newsletter 2026",
        text="Review your account security settings.",
    )
    assert extract_simple_code(message) is None
