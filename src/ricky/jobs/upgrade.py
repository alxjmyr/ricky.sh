"""Explicit inspection and historical migration boundary for the job ledger."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from ricky.upgrades.models import AdapterInspection, AdapterPreflight, AdapterTarget, MigrationStep

if TYPE_CHECKING:
    from ricky.jobs.store import JobRunStore

SCHEMA_VERSION = 12

# A create that loses the absent -> current race must not report corruption.
# Re-inspect for a bounded window: the winner may still be writing its schema
# from another process when the collision surfaces here.
_CREATE_RACE_ATTEMPTS = 25
_CREATE_RACE_DELAY_SECONDS = 0.02
_SUPPORTED_OLD_VERSIONS = frozenset(range(SCHEMA_VERSION))
_CURRENT_TABLES = frozenset(
    {
        "job_runs",
        "browser_attempts",
        "browser_attempt_budgets",
        "browser_navigation_checkpoints",
        "browser_action_evidence",
        "browser_budget_reservations",
    }
)


class JobsStoreInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    exists: bool
    schema_version: int | None = Field(default=None, ge=0)
    target_schema_version: int = SCHEMA_VERSION
    quick_check: str | None = None
    size: int = Field(ge=0)


class JobsUpgradePreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    source_schema_version: int | None = Field(default=None, ge=0)
    target_schema_version: int = SCHEMA_VERSION
    migration_required: bool
    backup_required: bool
    backup_bytes: int = Field(ge=0)


def inspect_jobs_store(
    path: Path,
    *,
    allow_supported_old: bool = True,
) -> JobsStoreInspection:
    """Inspect one ledger with a read-only SQLite connection."""

    selected = _canonical(path)
    if not selected.exists():
        return JobsStoreInspection(path=str(selected), exists=False, size=0)
    if not selected.is_file():
        raise _job_error(f"job run store path is not a regular file: {selected}")
    try:
        with _read_only(selected) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            checked = connection.execute("PRAGMA quick_check").fetchone()
            quick_check = None if checked is None else str(checked[0])
            tables = _tables(connection)
    except sqlite3.Error as exc:
        raise _job_error("job run store inspection failed") from exc
    if quick_check != "ok":
        raise _job_error("job run store integrity check failed")
    accepted = {SCHEMA_VERSION, *(_SUPPORTED_OLD_VERSIONS if allow_supported_old else ())}
    if version not in accepted:
        raise _job_error(f"unsupported job run schema version: {version}")
    if version == SCHEMA_VERSION and not tables >= _CURRENT_TABLES:
        raise _job_error("job run store current schema is incomplete")
    if version > 0 and "job_runs" not in tables:
        raise _job_error("job run store source schema is incomplete")
    if version == 0 and tables - {"sqlite_sequence"}:
        raise _job_error("job run version-zero store contains unknown schema")
    return JobsStoreInspection(
        path=str(selected),
        exists=True,
        schema_version=version,
        quick_check=quick_check,
        size=selected.stat().st_size,
    )


def create_current_jobs_store(path: Path) -> JobsStoreInspection:
    """Create a current ledger only at one absent canonical target."""

    selected = _canonical(path)
    selected.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(selected, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another creator won the exclusive create. Losing that race is benign
        # only when the ledger it left behind is current and valid.
        return _accept_current_after_create_race(
            selected, "job create-current target already exists"
        )
    os.close(descriptor)
    try:
        _path_store(selected)._migrate_or_create_sync()
        return verify_jobs_store(selected)
    except BaseException:
        for candidate in (selected, Path(f"{selected}-wal"), Path(f"{selected}-shm")):
            candidate.unlink(missing_ok=True)
        raise


def migrate_jobs_store(path: Path) -> JobsStoreInspection:
    """Apply the owner-supported, step-transactional migration chain."""

    selected = _canonical(path)
    source = inspect_jobs_store(selected)
    if not source.exists or source.schema_version == SCHEMA_VERSION:
        return source
    _path_store(selected)._migrate_or_create_sync()
    return verify_jobs_store(selected)


def _accept_current_after_create_race(path: Path, detail: str) -> JobsStoreInspection:
    """Accept a lost create race only when it left a current, valid ledger."""

    from ricky.jobs.store import JobStoreError

    attempts = 0
    while True:
        attempts += 1
        try:
            inspection = verify_jobs_store(path)
        except JobStoreError as exc:
            failure = str(exc)
        else:
            if inspection.exists:
                return inspection
            failure = "job create-current target disappeared"
        if attempts >= _CREATE_RACE_ATTEMPTS:
            raise _job_error(f"{detail}: {failure}")
        time.sleep(_CREATE_RACE_DELAY_SECONDS)


def verify_jobs_store(path: Path) -> JobsStoreInspection:
    return inspect_jobs_store(path, allow_supported_old=False)


class JobsUpgradeAdapter:
    adapter_id = "jobs"

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
        from ricky.jobs.store import JobStoreError

        path = self._owned_path(target)
        try:
            found = inspect_jobs_store(path)
        except JobStoreError as exc:
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
            detail=f"job run store is {state.replace('_', ' ')}",
        )

    def preflight(self, inspection: AdapterInspection) -> AdapterPreflight:
        path = self._owned_path(inspection.target)
        if inspection.state in {"unsupported", "corrupt"}:
            raise _job_error("job run store cannot be upgraded safely")
        backup = inspection.state == "migration_required"
        return AdapterPreflight(
            target=inspection.target,
            estimated_backup_bytes=path.stat().st_size if backup else 0,
            backup_paths=(str(path),) if backup else (),
        )

    def plan_steps(
        self, *, source_data_generation: int, target_data_generation: int
    ) -> tuple[MigrationStep, ...]:
        if source_data_generation != target_data_generation:
            raise _job_error("jobs has no data-generation migration path")
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
                    step_id=(
                        f"{target.target_id}_schema_"
                        f"{inspection.found_schema_version}_to_{SCHEMA_VERSION}"
                    ),
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
            raise _job_error("job migration step has the wrong target schema")
        found = inspect_jobs_store(Path(target.path))
        if found.schema_version == SCHEMA_VERSION:
            return
        if found.schema_version != step.source_schema_version:
            raise _job_error("job run schema changed after upgrade planning")
        migrate_jobs_store(Path(target.path))

    def verify(self, target: AdapterTarget) -> AdapterInspection:
        result = self.inspect(target)
        if result.state not in {"absent", "current"}:
            raise _job_error("job run store did not verify at the target schema")
        return result

    def _target(self, index: int, path: Path, root: Path) -> AdapterTarget:
        if not path.is_relative_to(root):
            raise ValueError("jobs target escapes user_data_dir")
        return AdapterTarget(
            adapter_id=self.adapter_id,
            target_id=f"jobs_{index}",
            path=str(path),
            physical_path=str(path),
            kind="sqlite",
        )

    def _owned_path(self, target: AdapterTarget) -> Path:
        if target.adapter_id != self.adapter_id:
            raise ValueError("jobs target is owned by another adapter")
        path = Path(target.path)
        if path not in self.paths or target.physical_path != target.path:
            raise ValueError("unknown jobs target")
        return path

    def _step_target(self, step: MigrationStep) -> AdapterTarget:
        if step.adapter_id != self.adapter_id or step.physical_path is None:
            raise ValueError("migration step is not owned by jobs")
        for index, path in enumerate(self.paths):
            if str(path) == step.physical_path and step.target_id == f"jobs_{index}":
                return self._target(index, path, Path("/"))
        raise ValueError("unknown jobs migration step target")


def _path_store(path: Path) -> JobRunStore:
    # The migration implementation depends only on these three path/timeout
    # fields. Constructing RickySettings here could reject a historical config
    # before its owner-local migration has had a chance to inspect it.
    from ricky.jobs.store import JobRunStore

    store = object.__new__(JobRunStore)
    store.root = path.parent
    store.path = path
    store._busy_timeout_ms = 5_000
    return store


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
        raise _job_error("job upgrade path must be absolute")
    return selected.resolve()


def _job_error(message: str) -> RuntimeError:
    from ricky.jobs.store import JobStoreError

    return JobStoreError(message)
