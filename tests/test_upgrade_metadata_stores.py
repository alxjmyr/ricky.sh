"""Upgrade boundaries for the messaging, notification, and session stores."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from ricky.config import RickySettings
from ricky.messaging import upgrade as messaging_upgrade
from ricky.messaging.store import MessagingStore, MessagingStoreError
from ricky.messaging.upgrade import (
    MessagingUpgradeAdapter,
    create_current_messaging_store,
    inspect_messaging_store,
)
from ricky.notifications import upgrade as notifications_upgrade
from ricky.notifications.store import NotificationSchemaError, NotificationStore
from ricky.notifications.upgrade import (
    NotificationsUpgradeAdapter,
    create_current_notifications_store,
    inspect_notifications_store,
)
from ricky.sessions import upgrade as sessions_upgrade
from ricky.sessions.store import SessionSchemaError, SessionStore
from ricky.sessions.upgrade import (
    SessionsUpgradeAdapter,
    create_current_sessions_store,
    inspect_sessions_store,
)
from ricky.upgrades.models import AdapterInspection, MigrationStep
from ricky.upgrades.registry import UpgradeAdapter


@dataclass(frozen=True)
class StoreCase:
    name: str
    relative_path: Path
    metadata_key: str
    owned_table: str
    adapter: type[Any]
    inspect: Callable[[Path], AdapterInspection]
    create: Callable[[Path], None]
    store: type[Any]
    error: type[Exception]
    upgrade_module: ModuleType
    inspect_name: str
    upgrade_error: type[Exception]


CASES = (
    StoreCase(
        "messaging",
        Path("notifications/notifications.sqlite3"),
        "messaging_schema_version",
        "inbox_messages",
        MessagingUpgradeAdapter,
        inspect_messaging_store,
        create_current_messaging_store,
        MessagingStore,
        MessagingStoreError,
        messaging_upgrade,
        "inspect_messaging_store",
        messaging_upgrade.MessagingUpgradeError,
    ),
    StoreCase(
        "notifications",
        Path("notifications/notifications.sqlite3"),
        "schema_version",
        "notifications",
        NotificationsUpgradeAdapter,
        inspect_notifications_store,
        create_current_notifications_store,
        NotificationStore,
        NotificationSchemaError,
        notifications_upgrade,
        "inspect_notifications_store",
        notifications_upgrade.NotificationsUpgradeError,
    ),
    StoreCase(
        "sessions",
        Path("sessions/sessions.sqlite3"),
        "schema_version",
        "sessions",
        SessionsUpgradeAdapter,
        inspect_sessions_store,
        create_current_sessions_store,
        SessionStore,
        SessionSchemaError,
        sessions_upgrade,
        "inspect_sessions_store",
        sessions_upgrade.SessionsUpgradeError,
    ),
)


def _path(root: Path, case: StoreCase) -> Path:
    return root / case.relative_path


def _tables(path: Path) -> frozenset[str]:
    with sqlite3.connect(path) as connection:
        return frozenset(
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        )


def _assert_adapter(adapter: UpgradeAdapter) -> None:
    assert adapter.target_schema_version == 1


_CONCURRENT_CREATORS = 4


def _create_concurrently(create: Callable[[Path], None], path: Path) -> None:
    """Start every creator at the same instant so one of them loses the race."""

    barrier = threading.Barrier(_CONCURRENT_CREATORS)

    def attempt() -> None:
        barrier.wait()
        create(path)

    with ThreadPoolExecutor(max_workers=_CONCURRENT_CREATORS) as pool:
        for future in [pool.submit(attempt) for _ in range(_CONCURRENT_CREATORS)]:
            future.result()


def _lose_one_create_race(monkeypatch: pytest.MonkeyPatch, case: StoreCase) -> None:
    """Report 'absent' once so the next create reaches its exclusive transaction."""

    real = case.inspect
    seen = [0]

    def inspect(path: Path) -> AdapterInspection:
        found = real(path)
        seen[0] += 1
        return found.model_copy(update={"state": "absent"}) if seen[0] == 1 else found

    monkeypatch.setattr(case.upgrade_module, case.inspect_name, inspect)


def _drop_tables(path: Path, names: Sequence[str]) -> None:
    connection = sqlite3.connect(path)
    try:
        for name in names:
            connection.execute(f'DROP TABLE "{name}"')
        connection.commit()
    finally:
        connection.close()


def _schema_tables(path: Path) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            if str(row[0]) != "store_metadata" and not str(row[0]).startswith("sqlite_")
        ]
    finally:
        connection.close()


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_discovery_is_deterministic_read_only_and_does_not_create_parents(
    tmp_path: Path, case: StoreCase
) -> None:
    root = (tmp_path / "user").resolve()
    root.mkdir()
    first = _path(root, case)
    second = root / "extra" / first.name
    adapter = case.adapter((second, first, first))
    _assert_adapter(adapter)

    targets = adapter.discover(user_data_dir=root)

    assert tuple(Path(target.path) for target in targets) == tuple(sorted({first, second}))
    assert all(adapter.inspect(target).state == "absent" for target in targets)
    assert not first.parent.exists()
    assert not second.parent.exists()


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_create_current_preflight_verify_and_current_apply_are_idempotent(
    tmp_path: Path, case: StoreCase
) -> None:
    root = (tmp_path / "user").resolve()
    root.mkdir()
    path = _path(root, case)
    adapter = case.adapter((path,))
    target = adapter.discover(user_data_dir=root)[0]

    case.create(path)
    current = adapter.verify(target)
    before = path.read_bytes()
    preflight = adapter.preflight(current)
    step = MigrationStep(
        adapter_id=adapter.adapter_id,
        step_id=f"{adapter.adapter_id}.current",
        target_id=target.target_id,
        physical_path=target.physical_path,
        source_schema_version=1,
        target_schema_version=1,
    )

    adapter.apply(step)
    case.create(path)

    assert path.read_bytes() == before
    assert preflight.backup_paths == (str(path),)
    assert preflight.estimated_backup_bytes == path.stat().st_size
    assert current.state == "current"
    assert current.integrity_valid


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_future_schema_is_rejected_before_ordinary_open_can_run_ddl(
    tmp_path: Path, case: StoreCase
) -> None:
    root = (tmp_path / "user").resolve()
    path = _path(root, case)
    case.create(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE store_metadata SET value = '99' WHERE key = ?",
            (case.metadata_key,),
        )
    tables_before = _tables(path)
    bytes_before = path.read_bytes()

    inspection = case.inspect(path)
    store = case.store(RickySettings(user_data_dir=str(root)))
    with pytest.raises(case.error):
        store._initialize()

    assert inspection.state == "unsupported"
    assert inspection.found_schema_version == 99
    assert _tables(path) == tables_before
    assert path.read_bytes() == bytes_before


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_incomplete_schema_is_not_repaired_by_ordinary_open(
    tmp_path: Path, case: StoreCase
) -> None:
    root = (tmp_path / "user").resolve()
    path = _path(root, case)
    case.create(path)
    with sqlite3.connect(path) as connection:
        connection.execute(f'DROP TABLE "{case.owned_table}"')
    tables_before = _tables(path)

    inspection = case.inspect(path)
    store = case.store(RickySettings(user_data_dir=str(root)))
    with pytest.raises(case.error):
        store._initialize()

    assert inspection.state == "corrupt"
    assert _tables(path) == tables_before
    assert case.owned_table not in tables_before


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_malformed_sqlite_is_corrupt_and_never_replaced(tmp_path: Path, case: StoreCase) -> None:
    root = (tmp_path / "user").resolve()
    path = _path(root, case)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a sqlite database")
    before = path.read_bytes()

    inspection = case.inspect(path)
    with pytest.raises(case.error):
        case.store(RickySettings(user_data_dir=str(root)))._initialize()

    assert inspection.state == "corrupt"
    assert path.read_bytes() == before


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_current_inspection_and_ordinary_open_do_not_write(tmp_path: Path, case: StoreCase) -> None:
    root = (tmp_path / "user").resolve()
    path = _path(root, case)
    case.create(path)
    before = (path.read_bytes(), path.stat().st_mtime_ns)

    assert case.inspect(path).state == "current"
    case.store(RickySettings(user_data_dir=str(root)))._initialize()

    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()


@pytest.mark.parametrize(
    "order",
    ((MessagingStore, NotificationStore), (NotificationStore, MessagingStore)),
    ids=("messaging-first", "notifications-first"),
)
def test_messaging_and_notifications_safely_share_one_database(
    tmp_path: Path, order: Sequence[type[Any]]
) -> None:
    root = (tmp_path / "user").resolve()
    settings = RickySettings(user_data_dir=str(root))
    first, second = (store(settings) for store in order)

    first._initialize()
    assert _owner_states(first.db_path) == {order[0].__name__: "current"}
    second._initialize()

    assert inspect_messaging_store(first.db_path).state == "current"
    assert inspect_notifications_store(first.db_path).state == "current"
    with sqlite3.connect(first.db_path) as connection:
        metadata = dict(connection.execute("SELECT key, value FROM store_metadata"))
    assert metadata == {"messaging_schema_version": "1", "schema_version": "1"}


def _owner_states(path: Path) -> dict[str, str]:
    states = {
        "MessagingStore": inspect_messaging_store(path).state,
        "NotificationStore": inspect_notifications_store(path).state,
    }
    return {name: state for name, state in states.items() if state == "current"}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_concurrent_first_create_yields_one_current_store(tmp_path: Path, case: StoreCase) -> None:
    root = (tmp_path / "user").resolve()
    path = _path(root, case)

    _create_concurrently(case.create, path)

    inspection = case.inspect(path)
    assert inspection.state == "current"
    assert inspection.integrity_valid


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_create_current_accepts_a_store_another_creator_finished(
    tmp_path: Path, case: StoreCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = (tmp_path / "user").resolve()
    path = _path(root, case)
    case.create(path)
    before = path.read_bytes()
    _lose_one_create_race(monkeypatch, case)

    case.create(path)

    assert case.inspect(path).state == "current"
    assert path.read_bytes() == before


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_create_current_still_reports_a_partial_store_it_raced(
    tmp_path: Path, case: StoreCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = (tmp_path / "user").resolve()
    path = _path(root, case)
    case.create(path)
    _drop_tables(path, (case.owned_table,))
    before = path.read_bytes()
    _lose_one_create_race(monkeypatch, case)

    with pytest.raises(case.upgrade_error, match="partially present.*incomplete or invalid"):
        case.create(path)

    assert path.read_bytes() == before


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_create_current_still_reports_orphaned_metadata_it_raced(
    tmp_path: Path, case: StoreCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = (tmp_path / "user").resolve()
    path = _path(root, case)
    case.create(path)
    _drop_tables(path, _schema_tables(path))
    before = path.read_bytes()
    _lose_one_create_race(monkeypatch, case)

    with pytest.raises(case.upgrade_error, match="metadata appeared during create"):
        case.create(path)

    assert path.read_bytes() == before


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_concurrent_first_store_open_creates_one_current_store(
    tmp_path: Path, case: StoreCase
) -> None:
    settings = RickySettings(user_data_dir=str((tmp_path / "user").resolve()))
    stores = [case.store(settings) for _ in range(_CONCURRENT_CREATORS)]
    barrier = threading.Barrier(_CONCURRENT_CREATORS)

    def attempt(store: Any) -> None:
        barrier.wait()
        store._initialize()

    with ThreadPoolExecutor(max_workers=_CONCURRENT_CREATORS) as pool:
        for future in [pool.submit(attempt, store) for store in stores]:
            future.result()

    assert case.inspect(stores[0].db_path).state == "current"
