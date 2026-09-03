"""Owner-local inspection and migration boundary for execution SQLite state."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from ricky.executions.store import (
    _BROWSER_APPROVAL_SCHEMA,
    _SCHEMA,
    SCHEMA_VERSION,
    ExecutionStoreError,
)
from ricky.upgrades.models import (
    AdapterInspection,
    AdapterPreflight,
    AdapterTarget,
    MigrationStep,
)

ADAPTER_ID = "executions"

# A create that loses the absent -> current race must not report corruption.
# Re-inspect for a bounded window: the winner may still be writing its schema
# from another process when the collision surfaces here.
_CREATE_RACE_ATTEMPTS = 25
_CREATE_RACE_DELAY_SECONDS = 0.02
_V7_SCHEMA_VERSION = 7
_V7_TO_V8_SQL = _BROWSER_APPROVAL_SCHEMA.replace(
    "COMMIT;",
    f"PRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;",
)
_BASE_SCHEMA = {
    "execution_requests": {
        "id",
        "kind",
        "status",
        "named_job",
        "job_digest",
        "project_root_ref",
        "goal",
        "contract_id",
        "contract_digest",
        "task_id",
        "task_revision",
        "profile_scope_json",
        "source_conversation_id",
        "source_message_id",
        "grant_id",
        "notification_route",
        "request_key",
        "parent_request_id",
        "created_at",
        "not_before",
        "expires_at",
        "claimed_by",
        "claim_token",
        "claim_fence",
        "claim_expires_at",
        "run_id",
        "error",
    },
    "execution_activities": {
        "id",
        "request_id",
        "kind",
        "from_status",
        "to_status",
        "worker_id",
        "summary",
        "fence",
        "created_at",
    },
    "execution_resolutions": {
        "id",
        "request_id",
        "disposition",
        "actor",
        "note",
        "created_at",
    },
    "execution_drafts": {
        "id",
        "conversation_id",
        "task_id",
        "status",
        "revision",
        "updated_at",
        "expires_at",
        "data_json",
    },
    "execution_draft_sources": {"draft_id", "message_id", "source_json"},
    "execution_draft_guardrail_fields": {
        "draft_id",
        "capability_id",
        "field_name",
        "source_message_id",
        "field_json",
    },
    "execution_draft_activity": {
        "id",
        "draft_id",
        "kind",
        "from_status",
        "to_status",
        "revision",
        "summary",
        "created_at",
    },
    "execution_contracts": {
        "id",
        "digest",
        "task_id",
        "created_at",
        "expires_at",
        "data_json",
    },
    "execution_contract_confirmations": {
        "id",
        "draft_id",
        "draft_revision",
        "summary_digest",
        "expires_at",
        "data_json",
    },
}
_BROWSER_SCHEMA = {
    "execution_browser_approvals": {
        "id",
        "request_id",
        "kind",
        "state",
        "revision",
        "logical_effect_key",
        "expires_at",
        "data_json",
    },
    "execution_browser_attestations": {
        "id",
        "transaction_id",
        "request_id",
        "disposition",
        "actor_principal_id",
        "source_conversation_id",
        "source_message_id",
        "note",
        "created_at",
    },
}


def inspect_executions_database(path: Path) -> AdapterInspection:
    """Inspect one execution database without creating it or changing journal state."""

    target = _target(path)
    if not path.exists():
        return _inspection(target, "absent", None, True, "execution database is absent")
    if path.is_symlink() or not path.is_file():
        return _inspection(target, "corrupt", None, False, "execution database is not a file")
    try:
        with _read_only_connection(path) as connection:
            version = _user_version(connection)
            if not _quick_check(connection):
                return _inspection(
                    target,
                    "corrupt",
                    version,
                    False,
                    "execution database integrity check failed",
                )
            if version == SCHEMA_VERSION:
                if not _has_columns(connection, _BASE_SCHEMA | _BROWSER_SCHEMA):
                    return _inspection(
                        target,
                        "corrupt",
                        version,
                        False,
                        f"execution schema version {SCHEMA_VERSION} is incomplete",
                    )
                state = "current"
                detail = "execution database is current"
            elif version == _V7_SCHEMA_VERSION:
                if not _has_columns(connection, _BASE_SCHEMA) or any(
                    _table_exists(connection, table) for table in _BROWSER_SCHEMA
                ):
                    return _inspection(
                        target,
                        "corrupt",
                        version,
                        False,
                        "execution schema version 7 is incomplete or partially migrated",
                    )
                state = "migration_required"
                detail = "execution schema requires migration from 7 to 8"
            else:
                return _inspection(
                    target,
                    "unsupported",
                    version,
                    True,
                    f"unsupported execution schema version: {version}",
                )
    except (OSError, sqlite3.Error):
        return _inspection(
            target, "corrupt", None, False, "execution database is corrupt or unreadable"
        )
    return _inspection(target, state, version, True, detail)


def create_current_executions_database(path: Path) -> None:
    """Create schema version 8 only when the database file is genuinely absent."""

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
        built = inspect_executions_database(staged)
        if built.state != "current":
            raise ExecutionStoreError(built.detail)
        try:
            # One atomic link publishes the finished database, so no observer,
            # in this process or another, can see a half-built target.
            os.link(staged, path)
        except FileExistsError:
            # Another creator published first. Losing that race is benign only
            # when the database it left behind is current and valid.
            _accept_current_after_create_race(path, "execution database already exists")
    finally:
        _remove_new_database(staged)


def _staged_database(path: Path) -> Path:
    """Return a private sibling path that holds the database before publishing."""

    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".new")
    os.close(descriptor)
    return Path(name)


def _accept_current_after_create_race(path: Path, detail: str) -> None:
    """Accept a lost create race only when it left a current, valid database."""

    inspection = inspect_executions_database(path)
    attempts = 1
    while inspection.state != "current" and attempts < _CREATE_RACE_ATTEMPTS:
        time.sleep(_CREATE_RACE_DELAY_SECONDS)
        inspection = inspect_executions_database(path)
        attempts += 1
    if inspection.state != "current":
        raise ExecutionStoreError(f"{detail}: {inspection.detail}")


class ExecutionsUpgradeAdapter:
    """Upgrade adapter for explicitly configured execution database paths."""

    def __init__(self, paths: Sequence[Path]) -> None:
        self._paths = _canonical_paths(paths)

    @property
    def adapter_id(self) -> str:
        return ADAPTER_ID

    @property
    def supported_source_schema_versions(self) -> frozenset[int]:
        return frozenset({_V7_SCHEMA_VERSION, SCHEMA_VERSION})

    @property
    def target_schema_version(self) -> int:
        return SCHEMA_VERSION

    def discover(self, *, user_data_dir: Path) -> tuple[AdapterTarget, ...]:
        root = _require_canonical(user_data_dir)
        if any(not path.is_relative_to(root) for path in self._paths):
            raise ValueError("execution upgrade paths must stay under user_data_dir")
        return tuple(_target(path) for path in self._paths)

    def inspect(self, target: AdapterTarget) -> AdapterInspection:
        path = self._owned_path(target)
        inspected = inspect_executions_database(path)
        return inspected.model_copy(update={"target": target})

    def preflight(self, inspection: AdapterInspection) -> AdapterPreflight:
        path = self._owned_path(inspection.target)
        if inspection.state in {"corrupt", "unsupported"}:
            raise ExecutionStoreError(inspection.detail)
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
            inspection = inspect_executions_database(path)
            if inspection.state == "migration_required":
                target = _target(path)
                steps.append(
                    MigrationStep(
                        adapter_id=ADAPTER_ID,
                        step_id=f"{target.target_id}.v7-to-v8",
                        target_id=target.target_id,
                        physical_path=str(path),
                        source_schema_version=_V7_SCHEMA_VERSION,
                        target_schema_version=SCHEMA_VERSION,
                    )
                )
            elif inspection.state in {"corrupt", "unsupported"}:
                raise ExecutionStoreError(inspection.detail)
        return tuple(steps)

    def apply(self, step: MigrationStep) -> None:
        path = self._path_for_step(step)
        inspection = inspect_executions_database(path)
        if inspection.state == "current":
            return
        if inspection.state != "migration_required":
            raise ExecutionStoreError(inspection.detail)
        try:
            with sqlite3.connect(path, isolation_level=None) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.executescript(_V7_TO_V8_SQL)
        except sqlite3.Error as exc:
            raise ExecutionStoreError("execution migration from 7 to 8 failed") from exc
        if os.name == "posix":
            for candidate in path.parent.glob(f"{path.name}*"):
                if candidate.is_file():
                    candidate.chmod(0o600)
        self.verify(_target(path))

    def verify(self, target: AdapterTarget) -> AdapterInspection:
        inspection = self.inspect(target)
        if inspection.state not in {"absent", "current"}:
            raise ExecutionStoreError(inspection.detail)
        return inspection

    def _owned_path(self, target: AdapterTarget) -> Path:
        if target.adapter_id != ADAPTER_ID or target.kind != "sqlite":
            raise ExecutionStoreError("upgrade target belongs to another adapter")
        path = Path(target.path)
        if (
            path not in self._paths
            or target.physical_path != str(path)
            or target.target_id != _target_id(path)
        ):
            raise ExecutionStoreError("execution upgrade target is not configured")
        return path

    def _path_for_step(self, step: MigrationStep) -> Path:
        if (
            step.adapter_id != ADAPTER_ID
            or step.physical_path is None
            or step.source_schema_version != _V7_SCHEMA_VERSION
            or step.target_schema_version != SCHEMA_VERSION
        ):
            raise ExecutionStoreError("invalid execution migration step")
        path = Path(step.physical_path)
        if path not in self._paths or step.target_id != _target_id(path):
            raise ExecutionStoreError("execution migration step is not configured")
        return path


def _canonical_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
    canonical = tuple(_require_canonical(path) for path in paths)
    if len(canonical) != len(set(canonical)):
        raise ValueError("execution upgrade paths must be unique")
    return tuple(sorted(canonical))


def _require_canonical(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute() or expanded != expanded.resolve():
        raise ValueError("execution upgrade paths must be absolute and canonical")
    return expanded


def _target_id(path: Path) -> str:
    return f"executions-{hashlib.sha256(str(path).encode()).hexdigest()[:16]}"


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
