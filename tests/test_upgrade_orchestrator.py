"""Crash-boundary recovery coverage for forward migration and rollback."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ricky.durable_tasks.upgrade import (
    DurableTasksUpgradeAdapter,
    create_current_durable_tasks_database,
    inspect_durable_tasks_database,
)
from ricky.installation import (
    initialize_installation,
    installation_operation_lock,
    require_installation,
)
from ricky.upgrades.backups import BackupTarget, create_targeted_backup
from ricky.upgrades.journal import (
    UpgradeJournal,
    UpgradeManagedBinding,
    load_upgrade_journal,
)
from ricky.upgrades.non_sql import SchedulesUpgradeAdapter
from ricky.upgrades.orchestrator import UpgradeCoordinator, UpgradeCoordinatorError
from ricky.upgrades.registry import UpgradeRegistry
from ricky.upgrades.versions import ReleaseVersion

OPERATION_ID = "f" * 32
PRIOR_OPERATION_ID = "a" * 32
SOURCE_VERSION = ReleaseVersion.parse("0.6.0")
TARGET_VERSION = ReleaseVersion.parse("0.7.0")

FORWARD_BOUNDARIES = (
    "journal_created",
    "manifest_prepared",
    "backup_published",
    "journal_backup_verified",
    "before_target_install",
    "after_target_install",
    "journal_software_replaced",
    "manifest_software_replaced",
    "journal_migrating",
    "manifest_migrating",
    "step_applying",
    "step_applied",
    "step_verified",
    "step_progress_committed",
    "whole_target_verified",
    "managed_target_reconciled",
    "journal_commit_ready",
    "backup_retention_enforced",
    "manifest_forward_clean",
    "journal_completed",
)

ROLLBACK_BOUNDARIES = (
    "journal_rolling_back",
    "manifest_rolling_back",
    "rollback_backup_verified",
    "rollback_data_restored",
    "rollback_data_verified",
    "before_source_install",
    "after_source_install",
    "managed_source_reconciled",
    "manifest_rollback_clean",
    "journal_rolled_back",
)


class _SimulatedCrash(BaseException):
    pass


class _CrashOnce:
    def __init__(self, boundary: str) -> None:
        self.boundary = boundary
        self.triggered = False

    def hit(
        self,
        boundary: str,
        *,
        operation_id: str,
        step_identity: str | None = None,
    ) -> None:
        del operation_id, step_identity
        if boundary == self.boundary and not self.triggered:
            self.triggered = True
            raise _SimulatedCrash(boundary)


class _FailOnce:
    """Raise one ordinary error the coordinator records as a durable failure."""

    def __init__(self, boundary: str) -> None:
        self.boundary = boundary
        self.triggered = False

    def hit(
        self,
        boundary: str,
        *,
        operation_id: str,
        step_identity: str | None = None,
    ) -> None:
        del operation_id, step_identity
        if boundary == self.boundary and not self.triggered:
            self.triggered = True
            raise RuntimeError(f"injected recoverable failure at {boundary}")


class _Software:
    def __init__(self) -> None:
        self.version = SOURCE_VERSION
        self.target_installs = 0
        self.source_installs = 0

    def inspect_version(self) -> ReleaseVersion:
        return self.version

    def install_target(self, journal: object) -> None:
        del journal
        self.target_installs += 1
        self.version = TARGET_VERSION

    def install_source(self, journal: object) -> None:
        del journal
        self.source_installs += 1
        self.version = SOURCE_VERSION


def _old_installation(tmp_path: Path) -> tuple[Path, Path, UpgradeRegistry]:
    root = (tmp_path / "ricky-data").resolve()
    initialize_installation(root)
    database = root / "profiles" / "shared" / "tasks" / "tasks.sqlite3"
    create_current_durable_tasks_database(database.resolve())
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE task_tags")
        connection.execute("PRAGMA user_version = 1")
    registry = UpgradeRegistry((DurableTasksUpgradeAdapter((database.resolve(),)),))
    assert inspect_durable_tasks_database(database).state == "migration_required"
    return root, database, registry


def _drive_forward(
    root: Path,
    registry: UpgradeRegistry,
    software: _Software,
    fault: _CrashOnce | None,
) -> None:
    for _attempt in range(8):
        _pointer, manifest = require_installation()
        if (
            manifest.migration_state == "clean"
            and software.version == TARGET_VERSION
            and manifest.last_lifecycle_version == str(TARGET_VERSION)
        ):
            return
        try:
            with installation_operation_lock(
                mode="exclusive",
                timeout_seconds=1,
                operation="test_upgrade",
                operation_id=OPERATION_ID,
            ) as lock:
                coordinator = UpgradeCoordinator(
                    user_data_dir=root,
                    lock=lock,
                    registry=registry,
                    software=software,
                    fault_injector=fault,
                )
                _pointer, locked_manifest = require_installation()
                if locked_manifest.migration_state == "clean":
                    coordinator.prepare(
                        target_software_version=TARGET_VERSION,
                        target_data_generation=1,
                        operation_id=OPERATION_ID,
                    )
                coordinator.resume()
        except _SimulatedCrash:
            continue
    raise AssertionError("forward upgrade did not converge")


def _drive_rollback(
    root: Path,
    registry: UpgradeRegistry,
    software: _Software,
    fault: _CrashOnce | None,
) -> None:
    for _attempt in range(8):
        _pointer, manifest = require_installation()
        if (
            manifest.migration_state == "clean"
            and software.version == SOURCE_VERSION
            and manifest.last_lifecycle_version == str(SOURCE_VERSION)
        ):
            return
        try:
            with installation_operation_lock(
                mode="exclusive",
                timeout_seconds=1,
                operation="test_rollback",
                operation_id=OPERATION_ID,
            ) as lock:
                UpgradeCoordinator(
                    user_data_dir=root,
                    lock=lock,
                    registry=registry,
                    software=software,
                    fault_injector=fault,
                ).rollback()
        except _SimulatedCrash:
            continue
    raise AssertionError("rollback did not converge")


def _crash_forward_at(
    root: Path,
    registry: UpgradeRegistry,
    software: _Software,
    boundary: str,
) -> None:
    fault = _CrashOnce(boundary)
    try:
        with installation_operation_lock(
            mode="exclusive",
            timeout_seconds=1,
            operation="test_upgrade",
            operation_id=OPERATION_ID,
        ) as lock:
            coordinator = UpgradeCoordinator(
                user_data_dir=root,
                lock=lock,
                registry=registry,
                software=software,
                fault_injector=fault,
            )
            coordinator.prepare(
                target_software_version=TARGET_VERSION,
                target_data_generation=1,
                operation_id=OPERATION_ID,
            )
            coordinator.resume()
    except _SimulatedCrash:
        pass
    assert fault.triggered is True


def _crash_rollback_at(
    root: Path,
    registry: UpgradeRegistry,
    software: _Software,
    boundary: str,
) -> None:
    fault = _CrashOnce(boundary)
    try:
        with installation_operation_lock(
            mode="exclusive",
            timeout_seconds=1,
            operation="test_rollback",
            operation_id=OPERATION_ID,
        ) as lock:
            UpgradeCoordinator(
                user_data_dir=root,
                lock=lock,
                registry=registry,
                software=software,
                fault_injector=fault,
            ).rollback()
    except _SimulatedCrash:
        pass
    assert fault.triggered is True


def _fail_rollback_at(
    root: Path,
    registry: UpgradeRegistry,
    software: _Software,
    boundary: str,
) -> None:
    fault = _FailOnce(boundary)
    with (
        pytest.raises(UpgradeCoordinatorError),
        installation_operation_lock(
            mode="exclusive",
            timeout_seconds=1,
            operation="test_rollback",
            operation_id=OPERATION_ID,
        ) as lock,
    ):
        UpgradeCoordinator(
            user_data_dir=root,
            lock=lock,
            registry=registry,
            software=software,
            fault_injector=fault,
        ).rollback()
    assert fault.triggered is True


def _resume_once(
    root: Path,
    registry: UpgradeRegistry,
    software: _Software,
) -> UpgradeJournal:
    with installation_operation_lock(
        mode="exclusive",
        timeout_seconds=1,
        operation="test_resume",
        operation_id=OPERATION_ID,
    ) as lock:
        return UpgradeCoordinator(
            user_data_dir=root,
            lock=lock,
            registry=registry,
            software=software,
        ).resume()


@pytest.mark.parametrize("boundary", FORWARD_BOUNDARIES)
def test_each_forward_durable_boundary_resumes_deterministically(
    tmp_path: Path,
    boundary: str,
) -> None:
    root, database, registry = _old_installation(tmp_path)
    software = _Software()
    fault = _CrashOnce(boundary)

    _drive_forward(root, registry, software, fault)

    _pointer, manifest = require_installation()
    assert fault.triggered is True
    assert manifest.migration_state == "clean"
    assert manifest.data_generation == 1
    assert manifest.last_lifecycle_version == "0.7.0"
    assert software.version == TARGET_VERSION
    assert software.target_installs == 1
    assert inspect_durable_tasks_database(database).state == "current"


@pytest.mark.parametrize("boundary", ROLLBACK_BOUNDARIES)
def test_each_rollback_boundary_restores_exact_source_state(
    tmp_path: Path,
    boundary: str,
) -> None:
    root, database, registry = _old_installation(tmp_path)
    software = _Software()
    setup_fault = _CrashOnce("step_applied")
    try:
        with installation_operation_lock(
            mode="exclusive",
            timeout_seconds=1,
            operation="test_upgrade",
            operation_id=OPERATION_ID,
        ) as lock:
            coordinator = UpgradeCoordinator(
                user_data_dir=root,
                lock=lock,
                registry=registry,
                software=software,
                fault_injector=setup_fault,
            )
            coordinator.prepare(
                target_software_version=TARGET_VERSION,
                target_data_generation=1,
                operation_id=OPERATION_ID,
            )
            coordinator.resume()
    except _SimulatedCrash:
        pass
    assert setup_fault.triggered is True
    assert software.version == TARGET_VERSION
    assert inspect_durable_tasks_database(database).state == "current"

    fault = _CrashOnce(boundary)
    _drive_rollback(root, registry, software, fault)

    _pointer, manifest = require_installation()
    assert fault.triggered is True
    assert manifest.migration_state == "clean"
    assert manifest.data_generation == 1
    assert manifest.last_lifecycle_version == "0.6.0"
    assert software.version == SOURCE_VERSION
    assert software.source_installs == 1
    restored = inspect_durable_tasks_database(database)
    assert restored.state == "migration_required"
    assert restored.found_schema_version == 1


def test_pre_backup_rollback_cancels_without_installing_or_creating_a_backup(
    tmp_path: Path,
) -> None:
    root, database, registry = _old_installation(tmp_path)
    software = _Software()
    with installation_operation_lock(
        mode="exclusive",
        timeout_seconds=1,
        operation="test_upgrade",
        operation_id=OPERATION_ID,
    ) as lock:
        coordinator = UpgradeCoordinator(
            user_data_dir=root,
            lock=lock,
            registry=registry,
            software=software,
        )
        coordinator.prepare(
            target_software_version=TARGET_VERSION,
            target_data_generation=1,
            operation_id=OPERATION_ID,
        )
        journal = coordinator.rollback()

    _pointer, manifest = require_installation()
    assert journal.state == "rolled_back"
    assert journal.backup.manifest_digest is None
    assert manifest.migration_state == "clean"
    assert software.target_installs == software.source_installs == 0
    assert inspect_durable_tasks_database(database).state == "migration_required"


def test_journaled_backup_targets_and_manifest_bind_the_complete_operation(
    tmp_path: Path,
) -> None:
    root, database, registry = _old_installation(tmp_path)
    software = _Software()
    fault = _CrashOnce("journal_backup_verified")
    try:
        _drive_forward(root, registry, software, fault)
    except AssertionError:  # pragma: no cover - helper normally converges after one crash.
        raise

    journal = load_upgrade_journal(
        user_data_dir=root,
        operation_id=OPERATION_ID,
    )
    assert journal.backup_targets[0].source_path == str(database)
    assert journal.operation_digest != journal.plan_digest
    assert journal.backup.manifest_digest is not None


def test_job_update_opt_in_binds_schedule_desired_state_to_backup(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "ricky-data").resolve()
    initialize_installation(root)
    schedules = root / "schedules.toml"
    schedules.write_text("version = 1\nschedules = []\n", encoding="utf-8")
    registry = UpgradeRegistry((SchedulesUpgradeAdapter(schedules),))
    software = _Software()

    with installation_operation_lock(
        mode="exclusive",
        timeout_seconds=1,
        operation="test_upgrade",
        operation_id=OPERATION_ID,
    ) as lock:
        journal = UpgradeCoordinator(
            user_data_dir=root,
            lock=lock,
            registry=registry,
            software=software,
        ).prepare(
            target_software_version=TARGET_VERSION,
            target_data_generation=1,
            operation_id=OPERATION_ID,
            managed=UpgradeManagedBinding(update_jobs=True),
        )

    assert len(journal.backup_targets) == 1
    assert journal.backup_targets[0].source_path == str(schedules)
    assert journal.backup_targets[0].kind == "file"


def test_resume_refuses_a_rolling_back_operation_without_mutating_state(
    tmp_path: Path,
) -> None:
    root, database, registry = _old_installation(tmp_path)
    software = _Software()
    _crash_forward_at(root, registry, software, "step_applied")
    _crash_rollback_at(root, registry, software, "rollback_backup_verified")

    before_journal = load_upgrade_journal(user_data_dir=root, operation_id=OPERATION_ID)
    _pointer, before_manifest = require_installation()
    assert before_journal.state == "rolling_back"
    assert before_manifest.migration_state == "rolling_back"

    with pytest.raises(UpgradeCoordinatorError) as error:
        _resume_once(root, registry, software)

    assert "ricky upgrade --rollback --yes" in str(error.value)
    _pointer, after_manifest = require_installation()
    assert load_upgrade_journal(user_data_dir=root, operation_id=OPERATION_ID) == before_journal
    assert after_manifest == before_manifest
    # The crash landed before the data restore, so nothing may have advanced.
    assert inspect_durable_tasks_database(database).state == "current"


def test_resume_refuses_a_failed_journal_that_resolves_to_rolling_back(
    tmp_path: Path,
) -> None:
    root, _database, registry = _old_installation(tmp_path)
    software = _Software()
    _crash_forward_at(root, registry, software, "step_applied")
    _crash_rollback_at(root, registry, software, "rollback_backup_verified")
    _fail_rollback_at(root, registry, software, "manifest_rolling_back")

    failed = load_upgrade_journal(user_data_dir=root, operation_id=OPERATION_ID)
    _pointer, before_manifest = require_installation()
    assert failed.state == "failed"
    assert failed.failure is not None
    assert failed.failure.resume_state == "rolling_back"
    assert before_manifest.migration_state == "failed"

    with pytest.raises(UpgradeCoordinatorError) as error:
        _resume_once(root, registry, software)

    assert "ricky upgrade --rollback --yes" in str(error.value)
    _pointer, after_manifest = require_installation()
    assert load_upgrade_journal(user_data_dir=root, operation_id=OPERATION_ID).state == (
        "rolling_back"
    )
    assert after_manifest == before_manifest


def test_refused_forward_resume_leaves_the_rollback_able_to_converge(
    tmp_path: Path,
) -> None:
    root, database, registry = _old_installation(tmp_path)
    software = _Software()
    _crash_forward_at(root, registry, software, "step_applied")
    _crash_rollback_at(root, registry, software, "rollback_backup_verified")

    with pytest.raises(UpgradeCoordinatorError):
        _resume_once(root, registry, software)

    _drive_rollback(root, registry, software, None)

    _pointer, manifest = require_installation()
    assert manifest.migration_state == "clean"
    assert manifest.last_lifecycle_version == "0.6.0"
    assert software.version == SOURCE_VERSION
    restored = inspect_durable_tasks_database(database)
    assert restored.state == "migration_required"
    assert restored.found_schema_version == 1


def test_forward_upgrade_completes_despite_a_corrupt_prior_operation_journal(
    tmp_path: Path,
) -> None:
    root, database, registry = _old_installation(tmp_path)
    stale_root = root / "upgrades" / PRIOR_OPERATION_ID
    stale_root.mkdir(mode=0o700, parents=True)
    stale_journal = stale_root / "journal.json"
    stale_journal.write_text('{"format_version": 1, "user_data', encoding="utf-8")
    software = _Software()

    _drive_forward(root, registry, software, None)

    _pointer, manifest = require_installation()
    journal = load_upgrade_journal(user_data_dir=root, operation_id=OPERATION_ID)
    assert journal.state == "completed"
    assert manifest.migration_state == "clean"
    assert manifest.data_generation == 1
    assert manifest.last_lifecycle_version == "0.7.0"
    assert inspect_durable_tasks_database(database).state == "current"
    # Retention never rewrites or removes an operation journal it cannot read.
    assert stale_journal.read_text(encoding="utf-8") == '{"format_version": 1, "user_data'


def test_forward_upgrade_completes_despite_a_corrupt_prior_retained_backup(
    tmp_path: Path,
) -> None:
    root, database, registry = _old_installation(tmp_path)
    _pointer, installed = require_installation()
    legacy = root / "legacy-state.txt"
    legacy.write_text("legacy", encoding="utf-8")
    create_targeted_backup(
        user_data_dir=root,
        operation_id=PRIOR_OPERATION_ID,
        installation_id=installed.installation_id,
        source_data_generation=1,
        source_software_version=SOURCE_VERSION,
        plan_digest="0" * 64,
        targets=(BackupTarget(source_path=str(legacy), kind="file"),),
    )
    prior_backup = root / "upgrades" / PRIOR_OPERATION_ID / "backup"
    artifacts = sorted((prior_backup / "artifacts").iterdir())
    assert len(artifacts) == 1
    artifacts[0].write_bytes(b"corrupted")
    software = _Software()

    _drive_forward(root, registry, software, None)

    _pointer, manifest = require_installation()
    journal = load_upgrade_journal(user_data_dir=root, operation_id=OPERATION_ID)
    assert journal.state == "completed"
    assert manifest.migration_state == "clean"
    assert manifest.data_generation == 1
    assert inspect_durable_tasks_database(database).state == "current"
    # Housekeeping that cannot verify a backup must not delete one either.
    assert prior_backup.is_dir()
    assert (root / "upgrades" / OPERATION_ID / "backup").is_dir()
