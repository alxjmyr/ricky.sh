"""Owner-local schema inspection and upgrade boundary for persistent sessions."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from ricky.upgrades.models import (
    AdapterInspection,
    AdapterPreflight,
    AdapterTarget,
    MigrationStep,
)

ADAPTER_ID = "sessions"
SCHEMA_VERSION = 1
METADATA_KEY = "schema_version"

# A create that loses the absent -> current race must not report corruption.
# Re-inspect for a bounded window: the winner may still be writing its schema
# from another process when the collision surfaces here.
_CREATE_RACE_ATTEMPTS = 25
_CREATE_RACE_DELAY_SECONDS = 0.02

_TABLE_COLUMNS = {
    "sessions": frozenset(
        {
            "id",
            "schema_version",
            "session_json",
            "revision",
            "status",
            "created_at",
            "updated_at",
            "last_turn_id",
            "lease_owner",
            "lease_token",
            "lease_fence",
            "lease_acquired_at",
            "lease_expires_at",
        }
    ),
    "turns": frozenset(
        {
            "id",
            "session_id",
            "inbound_ref",
            "base_revision",
            "status",
            "started_at",
            "finished_at",
            "error",
        }
    ),
}

_CREATE_STATEMENTS = (
    """CREATE TABLE sessions (
           id TEXT PRIMARY KEY,
           schema_version INTEGER NOT NULL,
           session_json TEXT NOT NULL,
           revision INTEGER NOT NULL CHECK (revision >= 0),
           status TEXT NOT NULL CHECK (status IN ('active', 'archived', 'uncertain')),
           created_at TEXT NOT NULL,
           updated_at TEXT NOT NULL,
           last_turn_id TEXT,
           lease_owner TEXT,
           lease_token TEXT,
           lease_fence INTEGER NOT NULL DEFAULT 0,
           lease_acquired_at TEXT,
           lease_expires_at TEXT
       )""",
    """CREATE TABLE turns (
           id TEXT PRIMARY KEY,
           session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE RESTRICT,
           inbound_ref TEXT,
           base_revision INTEGER NOT NULL CHECK (base_revision >= 0),
           status TEXT NOT NULL CHECK (
               status IN ('running', 'committed', 'failed', 'uncertain')
           ),
           started_at TEXT NOT NULL,
           finished_at TEXT,
           error TEXT
       )""",
    "CREATE INDEX idx_sessions_status_updated ON sessions(status, updated_at DESC)",
    "CREATE INDEX idx_turns_session_started ON turns(session_id, started_at DESC)",
)


class SessionsUpgradeError(RuntimeError):
    """Session state cannot be safely inspected, created, or migrated."""


def inspect_sessions_store(path: Path) -> AdapterInspection:
    """Inspect only session-owned metadata and tables without creating paths."""

    target = _target(path)
    if not path.exists():
        return _inspection(target, "absent", None, True, "session store is absent")
    if not path.is_file() or path.is_symlink():
        return _inspection(target, "corrupt", None, False, "session path is not a real file")
    try:
        with closing(_read_only(path)) as connection:
            quick = connection.execute("PRAGMA quick_check").fetchone()
            if quick is None or quick[0] != "ok":
                return _inspection(
                    target, "corrupt", None, False, "session SQLite quick_check failed"
                )
            tables = _tables(connection)
            owned = set(_TABLE_COLUMNS).intersection(tables)
            if "store_metadata" not in tables:
                state = "absent" if not owned else "corrupt"
                detail = (
                    "session store is absent from the database"
                    if not owned
                    else "session tables exist without store_metadata"
                )
                return _inspection(target, state, None, state == "absent", detail)
            if _columns(connection, "store_metadata") != frozenset({"key", "value"}):
                return _inspection(
                    target, "corrupt", None, False, "store_metadata has an invalid shape"
                )
            row = connection.execute(
                "SELECT value FROM store_metadata WHERE key = ?", (METADATA_KEY,)
            ).fetchone()
            if row is None:
                state = "absent" if not owned else "corrupt"
                detail = (
                    "session store is absent from the database"
                    if not owned
                    else "session tables exist without session schema metadata"
                )
                return _inspection(target, state, None, state == "absent", detail)
            version = _version(row[0])
            if version is None:
                return _inspection(
                    target, "corrupt", None, False, "session schema metadata is invalid"
                )
            if version != SCHEMA_VERSION:
                return _inspection(
                    target,
                    "unsupported",
                    version,
                    True,
                    f"unsupported session schema version: {version}",
                )
            invalid = _invalid_owned_tables(connection, tables)
            if invalid:
                return _inspection(
                    target,
                    "corrupt",
                    version,
                    False,
                    "session schema is incomplete or invalid: " + ", ".join(invalid),
                )
            return _inspection(target, "current", version, True, "session schema is current")
    except sqlite3.Error:
        return _inspection(target, "corrupt", None, False, "session SQLite is unreadable")


def create_current_sessions_store(path: Path) -> None:
    """Create the current session schema when this owner is genuinely absent."""

    before = inspect_sessions_store(path)
    if before.state == "current":
        return
    if before.state != "absent":
        raise SessionsUpgradeError(before.detail)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(path.parent, 0o700)
    collision = _create_sessions_schema(path)
    if collision is not None:
        _accept_current_after_create_race(path, collision)
        return
    Path(f"{path}-wal").unlink(missing_ok=True)
    Path(f"{path}-shm").unlink(missing_ok=True)
    if os.name == "posix":
        os.chmod(path, 0o600)
    verified = inspect_sessions_store(path)
    if verified.state != "current":
        raise SessionsUpgradeError(f"created session schema failed verification: {verified.detail}")


def _create_sessions_schema(path: Path) -> str | None:
    """Write the schema, or return why another writer created this owner first."""

    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        # Establish WAL once while creation is exclusive. Ordinary concurrent
        # connections then inherit the persisted mode without racing a
        # mode-changing pragma. Unlike BEGIN IMMEDIATE this pragma does not
        # wait on the busy timeout, so a concurrent creator holding the write
        # lock makes it fail outright; retry for the same bounded window that
        # settles a lost race, and let the exclusive transaction below decide.
        attempts = 1
        while True:
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                break
            except sqlite3.OperationalError:
                if attempts >= _CREATE_RACE_ATTEMPTS:
                    connection.rollback()
                    return "session journal mode stayed locked during create"
                time.sleep(_CREATE_RACE_DELAY_SECONDS)
                attempts += 1
        connection.execute("BEGIN IMMEDIATE")
        tables = _tables(connection)
        owned = set(_TABLE_COLUMNS).intersection(tables)
        metadata_exists = "store_metadata" in tables
        if owned:
            connection.rollback()
            return "session schema became partially present during create"
        if not metadata_exists:
            connection.execute(
                "CREATE TABLE store_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
        elif _columns(connection, "store_metadata") != frozenset({"key", "value"}):
            raise SessionsUpgradeError("store_metadata has an invalid shape")
        row = connection.execute(
            "SELECT value FROM store_metadata WHERE key = ?", (METADATA_KEY,)
        ).fetchone()
        if row is not None:
            connection.rollback()
            return "session schema metadata appeared during create"
        for statement in _CREATE_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO store_metadata(key, value) VALUES (?, ?)",
            (METADATA_KEY, str(SCHEMA_VERSION)),
        )
        connection.commit()
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or int(checkpoint[0]) != 0:
            raise SessionsUpgradeError("new session store WAL could not be checkpointed")
        return None
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _accept_current_after_create_race(path: Path, detail: str) -> None:
    """Accept a lost create race only when it left a current, valid store."""

    inspection = inspect_sessions_store(path)
    attempts = 1
    while inspection.state != "current" and attempts < _CREATE_RACE_ATTEMPTS:
        time.sleep(_CREATE_RACE_DELAY_SECONDS)
        inspection = inspect_sessions_store(path)
        attempts += 1
    if inspection.state != "current":
        raise SessionsUpgradeError(f"{detail}: {inspection.detail}")


class SessionsUpgradeAdapter:
    """UpgradeAdapter implementation for explicit session SQLite paths."""

    def __init__(self, paths: Sequence[Path]) -> None:
        self._paths = _canonical_paths(paths)

    @property
    def adapter_id(self) -> str:
        return ADAPTER_ID

    @property
    def supported_source_schema_versions(self) -> frozenset[int]:
        return frozenset({SCHEMA_VERSION})

    @property
    def target_schema_version(self) -> int:
        return SCHEMA_VERSION

    def discover(self, *, user_data_dir: Path) -> tuple[AdapterTarget, ...]:
        root = _canonical_root(user_data_dir)
        for path in self._paths:
            _require_within(path, root)
        return tuple(_target(path) for path in self._paths)

    def inspect(self, target: AdapterTarget) -> AdapterInspection:
        _require_target(target)
        return inspect_sessions_store(Path(target.path))

    def preflight(self, inspection: AdapterInspection) -> AdapterPreflight:
        _require_target(inspection.target)
        if inspection.state not in {"absent", "current", "migration_required"}:
            raise SessionsUpgradeError(inspection.detail)
        path = Path(inspection.target.path)
        present = inspection.state != "absent"
        return AdapterPreflight(
            target=inspection.target,
            estimated_backup_bytes=path.stat().st_size if present else 0,
            backup_paths=(str(path),) if present else (),
        )

    def plan_steps(
        self, *, source_data_generation: int, target_data_generation: int
    ) -> tuple[MigrationStep, ...]:
        if source_data_generation != target_data_generation:
            raise SessionsUpgradeError("sessions defines no cross-generation migration yet")
        return ()

    def apply(self, step: MigrationStep) -> None:
        if (
            step.adapter_id != ADAPTER_ID
            or step.source_schema_version != SCHEMA_VERSION
            or step.target_schema_version != SCHEMA_VERSION
            or step.physical_path is None
        ):
            raise SessionsUpgradeError("unsupported session migration step")
        self.verify(_target(Path(step.physical_path)))

    def verify(self, target: AdapterTarget) -> AdapterInspection:
        inspected = self.inspect(target)
        if inspected.state != "current" or not inspected.integrity_valid:
            raise SessionsUpgradeError(inspected.detail)
        return inspected


def _target(path: Path) -> AdapterTarget:
    canonical = _canonical_path(path)
    digest = hashlib.sha256(str(canonical).encode()).hexdigest()[:16]
    return AdapterTarget(
        adapter_id=ADAPTER_ID,
        target_id=f"{ADAPTER_ID}.{digest}",
        path=str(canonical),
        physical_path=str(canonical),
        kind="sqlite",
    )


def _canonical_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
    return tuple(sorted({_canonical_path(path) for path in paths}))


def _canonical_path(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute() or expanded != expanded.resolve():
        raise SessionsUpgradeError("session upgrade paths must be absolute and canonical")
    return expanded


def _canonical_root(path: Path) -> Path:
    root = _canonical_path(path)
    if not root.exists() or not root.is_dir() or root.is_symlink():
        raise SessionsUpgradeError("user_data_dir must be an existing real directory")
    return root


def _require_within(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise SessionsUpgradeError("session target is outside user_data_dir") from exc


def _require_target(target: AdapterTarget) -> None:
    if target.adapter_id != ADAPTER_ID or target.kind != "sqlite":
        raise SessionsUpgradeError("target is not owned by the sessions adapter")


def _read_only(path: Path) -> sqlite3.Connection:
    # An idle WAL-mode database with no sidecars is complete in its main file.
    # Immutable mode keeps inspection from recreating `-wal`/`-shm`. If a WAL
    # exists after an interruption, ordinary read-only mode is required so its
    # committed frames participate in inspection.
    immutable = "&immutable=1" if not Path(f"{path}-wal").exists() else ""
    connection = sqlite3.connect(
        f"file:{quote(str(path), safe='/')}?mode=ro{immutable}",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _tables(connection: sqlite3.Connection) -> frozenset[str]:
    return frozenset(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    )


def _columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(row[1] for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _invalid_owned_tables(connection: sqlite3.Connection, tables: frozenset[str]) -> list[str]:
    invalid: list[str] = []
    for table, expected in _TABLE_COLUMNS.items():
        if table not in tables:
            invalid.append(f"missing {table}")
        elif _columns(connection, table) != expected:
            invalid.append(f"invalid {table}")
    return invalid


def _version(value: object) -> int | None:
    if not isinstance(value, str) or not value.isdigit():
        return None
    parsed = int(value)
    return parsed if str(parsed) == value else None


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


__all__ = [
    "SessionsUpgradeAdapter",
    "SessionsUpgradeError",
    "create_current_sessions_store",
    "inspect_sessions_store",
]
