"""Explicit schema lifecycle boundary for the gateway SQLite store."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from ricky.upgrades.models import AdapterInspection, AdapterPreflight, AdapterTarget, MigrationStep

SCHEMA_VERSION = 2

# A create that loses the absent -> current race must not report corruption.
# Re-inspect for a bounded window: the winner may still be writing its schema
# from another process when the collision surfaces here.
_CREATE_RACE_ATTEMPTS = 25
_CREATE_RACE_DELAY_SECONDS = 0.02
_SCHEMA = """
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    key_digest TEXT NOT NULL,
    conversation_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'archived', 'uncertain')),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX active_conversation_key
    ON conversations(key_digest) WHERE status = 'active';
CREATE INDEX conversations_status_updated
    ON conversations(status, updated_at DESC, id);
CREATE TABLE gateway_inbound_results (
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE RESTRICT,
    result_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'committed', 'failed', 'uncertain')),
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX gateway_results_conversation_started
    ON gateway_inbound_results(conversation_id, started_at DESC);
"""
_TABLES = frozenset({"conversations", "gateway_inbound_results"})
_COLUMNS = {
    "conversations": frozenset(
        {
            "id",
            "key_digest",
            "conversation_json",
            "status",
            "revision",
            "created_at",
            "updated_at",
        }
    ),
    "gateway_inbound_results": frozenset(
        {
            "message_id",
            "conversation_id",
            "result_json",
            "status",
            "started_at",
            "finished_at",
        }
    ),
}


class GatewayStoreInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    exists: bool
    schema_version: int | None = Field(default=None, ge=0)
    target_schema_version: int = SCHEMA_VERSION
    quick_check: str | None = None
    size: int = Field(ge=0)


class GatewayUpgradePreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    source_schema_version: int | None = Field(default=None, ge=0)
    target_schema_version: int = SCHEMA_VERSION
    migration_required: bool
    backup_required: bool
    backup_bytes: int = Field(ge=0)


def inspect_gateway_store(path: Path) -> GatewayStoreInspection:
    """Inspect one exact target without creating its parent or database."""

    selected = _canonical(path)
    if not selected.exists():
        return GatewayStoreInspection(path=str(selected), exists=False, size=0)
    if not selected.is_file():
        raise _gateway_error(f"gateway store path is not a regular file: {selected}")
    try:
        with _read_only(selected) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            check = connection.execute("PRAGMA quick_check").fetchone()
            quick_check = None if check is None else str(check[0])
            tables = _tables(connection)
            columns = {
                table: frozenset(
                    str(row[1])
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                )
                for table in _TABLES
            }
    except sqlite3.Error as exc:
        raise _gateway_error("gateway store inspection failed") from exc
    if quick_check != "ok":
        raise _gateway_error("gateway store integrity check failed")
    if version != SCHEMA_VERSION:
        raise _gateway_error(f"unsupported gateway schema version: {version}")
    if not tables >= _TABLES or columns != _COLUMNS:
        raise _gateway_error("gateway store current schema is incomplete")
    return GatewayStoreInspection(
        path=str(selected),
        exists=True,
        schema_version=version,
        quick_check=quick_check,
        size=selected.stat().st_size,
    )


def create_current_gateway_store(path: Path) -> GatewayStoreInspection:
    """Create the current gateway schema only when the exact target is absent."""

    selected = _canonical(path)
    selected.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staged = _staged_store(selected)
    try:
        connection = sqlite3.connect(staged)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is None or int(checkpoint[0]) != 0:
                raise _gateway_error("new gateway store WAL could not be checkpointed")
        finally:
            connection.close()
        Path(f"{staged}-wal").unlink(missing_ok=True)
        Path(f"{staged}-shm").unlink(missing_ok=True)
        staged.chmod(0o600)
        inspect_gateway_store(staged)
        try:
            # One atomic link publishes the finished store, so no observer, in
            # this process or another, can see a half-built target.
            os.link(staged, selected)
        except FileExistsError:
            # Another creator published first. Losing that race is benign only
            # when the store it left behind is current and valid.
            return _accept_current_after_create_race(
                selected, "gateway create-current target already exists"
            )
        return inspect_gateway_store(selected)
    finally:
        for candidate in (staged, Path(f"{staged}-wal"), Path(f"{staged}-shm")):
            candidate.unlink(missing_ok=True)


def _staged_store(path: Path) -> Path:
    """Return a private sibling path that holds the store before publishing."""

    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".new")
    os.close(descriptor)
    return Path(name)


def _accept_current_after_create_race(path: Path, detail: str) -> GatewayStoreInspection:
    """Accept a lost create race only when it left a current, valid store."""

    from ricky.gateway.store import GatewayStoreError

    attempts = 0
    while True:
        attempts += 1
        try:
            inspection = inspect_gateway_store(path)
        except GatewayStoreError as exc:
            failure = str(exc)
        else:
            if inspection.exists:
                return inspection
            failure = "gateway create-current target disappeared"
        if attempts >= _CREATE_RACE_ATTEMPTS:
            raise _gateway_error(f"{detail}: {failure}")
        time.sleep(_CREATE_RACE_DELAY_SECONDS)


def verify_gateway_store(path: Path) -> GatewayStoreInspection:
    return inspect_gateway_store(path)


class GatewayUpgradeAdapter:
    """Gateway has no historical migration path in the first registry."""

    adapter_id = "gateway"

    def __init__(self, paths: tuple[Path, ...]) -> None:
        self.paths = tuple(sorted((_canonical(path) for path in paths), key=str))

    @property
    def supported_source_schema_versions(self) -> frozenset[int]:
        return frozenset({SCHEMA_VERSION})

    @property
    def target_schema_version(self) -> int:
        return SCHEMA_VERSION

    def discover(self, *, user_data_dir: Path) -> tuple[AdapterTarget, ...]:
        root = user_data_dir.resolve()
        return tuple(self._target(index, path, root) for index, path in enumerate(self.paths))

    def inspect(self, target: AdapterTarget) -> AdapterInspection:
        from ricky.gateway.store import GatewayStoreError

        path = self._owned_path(target)
        try:
            found = inspect_gateway_store(path)
        except GatewayStoreError as exc:
            detail = str(exc)
            return AdapterInspection(
                target=target,
                state="unsupported" if "unsupported" in detail else "corrupt",
                target_schema_version=SCHEMA_VERSION,
                integrity_valid=False,
                detail=detail,
            )
        return AdapterInspection(
            target=target,
            state="current" if found.exists else "absent",
            found_schema_version=found.schema_version,
            target_schema_version=SCHEMA_VERSION,
            integrity_valid=True,
            detail="gateway store is current" if found.exists else "gateway store is absent",
        )

    def preflight(self, inspection: AdapterInspection) -> AdapterPreflight:
        self._owned_path(inspection.target)
        if inspection.state in {"unsupported", "corrupt", "migration_required"}:
            raise _gateway_error("gateway store cannot be upgraded by the current release")
        return AdapterPreflight(
            target=inspection.target,
            estimated_backup_bytes=0,
            backup_paths=(),
        )

    def plan_steps(
        self, *, source_data_generation: int, target_data_generation: int
    ) -> tuple[MigrationStep, ...]:
        if source_data_generation != target_data_generation:
            raise _gateway_error("gateway has no data-generation migration path")
        return ()

    def apply(self, step: MigrationStep) -> None:
        raise ValueError(f"gateway has no migration step: {step.step_id}")

    def verify(self, target: AdapterTarget) -> AdapterInspection:
        result = self.inspect(target)
        if result.state not in {"absent", "current"}:
            raise _gateway_error("gateway store did not verify at the target schema")
        return result

    def _target(self, index: int, path: Path, root: Path) -> AdapterTarget:
        if not path.is_relative_to(root):
            raise ValueError("gateway target escapes user_data_dir")
        return AdapterTarget(
            adapter_id=self.adapter_id,
            target_id=f"gateway_{index}",
            path=str(path),
            physical_path=str(path),
            kind="sqlite",
        )

    def _owned_path(self, target: AdapterTarget) -> Path:
        if target.adapter_id != self.adapter_id:
            raise ValueError("gateway target is owned by another adapter")
        path = Path(target.path)
        if path not in self.paths or target.physical_path != target.path:
            raise ValueError("unknown gateway target")
        return path


def _read_only(path: Path) -> sqlite3.Connection:
    immutable = "&immutable=1" if not Path(f"{path}-wal").exists() else ""
    uri = f"file:{quote(str(path))}?mode=ro{immutable}"
    connection = sqlite3.connect(uri, uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _tables(connection: sqlite3.Connection) -> frozenset[str]:
    return frozenset(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    )


def _canonical(path: Path) -> Path:
    selected = path.expanduser()
    if not selected.is_absolute():
        raise _gateway_error("gateway upgrade path must be absolute")
    return selected.resolve()


def _gateway_error(message: str) -> RuntimeError:
    from ricky.gateway.store import GatewayStoreError

    return GatewayStoreError(message)
