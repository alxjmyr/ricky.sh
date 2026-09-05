"""Atomic inbox, cursor, fencing, and delivery-part persistence tests."""

import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from ricky.config import MessagingSettings, RickySettings, TelegramAccountSettings
from ricky.messaging import MessagingStore, MessagingStoreError, PollerConflictError
from ricky.messaging.types import InboundMessage, ReceiveBatch, ReceivedUpdate, TransportCursor


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 11, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings(
        user_data_dir=str(tmp_path / "user"),
        messaging=MessagingSettings(
            telegram_accounts={
                "personal/bot": TelegramAccountSettings(
                    bot_token=SecretStr("test-token"),
                    allowed_sender_ids=["100"],
                    allowed_destination_ids=["200"],
                )
            }
        ),
    )


def _update(
    clock: MutableClock, update_id: str, *, message_id: str | None = None
) -> ReceivedUpdate:
    inbound = InboundMessage(
        id=message_id or f"inbound_{int(update_id):032x}",
        transport="telegram",
        account="personal",
        update_id=update_id,
        destination_id="200",
        sender_id="100",
        platform_message_id=update_id,
        text="hello",
        received_at=clock.now,
        status="pending",
    )
    return ReceivedUpdate(update_id=update_id, message=inbound)


def _batch(clock: MutableClock, *ids: str) -> ReceiveBatch:
    return ReceiveBatch(
        transport="telegram",
        account="personal",
        updates=[_update(clock, update_id) for update_id in ids],
        next_cursor=TransportCursor(transport="telegram", account="personal", value=ids[-1]),
    )


async def test_cursor_and_all_inbox_inserts_commit_atomically(tmp_path: Path) -> None:
    clock = MutableClock()
    store = MessagingStore(_settings(tmp_path), clock=clock)
    await store.initialize()
    duplicate_id = "inbound_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    first = _update(clock, "10", message_id=duplicate_id)
    second = _update(clock, "11", message_id=duplicate_id)
    batch = ReceiveBatch(
        transport="telegram",
        account="personal",
        updates=[first, second],
        next_cursor=TransportCursor(transport="telegram", account="personal", value="11"),
    )

    with pytest.raises(MessagingStoreError):
        await store.ingest(batch)

    assert await store.cursor("telegram", "personal") is None
    assert await store.list_inbox() == []


async def test_duplicate_update_does_not_duplicate_inbox_work(tmp_path: Path) -> None:
    clock = MutableClock()
    store = MessagingStore(_settings(tmp_path), clock=clock)
    await store.initialize()
    batch = _batch(clock, "10")

    assert len(await store.ingest(batch)) == 1
    assert await store.ingest(batch) == []
    assert len(await store.list_inbox()) == 1
    assert [item.outcome for item in await store.inbound_attempts("10")] == [
        "accepted",
        "duplicate",
    ]
    assert (await store.cursor("telegram", "personal")).value == "10"  # type: ignore[union-attr]


