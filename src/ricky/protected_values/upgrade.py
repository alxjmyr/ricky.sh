"""Explicit ciphertext-preserving schema migration for protected-value vaults."""

from __future__ import annotations

import sqlite3
from contextlib import suppress
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from ricky.upgrades.models import AdapterInspection, AdapterPreflight, AdapterTarget, MigrationStep

SCHEMA_VERSION = 2
_SUPPORTED_OLD_VERSIONS = frozenset({1})
_CURRENT_TABLES = frozenset(
    {
        "vault_header",
        "unlock_slots",
        "protected_resources",
        "destination_approvals",
        "protected_uses",
        "protected_commits",
    }
)
_PROTECTED_COMMITS_SCHEMA = """
CREATE TABLE protected_commits (
    id TEXT PRIMARY KEY,
    resource_name TEXT REFERENCES protected_resources(name) ON DELETE SET NULL,
    execution_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    request_json TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (
        disposition IN ('reserved', 'performed', 'not_performed', 'in_doubt')
    ),
    created_at TEXT NOT NULL,
    finalized_at TEXT
)
"""


class ProtectedValuesInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    exists: bool
    schema_version: int | None = Field(default=None, ge=0)
    target_schema_version: int = SCHEMA_VERSION
    quick_check: str | None = None
    size: int = Field(ge=0)


class ProtectedValuesUpgradePreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    source_schema_version: int | None = Field(default=None, ge=0)
    target_schema_version: int = SCHEMA_VERSION
    migration_required: bool
    backup_required: bool
    backup_bytes: int = Field(ge=0)
    preserves_ciphertext: bool = True


def inspect_protected_values_store(
    path: Path,
    *,
    allow_supported_old: bool = True,
) -> ProtectedValuesInspection:
    """Inspect schema and integrity without unlocking or creating any path."""

    from ricky.protected_values.store import ProtectedValueStoreError

    selected = _canonical(path)
    if not selected.exists():
        return ProtectedValuesInspection(path=str(selected), exists=False, size=0)
    if not selected.is_file():
        raise _protected_values_error(f"protected-value store is not a file: {selected}")
    try:
        with _read_only(selected) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            checked = connection.execute("PRAGMA quick_check").fetchone()
            quick_check = None if checked is None else str(checked[0])
            tables = _tables(connection)
            _validate_source_shape(connection, version, tables)
    except ProtectedValueStoreError:
        raise
    except sqlite3.Error as exc:
        raise _protected_values_error("protected-value store inspection failed") from exc
    if quick_check != "ok":
        raise _protected_values_error("protected-value vault integrity check failed")
    accepted = {SCHEMA_VERSION, *(_SUPPORTED_OLD_VERSIONS if allow_supported_old else ())}
    if version not in accepted:
        raise _protected_values_error(f"unsupported protected-value schema version: {version}")
    return ProtectedValuesInspection(
        path=str(selected),
        exists=True,
        schema_version=version,
        quick_check=quick_check,
        size=selected.stat().st_size,
    )


