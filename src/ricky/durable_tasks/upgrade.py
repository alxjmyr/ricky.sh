"""Owner-local inspection and migration boundary for durable-task SQLite state."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from ricky.durable_tasks.store import _SCHEMA, SCHEMA_VERSION, TaskSchemaError
from ricky.upgrades.models import (
    AdapterInspection,
    AdapterPreflight,
    AdapterTarget,
    MigrationStep,
)

ADAPTER_ID = "durable_tasks"

# A create that loses the absent -> current race must not report corruption.
# Re-inspect for a bounded window: the winner may still be writing its schema
# from another process when the collision surfaces here.
_CREATE_RACE_ATTEMPTS = 25
_CREATE_RACE_DELAY_SECONDS = 0.02
_V1_SCHEMA_VERSION = 1
_TASK_COLUMNS = {
    "id",
    "title",
    "objective",
    "closure_criteria",
    "execution_mode",
    "status",
    "waiting_on",
    "current_summary",
    "next_action",
    "priority",
    "due_at",
    "completion_summary",
    "revision",
    "created_at",
    "updated_at",
    "completed_at",
    "cancelled_at",
    "lease_id",
    "lease_holder_session_id",
    "lease_epoch",
    "lease_acquired_at",
    "lease_expires_at",
}
_ACTIVITY_COLUMNS = {
    "id",
    "task_id",
    "kind",
    "authority",
    "executor_id",
    "session_id",
    "from_status",
    "to_status",
    "summary",
    "metadata_json",
    "task_revision",
    "created_at",
}
_CURRENT_SCHEMA = {
    "tasks": _TASK_COLUMNS,
    "task_activity": _ACTIVITY_COLUMNS,
    "task_tags": {"task_id", "tag"},
}
_V1_SCHEMA = {"tasks": _TASK_COLUMNS, "task_activity": _ACTIVITY_COLUMNS}
_V1_TO_V2_SQL = """
BEGIN IMMEDIATE;
CREATE TABLE task_tags (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (task_id, tag)
);
CREATE INDEX task_tags_tag_task_idx ON task_tags(tag, task_id);
CREATE TABLE task_activity_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    kind TEXT NOT NULL CHECK(kind IN
        ('created','claimed','lease_renewed','progressed','waiting','blocked',
         'completed','cancelled','reopened','released','lease_expired',
         'artifact_created','artifact_updated','tags_updated')),
    authority TEXT NOT NULL CHECK(authority IN
        ('agent_autonomy','joint_work','direct_user_instruction',
         'deterministic_user_command','system_recovery')),
    executor_id TEXT NOT NULL,
    session_id TEXT,
    from_status TEXT,
    to_status TEXT,
    summary TEXT NOT NULL CHECK(length(trim(summary)) > 0),
    metadata_json TEXT NOT NULL,
    task_revision INTEGER NOT NULL CHECK(task_revision >= 1),
    created_at TEXT NOT NULL
);
INSERT INTO task_activity_v2 SELECT * FROM task_activity;
DROP TABLE task_activity;
ALTER TABLE task_activity_v2 RENAME TO task_activity;
CREATE INDEX activity_task_id_idx ON task_activity(task_id, id DESC);
PRAGMA user_version = 2;
COMMIT;
"""


def inspect_durable_tasks_database(path: Path) -> AdapterInspection:
    """Inspect one task database without creating the database or its parents."""

    target = _target(path)
    if not path.exists():
        return _inspection(target, "absent", None, True, "durable task database is absent")
    if path.is_symlink() or not path.is_file():
        return _inspection(target, "corrupt", None, False, "durable task database is not a file")
    try:
        with _inspection_connection(path) as connection:
            version = _user_version(connection)
            if not _quick_check(connection):
                return _inspection(
                    target,
                    "corrupt",
                    version,
                    False,
                    "durable task database integrity check failed",
                )
            if version == SCHEMA_VERSION:
                if not _has_columns(connection, _CURRENT_SCHEMA):
                    return _inspection(
                        target,
                        "corrupt",
                        version,
                        False,
                        f"durable task schema version {SCHEMA_VERSION} is incomplete",
                    )
                state = "current"
                detail = "durable task database is current"
            elif version == _V1_SCHEMA_VERSION:
                if (
                    not _has_columns(connection, _V1_SCHEMA)
                    or _table_exists(connection, "task_tags")
                    or _table_exists(connection, "task_activity_v2")
                ):
                    return _inspection(
                        target,
                        "corrupt",
                        version,
                        False,
                        "durable task schema version 1 is incomplete",
                    )
                state = "migration_required"
                detail = "durable task schema requires migration from 1 to 2"
            else:
                return _inspection(
                    target,
                    "unsupported",
                    version,
                    True,
                    f"unsupported durable task schema version: {version}",
                )
    except (OSError, sqlite3.Error):
        return _inspection(
            target, "corrupt", None, False, "durable task database is corrupt or unreadable"
        )
    return _inspection(target, state, version, True, detail)


def create_current_durable_tasks_database(path: Path) -> None:
    """Create schema version 2 only when the database file is genuinely absent."""

    _require_canonical(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(path.parent, 0o700)
    staged = _staged_database(path)
    try:
        connection = sqlite3.connect(staged, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is None or int(checkpoint[0]) != 0:
                raise TaskSchemaError("new durable task WAL could not be checkpointed")
        finally:
            connection.close()
        Path(f"{staged}-wal").unlink(missing_ok=True)
        Path(f"{staged}-shm").unlink(missing_ok=True)
        if os.name == "posix":
            os.chmod(staged, 0o600)
        built = inspect_durable_tasks_database(staged)
        if built.state != "current":
            raise TaskSchemaError(built.detail)
        try:
            # One atomic link publishes the finished database, so no observer,
            # in this process or another, can see a half-built target.
            os.link(staged, path)
        except FileExistsError:
            # Another creator published first. Losing that race is benign only
            # when the database it left behind is current and valid.
            _accept_current_after_create_race(path, "durable task database already exists")
    finally:
        _remove_new_database(staged)


def _staged_database(path: Path) -> Path:
    """Return a private sibling path that holds the database before publishing."""

    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".new")
    os.close(descriptor)
    return Path(name)


def _accept_current_after_create_race(path: Path, detail: str) -> None:
    """Accept a lost create race only when it left a current, valid database."""

    inspection = inspect_durable_tasks_database(path)
    attempts = 1
    while inspection.state != "current" and attempts < _CREATE_RACE_ATTEMPTS:
        time.sleep(_CREATE_RACE_DELAY_SECONDS)
        inspection = inspect_durable_tasks_database(path)
        attempts += 1
    if inspection.state != "current":
        raise TaskSchemaError(f"{detail}: {inspection.detail}")


class DurableTasksUpgradeAdapter:
    """Upgrade adapter for explicit profile-owned durable-task database paths."""

    def __init__(self, paths: Sequence[Path]) -> None:
        self._paths = _canonical_paths(paths)

    @property
    def adapter_id(self) -> str:
        return ADAPTER_ID

    @property
    def supported_source_schema_versions(self) -> frozenset[int]:
        return frozenset({_V1_SCHEMA_VERSION, SCHEMA_VERSION})

    @property
    def target_schema_version(self) -> int:
        return SCHEMA_VERSION

    def discover(self, *, user_data_dir: Path) -> tuple[AdapterTarget, ...]:
        root = _require_canonical(user_data_dir)
        if any(not path.is_relative_to(root) for path in self._paths):
            raise ValueError("durable task upgrade paths must stay under user_data_dir")
        return tuple(_target(path) for path in self._paths)

    def inspect(self, target: AdapterTarget) -> AdapterInspection:
        path = self._owned_path(target)
        inspected = inspect_durable_tasks_database(path)
        return inspected.model_copy(update={"target": target})

    def preflight(self, inspection: AdapterInspection) -> AdapterPreflight:
        path = self._owned_path(inspection.target)
        if inspection.state in {"corrupt", "unsupported"}:
            raise TaskSchemaError(inspection.detail)
        mutable = inspection.state == "migration_required"
        return AdapterPreflight(
            target=inspection.target,
            estimated_backup_bytes=_database_bytes(path) if mutable else 0,
            backup_paths=(str(path),) if mutable else (),
        )

    def plan_steps(
        self,
        *,
        source_data_generation: int,
        target_data_generation: int,
    ) -> tuple[MigrationStep, ...]:
        del source_data_generation, target_data_generation
        steps: list[MigrationStep] = []
        for path in self._paths:
            inspection = inspect_durable_tasks_database(path)
            if inspection.state == "migration_required":
                target = _target(path)
                steps.append(
                    MigrationStep(
                        adapter_id=ADAPTER_ID,
                        step_id=f"{target.target_id}.v1-to-v2",
                        target_id=target.target_id,
                        physical_path=str(path),
                        source_schema_version=_V1_SCHEMA_VERSION,
                        target_schema_version=SCHEMA_VERSION,
                    )
                )
            elif inspection.state in {"corrupt", "unsupported"}:
                raise TaskSchemaError(inspection.detail)
        return tuple(steps)

    def apply(self, step: MigrationStep) -> None:
        path = self._path_for_step(step)
        inspection = inspect_durable_tasks_database(path)
        if inspection.state == "current":
            return
        if inspection.state != "migration_required":
            raise TaskSchemaError(inspection.detail)
        try:
            with sqlite3.connect(path, isolation_level=None) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.executescript(_V1_TO_V2_SQL)
        except sqlite3.Error as exc:
            raise TaskSchemaError("durable task migration from 1 to 2 failed") from exc
        if os.name == "posix":
            for candidate in path.parent.glob(f"{path.name}*"):
                if candidate.is_file():
                    candidate.chmod(0o600)
        self.verify(_target(path))

    def verify(self, target: AdapterTarget) -> AdapterInspection:
        inspection = self.inspect(target)
        if inspection.state not in {"absent", "current"}:
            raise TaskSchemaError(inspection.detail)
        return inspection

    def _owned_path(self, target: AdapterTarget) -> Path:
        if target.adapter_id != ADAPTER_ID or target.kind != "sqlite":
            raise TaskSchemaError("upgrade target belongs to another adapter")
        path = Path(target.path)
        if (
            path not in self._paths
            or target.physical_path != str(path)
            or target.target_id != _target_id(path)
        ):
            raise TaskSchemaError("durable task upgrade target is not configured")
        return path

    def _path_for_step(self, step: MigrationStep) -> Path:
        if (
            step.adapter_id != ADAPTER_ID
            or step.physical_path is None
            or step.source_schema_version != _V1_SCHEMA_VERSION
            or step.target_schema_version != SCHEMA_VERSION
        ):
            raise TaskSchemaError("invalid durable task migration step")
        path = Path(step.physical_path)
        if path not in self._paths or step.target_id != _target_id(path):
            raise TaskSchemaError("durable task migration step is not configured")
        return path


def _canonical_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
    canonical = tuple(_require_canonical(path) for path in paths)
    if len(canonical) != len(set(canonical)):
        raise ValueError("durable task upgrade paths must be unique")
    return tuple(sorted(canonical))


def _require_canonical(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute() or expanded != expanded.resolve():
        raise ValueError("durable task upgrade paths must be absolute and canonical")
    return expanded


def _target_id(path: Path) -> str:
    return f"tasks-{hashlib.sha256(str(path).encode()).hexdigest()[:16]}"


def _target(path: Path) -> AdapterTarget:
    canonical = _require_canonical(path)
    return AdapterTarget(
        adapter_id=ADAPTER_ID,
        target_id=_target_id(canonical),
        path=str(canonical),
        physical_path=str(canonical),
        kind="sqlite",
    )


def _inspection(
    target: AdapterTarget,
    state: str,
    version: int | None,
    integrity: bool,
    detail: str,
) -> AdapterInspection:
    return AdapterInspection.model_validate(
        {
            "target": target,
            "state": state,
            "found_schema_version": version,
            "target_schema_version": SCHEMA_VERSION,
            "integrity_valid": integrity,
            "detail": detail,
        }
    )


@contextmanager
def _inspection_connection(path: Path) -> Iterator[sqlite3.Connection]:
    wal_path = Path(f"{path}-wal")
    if not wal_path.exists():
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro&immutable=1",
            uri=True,
            isolation_level=None,
        )
        try:
            connection.execute("PRAGMA query_only = ON")
            yield connection
        finally:
            connection.close()
        return

    # A read-only SQLite connection still writes WAL reader marks into the
    # source ``-shm`` file. Inspect a private snapshot when a live WAL exists
    # so upgrade checks remain byte-for-byte non-mutating.
    with tempfile.TemporaryDirectory(prefix="ricky-tasks-inspect-") as directory:
        snapshot = Path(directory) / path.name
        _copy_stable_database_snapshot(path, snapshot)
        connection = sqlite3.connect(snapshot, isolation_level=None)
        try:
            connection.execute("PRAGMA query_only = ON")
            yield connection
        finally:
            connection.close()


def _copy_stable_database_snapshot(source: Path, target: Path) -> None:
    source_wal = Path(f"{source}-wal")
    target_wal = Path(f"{target}-wal")
    for _attempt in range(10):
        try:
            database_before = source.stat()
            wal_before = source_wal.stat() if source_wal.exists() else None
            shutil.copyfile(source, target)
            if wal_before is not None:
                shutil.copyfile(source_wal, target_wal)
            else:
                target_wal.unlink(missing_ok=True)
            database_after = source.stat()
            wal_after = source_wal.stat() if source_wal.exists() else None
        except FileNotFoundError:
            continue
        database_identity = (
            database_before.st_dev,
            database_before.st_ino,
            database_before.st_size,
            database_before.st_mtime_ns,
        )
        database_after_identity = (
            database_after.st_dev,
            database_after.st_ino,
            database_after.st_size,
            database_after.st_mtime_ns,
        )
        wal_identity = (
            None
            if wal_before is None
            else (
                wal_before.st_dev,
                wal_before.st_ino,
                wal_before.st_size,
                wal_before.st_mtime_ns,
            )
        )
        wal_after_identity = (
            None
            if wal_after is None
            else (
                wal_after.st_dev,
                wal_after.st_ino,
                wal_after.st_size,
                wal_after.st_mtime_ns,
            )
        )
        if database_identity == database_after_identity and wal_identity == wal_after_identity:
            return
    raise sqlite3.OperationalError("durable task database changed during inspection")


def _user_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    if row is None:
        raise sqlite3.DatabaseError("missing schema version")
    return int(row[0])


def _quick_check(connection: sqlite3.Connection) -> bool:
    return [str(row[0]) for row in connection.execute("PRAGMA quick_check")] == ["ok"]


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _has_columns(connection: sqlite3.Connection, expected: dict[str, set[str]]) -> bool:
    return all(
        required.issubset(
            {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
        )
        for table, required in expected.items()
    )


def _database_bytes(path: Path) -> int:
    return sum(
        candidate.stat().st_size for candidate in (path, Path(f"{path}-wal")) if candidate.is_file()
    )


def _remove_new_database(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        candidate.unlink(missing_ok=True)
