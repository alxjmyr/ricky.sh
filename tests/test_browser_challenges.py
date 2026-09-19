"""Shared challenge ownership, response claims, expiry, and interruption."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr, ValidationError

from ricky.browser.challenges import (
    BrowserChallenge,
    ChallengeBinding,
    ChallengeError,
    ChallengeResponse,
    ChallengeSource,
    LiveBrowserChallenge,
)
from ricky.profiles import ProfileScope


def record(**updates) -> BrowserChallenge:
    now = datetime.now(UTC)
    return BrowserChallenge(
        id="browser_challenge_" + "a" * 32,
        binding=ChallengeBinding(
            owner_id="test-owner",
            profile_scope=ProfileScope.create("personal"),
            session_id="browser_session_" + "b" * 32,
            page_id="browser_page_" + "c" * 32,
            page_generation=2,
            top_level_origin="https://merchant.example",
            frame_origin="https://verify.example",
            occurrence_digest="d" * 64,
            purpose="authentication",
        ),
        kind="otp",
        instruction="Reply with the verification code.",
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        **updates,
    )


SOURCE = ChallengeSource(
    principal_id="telegram:personal/bot:sender",
    conversation_id="conversation",
    prompt_message_id="100",
)
RESPONSE = ChallengeResponse(code=SecretStr("synthetic-otp"))


class Journal:
    def __init__(self, initial: BrowserChallenge) -> None:
        self.records = [initial]

    async def write(self, updated: BrowserChallenge, expected: int) -> None:
        assert self.records[-1].revision == expected
        self.records.append(BrowserChallenge.model_validate_json(updated.model_dump_json()))


async def live() -> tuple[LiveBrowserChallenge, Journal]:
    initial = record()
    journal = Journal(initial)
    owner = LiveBrowserChallenge(initial, writer=journal.write)
    await owner.bind_source(SOURCE)
    return owner, journal


async def test_reply_is_claimed_once_and_is_not_resolution() -> None:
    owner, journal = await live()
    outcomes = await asyncio.gather(
        owner.respond(RESPONSE, source=SOURCE),
        owner.respond(RESPONSE, source=SOURCE),
        return_exceptions=True,
    )
    assert sum(isinstance(item, ChallengeError) for item in outcomes) == 1
    assert await owner.wait() is RESPONSE
    with pytest.raises(ChallengeError, match="available response"):
        await owner.wait()
    assert owner.record.state == "responded"
    await owner.begin_submission(owner.record.binding)
    await owner.finish("submitted")
    assert owner.record.state == "submitted"
    await owner.finish("resolved")
    assert owner.record.state == "resolved"
    assert "synthetic-otp" not in repr(journal.records)
    assert "synthetic-otp" not in "".join(item.model_dump_json() for item in journal.records)


@pytest.mark.parametrize("field", ["principal_id", "conversation_id", "prompt_message_id"])
async def test_foreign_reply_cannot_claim_pending_challenge(field: str) -> None:
    owner, _ = await live()
    with pytest.raises(ChallengeError, match="owner and prompt"):
        await owner.respond(RESPONSE, source=SOURCE.model_copy(update={field: "foreign"}))
    assert owner.record.state == "waiting_for_user"
    await owner.respond(RESPONSE, source=SOURCE)
    assert await owner.wait() is RESPONSE


async def test_expired_reply_never_becomes_available() -> None:
    owner, _ = await live()
    with pytest.raises(ChallengeError, match="expired"):
        await owner.respond(RESPONSE, source=SOURCE, now=owner.record.expires_at)
    with pytest.raises(ChallengeError):
        await owner.wait()
    assert owner.record.state == "expired"


async def test_live_binding_change_invalidates_response() -> None:
    owner, _ = await live()
    await owner.respond(RESPONSE, source=SOURCE)
    await owner.wait()
    with pytest.raises(ChallengeError, match="changed"):
        await owner.begin_submission(owner.record.binding.model_copy(update={"page_generation": 3}))
    assert owner.record.state == "invalidated"
    with pytest.raises(ChallengeError):
        await owner.begin_submission(owner.record.binding)


async def test_cancel_wait_drops_input_and_terminates_owner() -> None:
    owner, _ = await live()
    pending = asyncio.create_task(owner.wait())
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert owner.record.state == "cancelled"
    with pytest.raises(ChallengeError):
        await owner.respond(RESPONSE, source=SOURCE)


@pytest.mark.parametrize("loss", ["cancelled", "invalidated", "expired"])
async def test_loss_after_submission_begins_is_ambiguous(loss) -> None:
    owner, _ = await live()
    await owner.respond(RESPONSE, source=SOURCE)
    await owner.wait()
    await owner.begin_submission(owner.record.binding)
    await owner.finish(loss)
    assert owner.record.state == "in_doubt"
    with pytest.raises(ChallengeError):
        await owner.respond(RESPONSE, source=SOURCE)


async def test_cancellation_joins_publication_and_keeps_claim() -> None:
    owner, journal = await live()
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(updated: BrowserChallenge, revision: int) -> None:
        entered.set()
        await release.wait()
        await journal.write(updated, revision)

    owner._writer = delayed
    task = asyncio.create_task(owner.respond(RESPONSE, source=SOURCE))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert owner.record == journal.records[-1]
    assert owner.record.state == "responded"
    assert await owner.wait() is RESPONSE
    with pytest.raises(ChallengeError):
        await owner.respond(RESPONSE, source=SOURCE)


def test_durable_record_cannot_reconstruct_resident_response() -> None:
    initial = record()
    journal = Journal(initial)
    restored = BrowserChallenge.model_validate_json(
        initial.transition("responded").model_dump_json()
    )
    with pytest.raises(ChallengeError, match="persisted state"):
        LiveBrowserChallenge(restored, writer=journal.write)
    with pytest.raises(ValidationError):
        BrowserChallenge.model_validate({**initial.model_dump(), "code": "synthetic-otp"})


@pytest.mark.parametrize("origin", ["http://merchant.example", "https://merchant.example/path"])
def test_challenge_requires_exact_https_origins(origin: str) -> None:
    binding = record().binding.model_dump()
    binding["top_level_origin"] = origin
    with pytest.raises(ValidationError):
        ChallengeBinding.model_validate(binding)