def migrate_protected_values_store(path: Path) -> ProtectedValuesInspection:
    """Transactionally migrate one supported vault without reading ciphertext."""

    selected = _canonical(path)
    source = inspect_protected_values_store(selected)
    if not source.exists:
        return source
    if source.schema_version == SCHEMA_VERSION:
        return inspect_protected_values_store(selected, allow_supported_old=False)
    if source.schema_version != 1:
        raise _protected_values_error(
            f"unsupported protected-value schema version: {source.schema_version}"
        )
    connection = sqlite3.connect(selected, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version == SCHEMA_VERSION:
            connection.rollback()
            return inspect_protected_values_store(selected, allow_supported_old=False)
        if version != 1:
            raise _protected_values_error(
                f"protected-value schema changed during migration: {version}"
            )
        connection.execute(_PROTECTED_COMMITS_SCHEMA)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except BaseException:
        with suppress(sqlite3.Error):
            connection.rollback()
        raise
    finally:
        connection.close()
    return inspect_protected_values_store(selected, allow_supported_old=False)


def verify_protected_values_store(path: Path) -> ProtectedValuesInspection:
    return inspect_protected_values_store(path, allow_supported_old=False)


class ProtectedValuesUpgradeAdapter:
    adapter_id = "protected_values"

    def __init__(self, paths: tuple[Path, ...]) -> None:
        self.paths = tuple(sorted((_canonical(path) for path in paths), key=str))

    @property
    def supported_source_schema_versions(self) -> frozenset[int]:
        return frozenset({*_SUPPORTED_OLD_VERSIONS, SCHEMA_VERSION})

    @property
    def target_schema_version(self) -> int:
        return SCHEMA_VERSION

    def discover(self, *, user_data_dir: Path) -> tuple[AdapterTarget, ...]:
        root = user_data_dir.resolve()
        return tuple(self._target(index, path, root) for index, path in enumerate(self.paths))

    def inspect(self, target: AdapterTarget) -> AdapterInspection:
        from ricky.protected_values.store import ProtectedValueStoreError

        path = self._owned_path(target)
        try:
            found = inspect_protected_values_store(path)
        except ProtectedValueStoreError as exc:
            detail = str(exc)
            return AdapterInspection(
                target=target,
                state="unsupported" if "unsupported" in detail else "corrupt",
                target_schema_version=SCHEMA_VERSION,
                integrity_valid=False,
                detail=detail,
            )
        state = (
            "absent"
            if not found.exists
            else "current"
            if found.schema_version == SCHEMA_VERSION
            else "migration_required"
        )
        return AdapterInspection(
            target=target,
            state=state,
            found_schema_version=found.schema_version,
            target_schema_version=SCHEMA_VERSION,
            integrity_valid=True,
            detail=f"protected-value store is {state.replace('_', ' ')}",
        )

    def preflight(self, inspection: AdapterInspection) -> AdapterPreflight:
        path = self._owned_path(inspection.target)
        if inspection.state in {"unsupported", "corrupt"}:
            raise _protected_values_error("protected-value store cannot be upgraded safely")
        backup = inspection.state == "migration_required"
        return AdapterPreflight(
            target=inspection.target,
            estimated_backup_bytes=path.stat().st_size if backup else 0,
            backup_paths=(str(path),) if backup else (),
            touches_encrypted_bytes=False,
        )

    def plan_steps(
        self, *, source_data_generation: int, target_data_generation: int
    ) -> tuple[MigrationStep, ...]:
        if source_data_generation != target_data_generation:
            raise _protected_values_error("protected values has no data-generation migration path")
        steps: list[MigrationStep] = []
        for index, path in enumerate(self.paths):
            target = self._target(index, path, Path("/"))
            inspection = self.inspect(target)
            if inspection.state != "migration_required":
                continue
            assert inspection.found_schema_version is not None
            steps.append(
                MigrationStep(
                    adapter_id=self.adapter_id,
                    step_id=f"{target.target_id}_schema_1_to_2",
                    target_id=target.target_id,
                    physical_path=target.physical_path,
                    source_schema_version=inspection.found_schema_version,
                    target_schema_version=SCHEMA_VERSION,
                )
            )
        return tuple(steps)

    def apply(self, step: MigrationStep) -> None:
        target = self._step_target(step)
        if step.target_schema_version != SCHEMA_VERSION:
            raise _protected_values_error(
                "protected-value migration step has the wrong target schema"
            )
        found = inspect_protected_values_store(Path(target.path))
        if found.schema_version == SCHEMA_VERSION:
            return
        if found.schema_version != step.source_schema_version:
            raise _protected_values_error("protected-value schema changed after upgrade planning")
        migrate_protected_values_store(Path(target.path))

    def verify(self, target: AdapterTarget) -> AdapterInspection:
        result = self.inspect(target)
        if result.state not in {"absent", "current"}:
            raise _protected_values_error(
                "protected-value store did not verify at the target schema"
            )
        return result

    def _target(self, index: int, path: Path, root: Path) -> AdapterTarget:
        if not path.is_relative_to(root):
            raise ValueError("protected-value target escapes user_data_dir")
        return AdapterTarget(
            adapter_id=self.adapter_id,
            target_id=f"protected_values_{index}",
            path=str(path),
            physical_path=str(path),
            kind="sqlite",
        )

    def _owned_path(self, target: AdapterTarget) -> Path:
        if target.adapter_id != self.adapter_id:
            raise ValueError("protected-value target is owned by another adapter")
        path = Path(target.path)
        if path not in self.paths or target.physical_path != target.path:
            raise ValueError("unknown protected-value target")
        return path

    def _step_target(self, step: MigrationStep) -> AdapterTarget:
        if step.adapter_id != self.adapter_id or step.physical_path is None:
            raise ValueError("migration step is not owned by protected values")
        for index, path in enumerate(self.paths):
            if str(path) == step.physical_path and step.target_id == f"protected_values_{index}":
                return self._target(index, path, Path("/"))
        raise ValueError("unknown protected-value migration step target")


def _validate_source_shape(
    connection: sqlite3.Connection,
    version: int,
    tables: frozenset[str],
) -> None:
    required = (
        _CURRENT_TABLES if version == SCHEMA_VERSION else _CURRENT_TABLES - {"protected_commits"}
    )
    if version in {SCHEMA_VERSION, *_SUPPORTED_OLD_VERSIONS} and not required <= tables:
        raise _protected_values_error("protected-value store schema is incomplete")
    if version in {SCHEMA_VERSION, *_SUPPORTED_OLD_VERSIONS}:
        header = connection.execute("SELECT profile FROM vault_header WHERE id = 1").fetchone()
        slot = connection.execute(
            "SELECT id FROM unlock_slots WHERE id = 'interactive-passphrase'"
        ).fetchone()
        if header is None or slot is None:
            raise _protected_values_error("protected-value store schema is incomplete")


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
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
        raise _protected_values_error("protected-value upgrade path must be absolute")
    return selected.resolve()


def _protected_values_error(message: str) -> RuntimeError:
    from ricky.protected_values.store import ProtectedValueStoreError

    return ProtectedValueStoreError(message)
