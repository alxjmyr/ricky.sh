"""Explicit upgrade boundaries for authority, durable tasks, and executions."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ricky.authority.store import AuthorityStore, AuthorityStoreError
from ricky.authority.upgrade import (
    AuthorityUpgradeAdapter,
    create_current_authority_database,
    inspect_authority_database,
)
from ricky.config import RickySettings
from ricky.durable_tasks.store import DurableTaskStore, TaskSchemaError
from ricky.durable_tasks.upgrade import (
    DurableTasksUpgradeAdapter,
    create_current_durable_tasks_database,
    inspect_durable_tasks_database,
)
from ricky.executions.store import ExecutionStore, ExecutionStoreError
from ricky.executions.upgrade import (
    ExecutionsUpgradeAdapter,
    create_current_executions_database,
    inspect_executions_database,
)
from ricky.upgrades.models import AdapterInspection


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _version(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


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


def test_absent_adapter_discovery_and_planning_create_nothing(tmp_path: Path) -> None:
    root = tmp_path / "user"
    adapters = (
        AuthorityUpgradeAdapter((root / "authority" / "authority.sqlite3",)),
        DurableTasksUpgradeAdapter((root / "profiles" / "shared" / "tasks.sqlite3",)),
        ExecutionsUpgradeAdapter((root / "executions" / "executions.sqlite3",)),
    )

    for adapter in adapters:
        target = adapter.discover(user_data_dir=root)[0]
        inspection = adapter.inspect(target)
        assert inspection.state == "absent"
        assert adapter.preflight(inspection).backup_paths == ()
        assert adapter.plan_steps(source_data_generation=1, target_data_generation=1) == ()
        assert adapter.verify(target).state == "absent"

    assert not root.exists()


@pytest.mark.parametrize(
    ("name", "create", "inspect"),
    [
        ("authority", create_current_authority_database, inspect_authority_database),
        ("tasks", create_current_durable_tasks_database, inspect_durable_tasks_database),
        ("executions", create_current_executions_database, inspect_executions_database),
    ],
)
def test_current_inspection_is_structural_integrity_checked_and_read_only(
    tmp_path: Path,
    name: str,
    create: Callable[[Path], None],
    inspect: Callable[[Path], AdapterInspection],
) -> None:
    path = tmp_path / name / f"{name}.sqlite3"
    create(path)
    before = _snapshot(path.parent)

    inspection = inspect(path)

    assert inspection.state == "current"
    assert inspection.integrity_valid is True
    assert _snapshot(path.parent) == before


@pytest.mark.parametrize(
    ("name", "inspect"),
    [
        ("authority", inspect_authority_database),
        ("tasks", inspect_durable_tasks_database),
        ("executions", inspect_executions_database),
    ],
)
def test_corrupt_inspection_is_read_only_and_sanitized(
    tmp_path: Path,
    name: str,
    inspect: Callable[[Path], AdapterInspection],
) -> None:
    path = tmp_path / name / f"{name}.sqlite3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a sqlite database")
    before = _snapshot(path.parent)

    inspection = inspect(path)

    assert inspection.state == "corrupt"
    assert inspection.integrity_valid is False
    assert "SELECT" not in inspection.detail
    assert "PRAGMA" not in inspection.detail
    assert _snapshot(path.parent) == before


@pytest.mark.parametrize(
    ("name", "create", "inspect"),
    [
        ("authority", create_current_authority_database, inspect_authority_database),
        ("tasks", create_current_durable_tasks_database, inspect_durable_tasks_database),
        ("executions", create_current_executions_database, inspect_executions_database),
    ],
)
def test_future_schema_versions_are_unsupported_without_mutation(
    tmp_path: Path,
    name: str,
    create: Callable[[Path], None],
    inspect: Callable[[Path], AdapterInspection],
) -> None:
    path = tmp_path / name / f"{name}.sqlite3"
    create(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 999")
    before = _snapshot(path.parent)

    inspection = inspect(path)

    assert inspection.state == "unsupported"
    assert inspection.found_schema_version == 999
    assert _snapshot(path.parent) == before


@pytest.mark.parametrize(
    ("name", "create", "inspect", "table"),
    [
        (
            "authority",
            create_current_authority_database,
            inspect_authority_database,
            "grant_activities",
        ),
        (
            "tasks",
            create_current_durable_tasks_database,
            inspect_durable_tasks_database,
            "task_tags",
        ),
        (
            "executions",
            create_current_executions_database,
            inspect_executions_database,
            "execution_browser_attestations",
        ),
    ],
)
def test_incomplete_current_schemas_are_corrupt(
    tmp_path: Path,
    name: str,
    create: Callable[[Path], None],
    inspect: Callable[[Path], AdapterInspection],
    table: str,
) -> None:
    path = tmp_path / name / f"{name}.sqlite3"
    create(path)
    with sqlite3.connect(path) as connection:
        connection.execute(f"DROP TABLE {table}")

    inspection = inspect(path)

    assert inspection.state == "corrupt"
    assert "incomplete" in inspection.detail


async def test_existing_version_zero_files_are_not_treated_as_absent(tmp_path: Path) -> None:
    root = tmp_path / "user"
    settings = RickySettings(user_data_dir=str(root))
    authority = AuthorityStore(settings)
    executions = ExecutionStore(settings)
    tasks_path = root / "profiles" / "shared" / settings.durable_tasks.dir / "tasks.sqlite3"
    for path in (authority.db_path, executions.db_path, tasks_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(path).close()

    with pytest.raises(AuthorityStoreError, match="schema version: 0"):
        await authority.initialize()
    with pytest.raises(ExecutionStoreError, match="schema version: 0"):
        await executions.initialize()
    with pytest.raises(TaskSchemaError, match="schema version: 0"):
        await DurableTaskStore.create(settings, profile="shared")

    assert _version(authority.db_path) == 0
    assert _version(executions.db_path) == 0
    assert _version(tasks_path) == 0


async def test_durable_task_v1_requires_adapter_and_migration_preserves_rows(
    tmp_path: Path,
) -> None:
    root = tmp_path / "user"
    settings = RickySettings(user_data_dir=str(root))
    store = await DurableTaskStore.create(settings, profile="shared")
    task = await store.create_task(
        title="Preserve this task",
        objective="Prove explicit migration keeps durable state",
        closure_criteria="The task and activity still parse",
        execution_mode="user",
        authority="direct_user_instruction",
        executor_id="upgrade-test",
    )
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("DROP TABLE task_tags")
        connection.execute("PRAGMA user_version = 1")

    before_database = store.db_path.read_bytes()
    before_names = {path.name for path in store.db_path.parent.iterdir()}
    with pytest.raises(TaskSchemaError, match="requires migration"):
        await DurableTaskStore.create(settings, profile="shared")
    assert store.db_path.read_bytes() == before_database
    assert {path.name for path in store.db_path.parent.iterdir()} == before_names

    adapter = DurableTasksUpgradeAdapter((store.db_path,))
    target = adapter.discover(user_data_dir=root)[0]
    inspection = adapter.inspect(target)
    assert inspection.state == "migration_required"
    assert adapter.preflight(inspection).backup_paths == (str(store.db_path),)
    step = adapter.plan_steps(source_data_generation=1, target_data_generation=1)[0]
    adapter.apply(step)
    adapter.apply(step)

    reopened = await DurableTaskStore.create(settings, profile="shared")
    assert await reopened.get_task(task.id) == task
    assert [item.kind for item in await reopened.activities(task.id)] == ["created"]
    assert adapter.verify(target).state == "current"


@pytest.mark.parametrize(
    ("name", "create", "inspect"),
    [
        ("authority", create_current_authority_database, inspect_authority_database),
        ("tasks", create_current_durable_tasks_database, inspect_durable_tasks_database),
        ("executions", create_current_executions_database, inspect_executions_database),
    ],
)
def test_concurrent_first_create_yields_one_current_store(
    tmp_path: Path,
    name: str,
    create: Callable[[Path], None],
    inspect: Callable[[Path], AdapterInspection],
) -> None:
    path = tmp_path / name / f"{name}.sqlite3"

    _create_concurrently(create, path)

    inspection = inspect(path)
    assert inspection.state == "current"
    assert inspection.integrity_valid is True


@pytest.mark.parametrize(
    ("name", "create", "inspect"),
    [
        ("authority", create_current_authority_database, inspect_authority_database),
        ("tasks", create_current_durable_tasks_database, inspect_durable_tasks_database),
        ("executions", create_current_executions_database, inspect_executions_database),
    ],
)
def test_create_current_accepts_a_database_another_creator_finished(
    tmp_path: Path,
    name: str,
    create: Callable[[Path], None],
    inspect: Callable[[Path], AdapterInspection],
) -> None:
    path = tmp_path / name / f"{name}.sqlite3"
    create(path)
    before = _snapshot(path.parent)

    create(path)

    assert inspect(path).state == "current"
    assert _snapshot(path.parent) == before


@pytest.mark.parametrize(
    ("name", "create", "error", "table"),
    [
        ("authority", create_current_authority_database, AuthorityStoreError, "grant_activities"),
        ("tasks", create_current_durable_tasks_database, TaskSchemaError, "task_tags"),
        (
            "executions",
            create_current_executions_database,
            ExecutionStoreError,
            "execution_browser_attestations",
        ),
    ],
)
def test_create_current_still_reports_an_incomplete_existing_database(
    tmp_path: Path,
    name: str,
    create: Callable[[Path], None],
    error: type[Exception],
    table: str,
) -> None:
    path = tmp_path / name / f"{name}.sqlite3"
    create(path)
    with sqlite3.connect(path) as connection:
        connection.execute(f"DROP TABLE {table}")
    before = _snapshot(path.parent)

    with pytest.raises(error, match="already exists.*incomplete"):
        create(path)

    assert _snapshot(path.parent) == before


@pytest.mark.parametrize(
    ("name", "create", "error"),
    [
        ("authority", create_current_authority_database, AuthorityStoreError),
        ("tasks", create_current_durable_tasks_database, TaskSchemaError),
        ("executions", create_current_executions_database, ExecutionStoreError),
    ],
)
def test_create_current_still_reports_an_unsupported_existing_database(
    tmp_path: Path,
    name: str,
    create: Callable[[Path], None],
    error: type[Exception],
) -> None:
    path = tmp_path / name / f"{name}.sqlite3"
    create(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 999")
    before = _snapshot(path.parent)

    with pytest.raises(error, match="already exists.*schema version: 999"):
        create(path)

    assert _snapshot(path.parent) == before


async def test_concurrent_first_store_open_creates_one_current_database(
    tmp_path: Path,
) -> None:
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    authority = (AuthorityStore(settings), AuthorityStore(settings))
    executions = (ExecutionStore(settings), ExecutionStore(settings))

    await asyncio.gather(*(store.initialize() for store in authority))
    await asyncio.gather(*(store.initialize() for store in executions))

    assert inspect_authority_database(authority[0].db_path).state == "current"
    assert inspect_executions_database(executions[0].db_path).state == "current"
