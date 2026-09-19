"""Challenge metadata is scoped, atomic, versioned, and rooted in user data."""

import asyncio
from pathlib import Path

import pytest

from authority_support import settings
from ricky.browser.challenge_store import BrowserChallengeStore
from ricky.browser.challenge_upgrade import BrowserChallengesUpgradeAdapter
from ricky.browser.challenges import ChallengeError
from ricky.profiles import ProfileScope
from test_browser_challenges import SOURCE, record


async def test_store_roundtrip_scope_and_separate_roots(tmp_path: Path) -> None:
    config = settings(tmp_path)
    store = BrowserChallengeStore(config)
    initial = record()
    scope = initial.binding.profile_scope
    await store.create(initial, scope=scope)
    assert await BrowserChallengeStore(config).get(initial.id, scope=scope) == initial
    assert store.root.is_relative_to(Path(config.user_data_dir))
    assert not (tmp_path / config.project_data_dir).exists()
    assert await store.list(scope=ProfileScope.create("shared")) == ()
    with pytest.raises(ChallengeError, match="profile scope"):
        await store.get(initial.id, scope=ProfileScope.create("shared"))
    assert (store.root / f"{initial.id}.json").stat().st_mode & 0o777 == 0o600


async def test_store_compare_and_swap_rejects_duplicate_claim(tmp_path: Path) -> None:
    store = BrowserChallengeStore(settings(tmp_path))
    initial = record(source=SOURCE)
    scope = initial.binding.profile_scope
    await store.create(initial, scope=scope)
    updated = initial.transition("responded")
    outcomes = await asyncio.gather(
        store.update(updated, 1, scope=scope),
        store.update(updated, 1, scope=scope),
        return_exceptions=True,
    )
    assert sum(isinstance(item, ChallengeError) for item in outcomes) == 1
    assert await store.get(initial.id, scope=scope) == updated
    with pytest.raises(ChallengeError):
        await store.update(
            updated.model_copy(update={"state": "waiting_for_user", "revision": 3}), 2, scope=scope
        )


async def test_upgrade_inventory_accepts_absence_and_validates_records(tmp_path: Path) -> None:
    store = BrowserChallengeStore(settings(tmp_path))
    adapter = BrowserChallengesUpgradeAdapter(store.root)
    (target,) = adapter.discover(user_data_dir=Path(store.settings.user_data_dir))
    assert adapter.inspect(target).state == "absent"
    assert not store.root.exists()
    initial = record()
    await store.create(initial, scope=initial.binding.profile_scope)
    assert adapter.inspect(target).state == "current"
    assert adapter.plan_steps(source_data_generation=1, target_data_generation=1) == ()
    assert adapter.preflight(adapter.inspect(target)).backup_paths == (str(store.root),)
    path = store.root / f"{initial.id}.json"
    path.write_text(path.read_text().replace('"version":1', '"version":2'))
    assert adapter.inspect(target).state == "corrupt"


@pytest.mark.parametrize("submitting", [False, True])
async def test_lost_owner_is_invalidated_without_replaying_response(
    tmp_path: Path,
    submitting: bool,
) -> None:
    store = BrowserChallengeStore(settings(tmp_path))
    initial = record(source=SOURCE)
    scope = initial.binding.profile_scope
    await store.create(initial, scope=scope)
    responded = initial.transition("responded")
    await store.update(responded, initial.revision, scope=scope)
    if submitting:
        await store.update(responded.transition("submitting"), responded.revision, scope=scope)
    await store.invalidate_owner("another-owner", scope=scope)
    current = await store.get(initial.id, scope=scope)
    assert current.state == ("submitting" if submitting else "responded")
    await store.invalidate_owner(initial.binding.owner_id, scope=scope)
    current = await store.get(initial.id, scope=scope)
    assert current.state == ("in_doubt" if submitting else "invalidated")
    await store.invalidate_owner(initial.binding.owner_id, scope=scope)
    assert await store.get(initial.id, scope=scope) == current
