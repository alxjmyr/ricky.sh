"""Owner-local SQLite upgrade boundaries for Phase 2 group B."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import SecretStr

from ricky.config import ProtectedValuesSettings, RickySettings
from ricky.gateway.store import GatewayStore, GatewayStoreError
from ricky.gateway.upgrade import SCHEMA_VERSION as GATEWAY_SCHEMA_VERSION
from ricky.gateway.upgrade import (
    GatewayUpgradeAdapter,
    create_current_gateway_store,
    inspect_gateway_store,
)
from ricky.jobs.store import SCHEMA_VERSION as JOBS_SCHEMA_VERSION
from ricky.jobs.store import JobStoreError
from ricky.jobs.upgrade import (
    JobsUpgradeAdapter,
    create_current_jobs_store,
    verify_jobs_store,
)
from ricky.protected_values.store import (
    SCHEMA_VERSION as PROTECTED_VALUES_SCHEMA_VERSION,
)
from ricky.protected_values.store import ProfileVaultStore, ProtectedValueStoreError
from ricky.protected_values.upgrade import ProtectedValuesUpgradeAdapter
from ricky.upgrades.registry import UpgradeAdapter


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings(
        user_data_dir=str(tmp_path / "user"),
        project_data_dir=str(tmp_path / "project" / ".ricky"),
        protected_values=ProtectedValuesSettings(
            enabled=True,
            argon2_iterations=1,
            argon2_lanes=1,
            argon2_memory_kib=8_192,
        ),
    )


def _snapshot(path: Path) -> tuple[bytes, int, int]:
    stat = path.stat()
    return path.read_bytes(), stat.st_mode, stat.st_mtime_ns


def _assert_protocol(adapter: UpgradeAdapter) -> None:
    assert adapter.adapter_id


_CONCURRENT_CREATORS = 4


def _create_concurrently(create: Callable[[Path], object], path: Path) -> None:
    """Start every creator at the same instant so one of them loses the race."""

    barrier = threading.Barrier(_CONCURRENT_CREATORS)

    def attempt() -> None:
        barrier.wait()
        create(path)

    with ThreadPoolExecutor(max_workers=_CONCURRENT_CREATORS) as pool:
        for future in [pool.submit(attempt) for _ in range(_CONCURRENT_CREATORS)]:
            future.result()


@pytest.mark.asyncio
async def test_gateway_absent_current_and_invalid_stores_are_never_rewritten(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = GatewayStore(settings)
    adapter = GatewayUpgradeAdapter((store.db_path,))
    _assert_protocol(adapter)

    target = adapter.discover(user_data_dir=tmp_path / "user")[0]
    assert adapter.inspect(target).state == "absent"
    assert not (tmp_path / "user").exists()

    await store.initialize()
    assert adapter.inspect(target).state == "current"
    before = _snapshot(store.db_path)
    await store.initialize()
    assert _snapshot(store.db_path) == before

    with sqlite3.connect(store.db_path) as database:
        database.execute("PRAGMA user_version = 999")
    before = _snapshot(store.db_path)
    assert adapter.inspect(target).state == "unsupported"
    with pytest.raises(GatewayStoreError, match="unsupported.*schema"):
        await store.initialize()
    assert _snapshot(store.db_path) == before


@pytest.mark.parametrize("kind", ["corrupt", "incomplete"])
def test_gateway_corrupt_and_incomplete_stores_are_read_only(
    tmp_path: Path,
    kind: str,
) -> None:
    path = (tmp_path / kind / "gateway.sqlite3").resolve()
    path.parent.mkdir(parents=True)
    if kind == "corrupt":
        path.write_bytes(b"not a sqlite database")
    else:
        with sqlite3.connect(path) as database:
            database.execute("PRAGMA user_version = 2")
    before = _snapshot(path)
    adapter = GatewayUpgradeAdapter((path,))
    target = adapter.discover(user_data_dir=tmp_path)[0]
    assert adapter.inspect(target).state == "corrupt"
    assert _snapshot(path) == before
    with pytest.raises(GatewayStoreError):
        adapter.preflight(adapter.inspect(target))


@pytest.mark.parametrize("version", range(JOBS_SCHEMA_VERSION))
def test_jobs_inspects_every_supported_old_version_without_writes(
    tmp_path: Path,
    version: int,
) -> None:
    path = (tmp_path / "user" / f"v{version}" / "runs.sqlite3").resolve()
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as database:
        if version > 0:
            database.execute("CREATE TABLE job_runs (id TEXT PRIMARY KEY)")
        database.execute(f"PRAGMA user_version = {version}")
    before = _snapshot(path)

    adapter = JobsUpgradeAdapter((path,))
    _assert_protocol(adapter)
    target = adapter.discover(user_data_dir=tmp_path / "user")[0]
    inspection = adapter.inspect(target)
    assert inspection.state == "migration_required"
    assert inspection.found_schema_version == version
    assert adapter.preflight(inspection).backup_paths == (str(path),)
    assert _snapshot(path) == before


@pytest.mark.parametrize("kind", ["future", "corrupt", "incomplete"])
def test_jobs_rejects_invalid_stores_without_writes(tmp_path: Path, kind: str) -> None:
    path = (tmp_path / "user" / kind / "runs.sqlite3").resolve()
    path.parent.mkdir(parents=True)
    if kind == "corrupt":
        path.write_bytes(b"not a sqlite database")
    else:
        with sqlite3.connect(path) as database:
            database.execute(
                "CREATE TABLE placeholder (id TEXT PRIMARY KEY)"
                if kind == "incomplete"
                else "CREATE TABLE job_runs (id TEXT PRIMARY KEY)"
            )
            database.execute(
                f"PRAGMA user_version = {JOBS_SCHEMA_VERSION if kind == 'incomplete' else 999}"
            )
    before = _snapshot(path)
    adapter = JobsUpgradeAdapter((path,))
    target = adapter.discover(user_data_dir=tmp_path / "user")[0]
    inspection = adapter.inspect(target)
    assert inspection.state in {"unsupported", "corrupt"}
    assert _snapshot(path) == before
    with pytest.raises(JobStoreError):
        adapter.preflight(inspection)


@pytest.mark.asyncio
async def test_locked_vault_migration_preserves_ciphertext_and_is_idempotent(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = ProfileVaultStore(settings, "personal")
    await store.initialize(SecretStr("test passphrase"))
    assert not store.unlocked
    ciphertext = b"opaque-ciphertext-must-not-change"
    with sqlite3.connect(store.path) as database:
        database.execute(
            """INSERT INTO protected_resources (
                name, profile, kind, label, description, fields_json, policy_json,
                revision, enabled, payload, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "locked-secret",
                "personal",
                "credential",
                "Locked",
                "test",
                "[]",
                "{}",
                1,
                1,
                ciphertext,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        database.execute("DROP TABLE protected_commits")
        database.execute("PRAGMA user_version = 1")

    with pytest.raises(ProtectedValueStoreError, match="unsupported.*schema"):
        await store.initialized()

    adapter = ProtectedValuesUpgradeAdapter((store.path,))
    _assert_protocol(adapter)
    target = adapter.discover(user_data_dir=tmp_path / "user")[0]
    inspection = adapter.inspect(target)
    assert inspection.state == "migration_required"
    assert not adapter.preflight(inspection).touches_encrypted_bytes
    (step,) = adapter.plan_steps(source_data_generation=1, target_data_generation=1)
    adapter.apply(step)
    adapter.apply(step)
    assert adapter.verify(target).state == "current"
    assert not store.unlocked

    with sqlite3.connect(store.path) as database:
        version = database.execute("PRAGMA user_version").fetchone()[0]
        stored = database.execute(
            "SELECT payload FROM protected_resources WHERE name = 'locked-secret'"
        ).fetchone()[0]
    assert version == PROTECTED_VALUES_SCHEMA_VERSION
    assert bytes(stored) == ciphertext


@pytest.mark.parametrize("kind", ["future", "corrupt", "incomplete"])
def test_protected_values_rejects_invalid_vaults_without_writes(
    tmp_path: Path,
    kind: str,
) -> None:
    path = (tmp_path / "user" / kind / "protected-values.sqlite3").resolve()
    path.parent.mkdir(parents=True)
    if kind == "corrupt":
        path.write_bytes(b"not a sqlite database")
    else:
        with sqlite3.connect(path) as database:
            database.execute("CREATE TABLE placeholder (id TEXT PRIMARY KEY)")
            database.execute(
                f"PRAGMA user_version = "
                f"{PROTECTED_VALUES_SCHEMA_VERSION if kind == 'incomplete' else 999}"
            )
    before = _snapshot(path)
    adapter = ProtectedValuesUpgradeAdapter((path,))
    target = adapter.discover(user_data_dir=tmp_path / "user")[0]
    inspection = adapter.inspect(target)
    assert inspection.state in {"unsupported", "corrupt"}
    assert _snapshot(path) == before
    with pytest.raises(ProtectedValueStoreError):
        adapter.preflight(inspection)


def test_discovery_of_absent_targets_does_not_create_parent_directories(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "missing-user-data").resolve()
    adapters: tuple[UpgradeAdapter, ...] = (
        GatewayUpgradeAdapter((root / "gateway.sqlite3",)),
        JobsUpgradeAdapter((root / "jobs" / "runs.sqlite3",)),
        ProtectedValuesUpgradeAdapter((root / "vault.sqlite3",)),
    )
    for adapter in adapters:
        targets = adapter.discover(user_data_dir=root)
        assert adapter.inspect(targets[0]).state == "absent"
    assert not root.exists()


def test_concurrent_gateway_create_current_yields_one_current_store(tmp_path: Path) -> None:
    path = (tmp_path / "user" / "gateway.sqlite3").resolve()

    _create_concurrently(create_current_gateway_store, path)

    inspection = inspect_gateway_store(path)
    assert inspection.exists
    assert inspection.schema_version == GATEWAY_SCHEMA_VERSION


def test_gateway_create_current_accepts_a_store_another_creator_finished(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "user" / "gateway.sqlite3").resolve()
    create_current_gateway_store(path)
    before = _snapshot(path)

    inspection = create_current_gateway_store(path)

    assert inspection.schema_version == GATEWAY_SCHEMA_VERSION
    assert _snapshot(path) == before


@pytest.mark.parametrize(
    ("kind", "match"),
    [
        ("unsupported", "already exists.*unsupported gateway schema version: 999"),
        ("corrupt", "already exists.*inspection failed"),
    ],
)
def test_gateway_create_current_still_rejects_an_invalid_existing_store(
    tmp_path: Path,
    kind: str,
    match: str,
) -> None:
    path = (tmp_path / "user" / kind / "gateway.sqlite3").resolve()
    if kind == "corrupt":
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not a sqlite database")
    else:
        create_current_gateway_store(path)
        with sqlite3.connect(path) as database:
            database.execute("PRAGMA user_version = 999")
    before = _snapshot(path)

    with pytest.raises(GatewayStoreError, match=match):
        create_current_gateway_store(path)

    assert _snapshot(path) == before


def test_concurrent_jobs_create_current_yields_one_current_ledger(tmp_path: Path) -> None:
    path = (tmp_path / "user" / "jobs" / "runs.sqlite3").resolve()

    _create_concurrently(create_current_jobs_store, path)

    inspection = verify_jobs_store(path)
    assert inspection.exists
    assert inspection.schema_version == JOBS_SCHEMA_VERSION


def test_jobs_create_current_accepts_a_ledger_another_creator_finished(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "user" / "jobs" / "runs.sqlite3").resolve()
    create_current_jobs_store(path)
    before = _snapshot(path)

    inspection = create_current_jobs_store(path)

    assert inspection.schema_version == JOBS_SCHEMA_VERSION
    assert _snapshot(path) == before


@pytest.mark.parametrize(
    ("kind", "match"),
    [
        ("unsupported", "already exists.*unsupported job run schema version: 1"),
        ("corrupt", "already exists.*inspection failed"),
    ],
)
def test_jobs_create_current_still_rejects_an_invalid_existing_ledger(
    tmp_path: Path,
    kind: str,
    match: str,
) -> None:
    path = (tmp_path / "user" / kind / "runs.sqlite3").resolve()
    path.parent.mkdir(parents=True)
    if kind == "corrupt":
        path.write_bytes(b"not a sqlite database")
    else:
        with sqlite3.connect(path) as database:
            database.execute("CREATE TABLE job_runs (id TEXT PRIMARY KEY)")
            database.execute("PRAGMA user_version = 1")
    before = _snapshot(path)

    with pytest.raises(JobStoreError, match=match):
        create_current_jobs_store(path)

    assert _snapshot(path) == before


@pytest.mark.asyncio
async def test_concurrent_first_gateway_store_open_creates_one_current_store(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    stores = (GatewayStore(settings), GatewayStore(settings))

    await asyncio.gather(*(store.initialize() for store in stores))

    inspection = inspect_gateway_store(stores[0].db_path)
    assert inspection.exists
    assert inspection.schema_version == GATEWAY_SCHEMA_VERSION
