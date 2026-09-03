"""Owner-local schema inspection and upgrade boundary for messaging state."""

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

ADAPTER_ID = "messaging"
SCHEMA_VERSION = 1
METADATA_KEY = "messaging_schema_version"

# A create that loses the absent -> current race must not report corruption.
# Re-inspect for a bounded window: the winner may still be writing its schema
# from another process when the collision surfaces here.
_CREATE_RACE_ATTEMPTS = 25
_CREATE_RACE_DELAY_SECONDS = 0.02

_TABLE_COLUMNS = {
    "inbox_messages": frozenset(
        {
            "id",
            "transport",
            "account",
            "update_id",
            "message_json",
            "status",
            "fence",
            "lease_owner",
            "lease_token",
            "lease_expires_at",
            "received_at",
            "updated_at",
        }
    ),
    "inbound_attempts": frozenset(
        {"id", "transport", "account", "update_id", "activity_json", "created_at"}
    ),
    "transport_cursors": frozenset({"transport", "account", "value", "updated_at"}),
    "poller_leases": frozenset({"transport", "account", "owner", "token", "fence", "expires_at"}),
    "delivery_parts": frozenset(
        {
            "outbox_id",
            "fence",
            "transport_message_id",
            "part_number",
            "message_json",
            "status",
            "platform_message_id",
            "error",
            "created_at",
            "updated_at",
        }
    ),
}

_CREATE_STATEMENTS = (
    """CREATE TABLE inbox_messages (
           id TEXT PRIMARY KEY,
           transport TEXT NOT NULL,
           account TEXT NOT NULL,
           update_id TEXT NOT NULL,
           message_json TEXT NOT NULL,
           status TEXT NOT NULL,
           fence INTEGER NOT NULL DEFAULT 0 CHECK (fence >= 0),
           lease_owner TEXT,
           lease_token TEXT,
           lease_expires_at TEXT,
           received_at TEXT NOT NULL,
           updated_at TEXT NOT NULL,
           UNIQUE(transport, account, update_id)
       )""",
    "CREATE INDEX inbox_status_received ON inbox_messages(status, received_at, id)",
    """CREATE TABLE inbound_attempts (
           id TEXT PRIMARY KEY,
           transport TEXT NOT NULL,
           account TEXT NOT NULL,
           update_id TEXT NOT NULL,
           activity_json TEXT NOT NULL,
           created_at TEXT NOT NULL
       )""",
    """CREATE INDEX inbound_attempt_update
       ON inbound_attempts(transport, account, update_id, created_at)""",
    """CREATE TABLE transport_cursors (
           transport TEXT NOT NULL,
           account TEXT NOT NULL,
           value TEXT NOT NULL,
           updated_at TEXT NOT NULL,
           PRIMARY KEY(transport, account)
       )""",
    """CREATE TABLE poller_leases (
           transport TEXT NOT NULL,
           account TEXT NOT NULL,
           owner TEXT NOT NULL,
           token TEXT NOT NULL,
           fence INTEGER NOT NULL CHECK (fence >= 1),
           expires_at TEXT NOT NULL,
           PRIMARY KEY(transport, account)
       )""",
    """CREATE TABLE delivery_parts (
           outbox_id TEXT NOT NULL,
           fence INTEGER NOT NULL,
           transport_message_id TEXT NOT NULL UNIQUE,
           part_number INTEGER NOT NULL,
           message_json TEXT NOT NULL,
           status TEXT NOT NULL,
           platform_message_id TEXT,
           error TEXT,
           created_at TEXT NOT NULL,
           updated_at TEXT NOT NULL,
           PRIMARY KEY(outbox_id, fence, part_number)
       )""",
)


class MessagingUpgradeError(RuntimeError):
    """Messaging state cannot be safely inspected, created, or migrated."""


def inspect_messaging_store(path: Path) -> AdapterInspection:
    """Inspect only messaging-owned metadata and tables without creating paths."""

    target = _target(path)
    if not path.exists():
        return _inspection(target, "absent", None, True, "messaging store is absent")
    if not path.is_file() or path.is_symlink():
        return _inspection(target, "corrupt", None, False, "messaging path is not a real file")
    try:
        with closing(_read_only(path)) as connection:
            quick = connection.execute("PRAGMA quick_check").fetchone()
            if quick is None or quick[0] != "ok":
                return _inspection(
                    target, "corrupt", None, False, "messaging SQLite quick_check failed"
                )
            tables = _tables(connection)
            owned = set(_TABLE_COLUMNS).intersection(tables)
            if "store_metadata" not in tables:
                state = "absent" if not owned else "corrupt"
                detail = (
                    "messaging store is absent from the shared database"
                    if not owned
                    else "messaging tables exist without store_metadata"
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
                    "messaging store is absent from the shared database"
                    if not owned
                    else "messaging tables exist without messaging schema metadata"
                )
                return _inspection(target, state, None, state == "absent", detail)
            version = _version(row[0])
            if version is None:
                return _inspection(
                    target, "corrupt", None, False, "messaging schema metadata is invalid"
                )
            if version != SCHEMA_VERSION:
                return _inspection(
                    target,
                    "unsupported",
                    version,
                    True,
                    f"unsupported messaging schema version: {version}",
                )
            invalid = _invalid_owned_tables(connection, tables)
            if invalid:
                return _inspection(
                    target,
                    "corrupt",
                    version,
                    False,
                    "messaging schema is incomplete or invalid: " + ", ".join(invalid),
                )
            return _inspection(target, "current", version, True, "messaging schema is current")
    except sqlite3.Error:
        return _inspection(target, "corrupt", None, False, "messaging SQLite is unreadable")


