"""Shared challenge ownership, response claims, expiry, and interruption."""

import asyncio

import pytest
from pydantic import ValidationError

from browser_challenge_support import (
    RESPONSE,
    SOURCE,
    Journal,
    live,
    record,
)
from ricky.browser.challenges import (
    BrowserChallenge,
    ChallengeBinding,
    ChallengeError,
    LiveBrowserChallenge,
)


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