async def test_pending_selector_returns_global_oldest_before_newer_backlog(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = MessagingStore(_settings(tmp_path), clock=clock)
    await store.initialize()
    for update_id in ("10", "11", "12"):
        await store.ingest(_batch(clock, update_id))
        clock.now += timedelta(seconds=1)

    selected = await store.list_pending_oldest(limit=2)

    assert [item.update_id for item in selected] == ["10", "11"]


async def test_inbox_claim_is_fenced_and_persists_across_store_instances(tmp_path: Path) -> None:
    clock = MutableClock()
    settings = _settings(tmp_path)
    store = MessagingStore(settings, clock=clock)
    await store.initialize()
    message = (await store.ingest(_batch(clock, "10")))[0]
    claim = await store.claim_inbox(message.id, owner="gateway", lease_seconds=10)
    assert (await store.get_inbox(message.id)).status == "claimed"

    later = MessagingStore(settings, clock=clock)
    assert (await later.finish_inbox(claim, status="processed")).status == "processed"


async def test_operator_can_dismiss_pending_or_uncertain_inbox_work(tmp_path: Path) -> None:
    clock = MutableClock()
    store = MessagingStore(_settings(tmp_path), clock=clock)
    await store.initialize()
    pending, uncertain = await store.ingest(_batch(clock, "10", "11"))

    claim = await store.claim_inbox(uncertain.id, owner="gateway")
    await store.finish_inbox(claim, status="uncertain")

    assert (await store.dismiss_inbox(pending.id)).status == "processed"
    assert (await store.dismiss_inbox(uncertain.id)).status == "processed"
    assert await store.list_pending_oldest() == []


async def test_operator_cannot_dismiss_claimed_or_terminal_inbox_work(tmp_path: Path) -> None:
    clock = MutableClock()
    store = MessagingStore(_settings(tmp_path), clock=clock)
    await store.initialize()
    claimed, processed = await store.ingest(_batch(clock, "10", "11"))
    await store.claim_inbox(claimed.id, owner="gateway")
    terminal_claim = await store.claim_inbox(processed.id, owner="gateway")
    await store.finish_inbox(terminal_claim, status="processed")

    with pytest.raises(MessagingStoreError, match="not claimed"):
        await store.dismiss_inbox(claimed.id)
    with pytest.raises(MessagingStoreError, match="not processed"):
        await store.dismiss_inbox(processed.id)


async def test_only_one_poller_can_own_an_account(tmp_path: Path) -> None:
    clock = MutableClock()
    store = MessagingStore(_settings(tmp_path), clock=clock)
    await store.initialize()
    first = await store.acquire_poller("telegram", "personal", owner="one", lease_seconds=30)
    with pytest.raises(PollerConflictError, match="active poller"):
        await store.acquire_poller("telegram", "personal", owner="two", lease_seconds=30)
    await store.release_poller(first)
    second = await store.acquire_poller("telegram", "personal", owner="two", lease_seconds=30)
    assert second.fence == 1
    await store.release_poller(second)


async def test_expired_poller_can_be_reclaimed_with_higher_fence(tmp_path: Path) -> None:
    clock = MutableClock()
    store = MessagingStore(_settings(tmp_path), clock=clock)
    await store.initialize()
    first = await store.acquire_poller("telegram", "personal", owner="one", lease_seconds=1)
    clock.now += timedelta(seconds=2)
    second = await store.acquire_poller("telegram", "personal", owner="two", lease_seconds=1)
    assert second.fence == first.fence + 1


@pytest.mark.skipif(os.name != "posix", reason="private store modes are POSIX file modes")
async def test_reopening_a_loosened_store_restores_private_modes(tmp_path: Path) -> None:
    clock = MutableClock()
    settings = _settings(tmp_path)
    store = MessagingStore(settings, clock=clock)
    await store.initialize()
    await store.ingest(_batch(clock, "10"))
    # A live WAL connection materializes the sidecars, which inherit whatever
    # mode the database file carried when SQLite created them.
    holder = sqlite3.connect(store.db_path)
    try:
        holder.execute("SELECT COUNT(*) FROM inbox_messages").fetchone()
        sidecars = [Path(f"{store.db_path}-wal"), Path(f"{store.db_path}-shm")]
        assert [path for path in sidecars if path.is_file()] == sidecars
        store.root.chmod(0o755)
        for path in (store.db_path, *sidecars):
            path.chmod(0o644)

        await MessagingStore(settings, clock=clock).initialize()

        assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
        for path in (store.db_path, *sidecars):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    finally:
        holder.close()


@pytest.mark.skipif(os.name != "posix", reason="private store modes are POSIX file modes")
async def test_reopening_tolerates_a_disappearing_sqlite_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock()
    settings = _settings(tmp_path)
    store = MessagingStore(settings, clock=clock)
    await store.initialize()
    store.root.chmod(0o755)
    store.db_path.chmod(0o644)
    real_chmod = os.chmod
    real_is_file = Path.is_file

    def sidecar_was_present(path: Path) -> bool:
        return str(path).endswith("-wal") or real_is_file(path)

    def disappearing_sidecar(path: os.PathLike[str] | str, mode: int) -> None:
        if str(path).endswith("-wal"):
            raise FileNotFoundError(path)
        real_chmod(path, mode)

    monkeypatch.setattr(Path, "is_file", sidecar_was_present)
    monkeypatch.setattr(os, "chmod", disappearing_sidecar)

    await MessagingStore(settings, clock=clock).initialize()

    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.db_path.stat().st_mode) == 0o600
