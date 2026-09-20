"""Mail candidates cannot be reused across concurrent or restarted browser tasks."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from authority_support import settings
from browser_challenge_support import record
from ricky.browser.challenge_store import BrowserChallengeStore
from ricky.browser.challenge_upgrade import BrowserChallengesUpgradeAdapter
from ricky.browser.challenges import ChallengeError
from ricky.browser.verification_store import VerificationClaimStore, VerificationSourceClaim
from ricky.profiles import ProfileResourceRef, ProfileScope


async def test_source_claim_is_atomic_durable_scoped_and_in_upgrade_inventory(tmp_path):
    config = settings(tmp_path)
    owner = record()
    scope = owner.binding.profile_scope
    await BrowserChallengeStore(config).create(owner, scope=scope)
    store = VerificationClaimStore(config)
    claim = VerificationSourceClaim(
        profile_scope=scope,
        account=ProfileResourceRef(profile="personal", name="mail"),
        message_id="message-1",
        challenge_id=owner.id,
        claimed_at=datetime.now(UTC),
    )
    results = await asyncio.gather(store.claim(claim, scope=scope), store.claim(claim, scope=scope))
    assert sorted(results) == [False, True]
    restarted = VerificationClaimStore(config)
    assert await restarted.claimed(claim.account, claim.message_id, scope=scope)
    assert not await restarted.claim(claim, scope=scope)
    with pytest.raises(ChallengeError, match="scope"):
        await restarted.claimed(
            claim.account, claim.message_id, scope=ProfileScope.create("shared")
        )
    files = list(store.root.glob("*.json"))
    assert len(files) == 1 and files[0].stat().st_mode & 0o777 == 0o600
    assert store.root.is_relative_to(Path(config.user_data_dir))
    assert not (tmp_path / config.project_data_dir).exists()
    adapter = BrowserChallengesUpgradeAdapter(store.root.parent)
    target = adapter.discover(user_data_dir=Path(config.user_data_dir))[0]
    assert adapter.inspect(target).state == "current"
    files[0].write_text(files[0].read_text().replace('"version":1', '"version":2'))
    assert adapter.inspect(target).state == "corrupt"


async def test_terminal_challenge_cannot_claim_new_mail(tmp_path):
    config = settings(tmp_path)
    owner = record()
    scope = owner.binding.profile_scope
    challenges = BrowserChallengeStore(config)
    await challenges.create(owner, scope=scope)
    await challenges.update(owner.transition("cancelled"), owner.revision, scope=scope)
    claim = VerificationSourceClaim(
        profile_scope=scope,
        account=ProfileResourceRef(profile="personal", name="mail"),
        message_id="message-1",
        challenge_id=owner.id,
        claimed_at=datetime.now(UTC),
    )
    with pytest.raises(ChallengeError, match="no longer"):
        await VerificationClaimStore(config).claim(claim, scope=scope)
