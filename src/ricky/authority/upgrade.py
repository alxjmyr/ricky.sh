"""Owner-local inspection and upgrade boundary for authority SQLite state."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from ricky.authority.store import _SCHEMA, SCHEMA_VERSION, AuthorityStoreError
from ricky.upgrades.models import (
    AdapterInspection,
    AdapterPreflight,
    AdapterTarget,
    MigrationStep,
)

ADAPTER_ID = "authority"

# A create that loses the absent -> current race must not report corruption.
# Re-inspect for a bounded window: the winner may still be writing its schema
# from another process when the collision surfaces here.
_CREATE_RACE_ATTEMPTS = 25
_CREATE_RACE_DELAY_SECONDS = 0.02
_REQUIRED_COLUMNS = {
    "delegation_grants": {
        "id",
        "source_json",
        "task_id",
        "task_revision",
        "profile_scope_json",
        "execution_request_id",
        "contract_id",
        "contract_digest",
        "confirmations_json",
        "scopes_json",
        "summary",
        "effect_call_limit",
        "financial_limit_minor",
        "currency",
        "issued_at",
        "expires_at",
        "status",
        "policy_digest",
    },
    "grant_activities": {
        "id",
        "grant_id",
        "kind",
        "capability",
        "tool_name",
        "action_id",
        "disposition",
        "summary",
        "created_at",
    },
}


def inspect_authority_database(path: Path) -> AdapterInspection:
    """Inspect one explicit path without creating a file, parent, or sidecar."""

    target = _standalone_target(path)
    if not path.exists():
        return _inspection(target, "absent", None, True, "authority database is absent")
    if path.is_symlink() or not path.is_file():
        return _inspection(target, "corrupt", None, False, "authority database is not a file")
    try:
        with _read_only_connection(path) as connection:
            version = _user_version(connection)
            if not _quick_check(connection):
                return _inspection(
                    target, "corrupt", version, False, "authority database integrity check failed"
                )
            if version != SCHEMA_VERSION:
                return _inspection(
                    target,
                    "unsupported",
                    version,
                    True,
                    f"unsupported authority schema version: {version}",
                )
            if not _has_columns(connection, _REQUIRED_COLUMNS):
                return _inspection(
                    target,
                    "corrupt",
                    version,
                    False,
                    f"authority schema version {SCHEMA_VERSION} is incomplete",
                )
    except (OSError, sqlite3.Error):
        return _inspection(
            target, "corrupt", None, False, "authority database is corrupt or unreadable"
        )
    return _inspection(target, "current", SCHEMA_VERSION, True, "authority database is current")


def create_current_authority_database(path: Path) -> None:
    """Create schema version 4 only when the database file is genuinely absent."""

    _require_canonical(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(path.parent, 0o700)
    staged = _staged_database(path)
    try:
        connection = sqlite3.connect(staged, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(_SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()
        finally:
            connection.close()
        if os.name == "posix":
            os.chmod(staged, 0o600)
        built = inspect_authority_database(staged)
        if built.state != "current":
            raise AuthorityStoreError(built.detail)
        try:
            # One atomic link publishes the finished database, so no observer,
            # in this process or another, can see a half-built target.
            os.link(staged, path)
        except FileExistsError:
            # Another creator published first. Losing that race is benign only
            # when the database it left behind is current and valid.
            _accept_current_after_create_race(path, "authority database already exists")
    finally:
        _remove_database(staged)


def _staged_database(path: Path) -> Path:
    """Return a private sibling path that holds the database before publishing."""

    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".new")
    os.close(descriptor)
    return Path(name)


def _remove_database(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        candidate.unlink(missing_ok=True)


def _accept_current_after_create_race(path: Path, detail: str) -> None:
    """Accept a lost create race only when it left a current, valid database."""

    inspection = inspect_authority_database(path)
    attempts = 1
    while inspection.state != "current" and attempts < _CREATE_RACE_ATTEMPTS:
        time.sleep(_CREATE_RACE_DELAY_SECONDS)
        inspection = inspect_authority_database(path)
        attempts += 1
    if inspection.state != "current":
        raise AuthorityStoreError(f"{detail}: {inspection.detail}")


class AuthorityUpgradeAdapter:
    """Upgrade adapter for explicitly configured authority database paths."""

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
        root = _require_canonical(user_data_dir)
        targets = tuple(_target(path, root) for path in self._paths)
        return tuple(sorted(targets, key=lambda item: item.target_id))

    def inspect(self, target: AdapterTarget) -> AdapterInspection:
        path = self._owned_path(target)
        inspected = inspect_authority_database(path)
        return inspected.model_copy(update={"target": target})

    def preflight(self, inspection: AdapterInspection) -> AdapterPreflight:
        self._owned_path(inspection.target)
        if inspection.state in {"corrupt", "unsupported"}:
            raise AuthorityStoreError(inspection.detail)
        mutable = inspection.state == "migration_required"
        path = Path(inspection.target.path)
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
        return ()

    def apply(self, step: MigrationStep) -> None:
        raise AuthorityStoreError(f"authority owns no migration step: {step.step_id}")

    def verify(self, target: AdapterTarget) -> AdapterInspection:
        inspection = self.inspect(target)
        if inspection.state not in {"absent", "current"}:
            raise AuthorityStoreError(inspection.detail)
        return inspection

    def _owned_path(self, target: AdapterTarget) -> Path:
        if target.adapter_id != ADAPTER_ID or target.kind != "sqlite":
            raise AuthorityStoreError("upgrade target belongs to another adapter")
        path = Path(target.path)
        if (
            path not in self._paths
            or target.physical_path != str(path)
            or target.target_id != _target_id(path)
        ):
            raise AuthorityStoreError("authority upgrade target is not configured")
        return path


def _canonical_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
    canonical = tuple(_require_canonical(path) for path in paths)
    if len(canonical) != len(set(canonical)):
        raise ValueError("authority upgrade paths must be unique")
    return tuple(sorted(canonical))


def _require_canonical(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute() or expanded != expanded.resolve():
        raise ValueError("authority upgrade paths must be absolute and canonical")
    return expanded


def _target(path: Path, root: Path) -> AdapterTarget:
    if not path.is_relative_to(root):
        raise ValueError("authority upgrade path must stay under user_data_dir")
    return AdapterTarget(
        adapter_id=ADAPTER_ID,
        target_id=_target_id(path),
        path=str(path),
        physical_path=str(path),
        kind="sqlite",
    )


def _standalone_target(path: Path) -> AdapterTarget:
    canonical = _require_canonical(path)
    return AdapterTarget(
        adapter_id=ADAPTER_ID,
        target_id=_target_id(canonical),
        path=str(canonical),
        physical_path=str(canonical),
        kind="sqlite",
    )


def _target_id(path: Path) -> str:
    return f"authority-{hashlib.sha256(str(path).encode()).hexdigest()[:16]}"


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


def _read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, isolation_level=None)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _user_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    if row is None:
        raise sqlite3.DatabaseError("missing schema version")
    return int(row[0])


def _quick_check(connection: sqlite3.Connection) -> bool:
    return [str(row[0]) for row in connection.execute("PRAGMA quick_check")] == ["ok"]


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