def create_current_messaging_store(path: Path) -> None:
    """Create the current messaging schema when this owner is genuinely absent."""

    before = inspect_messaging_store(path)
    if before.state == "current":
        return
    if before.state != "absent":
        raise MessagingUpgradeError(before.detail)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(path.parent, 0o700)
    collision = _create_messaging_schema(path)
    if collision is not None:
        _accept_current_after_create_race(path, collision)
        return
    if os.name == "posix":
        os.chmod(path, 0o600)
    verified = inspect_messaging_store(path)
    if verified.state != "current":
        raise MessagingUpgradeError(
            f"created messaging schema failed verification: {verified.detail}"
        )


def _create_messaging_schema(path: Path) -> str | None:
    """Write the schema, or return why another writer created this owner first."""

    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        tables = _tables(connection)
        owned = set(_TABLE_COLUMNS).intersection(tables)
        metadata_exists = "store_metadata" in tables
        if owned:
            connection.rollback()
            return "messaging schema became partially present during create"
        if not metadata_exists:
            connection.execute(
                "CREATE TABLE store_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
        elif _columns(connection, "store_metadata") != frozenset({"key", "value"}):
            raise MessagingUpgradeError("store_metadata has an invalid shape")
        row = connection.execute(
            "SELECT value FROM store_metadata WHERE key = ?", (METADATA_KEY,)
        ).fetchone()
        if row is not None:
            connection.rollback()
            return "messaging schema metadata appeared during create"
        for statement in _CREATE_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO store_metadata(key, value) VALUES (?, ?)",
            (METADATA_KEY, str(SCHEMA_VERSION)),
        )
        connection.commit()
        return None
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _accept_current_after_create_race(path: Path, detail: str) -> None:
    """Accept a lost create race only when it left a current, valid store."""

    inspection = inspect_messaging_store(path)
    attempts = 1
    while inspection.state != "current" and attempts < _CREATE_RACE_ATTEMPTS:
        time.sleep(_CREATE_RACE_DELAY_SECONDS)
        inspection = inspect_messaging_store(path)
        attempts += 1
    if inspection.state != "current":
        raise MessagingUpgradeError(f"{detail}: {inspection.detail}")


class MessagingUpgradeAdapter:
    """UpgradeAdapter implementation for explicit messaging SQLite paths."""

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
        return inspect_messaging_store(Path(target.path))

    def preflight(self, inspection: AdapterInspection) -> AdapterPreflight:
        _require_target(inspection.target)
        if inspection.state not in {"absent", "current", "migration_required"}:
            raise MessagingUpgradeError(inspection.detail)
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
            raise MessagingUpgradeError("messaging defines no cross-generation migration yet")
        return ()

    def apply(self, step: MigrationStep) -> None:
        if (
            step.adapter_id != ADAPTER_ID
            or step.source_schema_version != SCHEMA_VERSION
            or step.target_schema_version != SCHEMA_VERSION
            or step.physical_path is None
        ):
            raise MessagingUpgradeError("unsupported messaging migration step")
        self.verify(_target(Path(step.physical_path)))

    def verify(self, target: AdapterTarget) -> AdapterInspection:
        inspected = self.inspect(target)
        if inspected.state != "current" or not inspected.integrity_valid:
            raise MessagingUpgradeError(inspected.detail)
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
    canonical = tuple(sorted({_canonical_path(path) for path in paths}))
    return canonical


def _canonical_path(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute() or expanded != expanded.resolve():
        raise MessagingUpgradeError("messaging upgrade paths must be absolute and canonical")
    return expanded


def _canonical_root(path: Path) -> Path:
    root = _canonical_path(path)
    if not root.exists() or not root.is_dir() or root.is_symlink():
        raise MessagingUpgradeError("user_data_dir must be an existing real directory")
    return root


def _require_within(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise MessagingUpgradeError("messaging target is outside user_data_dir") from exc


def _require_target(target: AdapterTarget) -> None:
    if target.adapter_id != ADAPTER_ID or target.kind != "sqlite":
        raise MessagingUpgradeError("target is not owned by the messaging adapter")


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{quote(str(path), safe='/')}?mode=ro", uri=True)
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
    "MessagingUpgradeAdapter",
    "MessagingUpgradeError",
    "create_current_messaging_store",
    "inspect_messaging_store",
]
