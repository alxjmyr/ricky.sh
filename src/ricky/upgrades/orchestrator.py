"""Crash-resumable data upgrade and rollback orchestration under one EX lock."""

from __future__ import annotations

import re
from contextlib import suppress
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from ricky.installation import (
    InstallationError,
    InstallationManifest,
    InstallationOperationLock,
    require_installation,
    transition_installation_manifest,
)
from ricky.upgrades.backups import (
    BACKUP_MANIFEST_FILENAME,
    BackupTarget,
    backup_directory,
    create_targeted_backup,
    enforce_backup_retention,
    restore_backup,
    verify_backup,
)
from ricky.upgrades.journal import (
    UpgradeJournal,
    UpgradeJournalError,
    UpgradeManagedBinding,
    UpgradeSoftwareBinding,
    create_upgrade_journal,
    load_upgrade_journal,
    mark_upgrade_step_applying,
    mark_upgrade_step_verified,
    transition_upgrade_journal,
    upgrade_resume_cursor,
)
from ricky.upgrades.models import AdapterPreflight, MigrationPlan
from ricky.upgrades.registry import UpgradeRegistry
from ricky.upgrades.versions import ReleaseVersion, require_installed_release_version

_OPERATION_DIRECTORY_PATTERN = re.compile(r"[0-9a-f]{32}")
_FORWARD_RESUME_TERMINAL_STATE = "completed"


class UpgradeCoordinatorError(RuntimeError):
    """An upgrade or recovery operation failed closed with its journal intact."""


class _ForwardResumeRefused(UpgradeCoordinatorError):
    """Forward resume is closed for this operation and must not record a failure."""


class SoftwareController(Protocol):
    """Exact installed-software boundary supplied by the uv handoff phase."""

    def inspect_version(self) -> ReleaseVersion: ...

    def install_target(self, journal: UpgradeJournal) -> None: ...

    def install_source(self, journal: UpgradeJournal) -> None: ...


class ManagedIntegrationController(Protocol):
    """Idempotent cron, schedule, job, and gateway reconciliation boundary."""

    def reconcile_target(self, journal: UpgradeJournal) -> None: ...

    def reconcile_source(self, journal: UpgradeJournal) -> None: ...


class NoManagedIntegrations:
    """Default for coordinator tests and installations with no launch surfaces."""

    def reconcile_target(self, journal: UpgradeJournal) -> None:
        del journal

    def reconcile_source(self, journal: UpgradeJournal) -> None:
        del journal


class FaultInjector(Protocol):
    """Test seam for a process-ending fault after one durable boundary."""

    def hit(
        self,
        boundary: str,
        *,
        operation_id: str,
        step_identity: str | None = None,
    ) -> None: ...


class NoFaults:
    """Production fault-injection implementation."""

    def hit(
        self,
        boundary: str,
        *,
        operation_id: str,
        step_identity: str | None = None,
    ) -> None:
        del boundary, operation_id, step_identity


class UpgradeCoordinator:
    """Own one exact upgrade journal while its caller holds the EX lock."""

    def __init__(
        self,
        *,
        user_data_dir: Path,
        lock: InstallationOperationLock,
        registry: UpgradeRegistry,
        software: SoftwareController,
        integrations: ManagedIntegrationController | None = None,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self._root = user_data_dir.expanduser().resolve()
        self._lock = lock
        self._registry = registry
        self._software = software
        self._integrations = integrations or NoManagedIntegrations()
        self._faults = fault_injector or NoFaults()

    def prepare(
        self,
        *,
        target_software_version: ReleaseVersion,
        target_data_generation: int,
        operation_id: str | None = None,
        software: UpgradeSoftwareBinding | None = None,
        managed: UpgradeManagedBinding | None = None,
    ) -> UpgradeJournal:
        """Freeze the exact plan before gating the installation as prepared."""

        self._require_lock()
        manifest = self._bound_manifest(require_clean=True)
        source_version = require_installed_release_version(self._software.inspect_version())
        require_installed_release_version(target_software_version)
        if target_software_version < source_version:
            raise UpgradeCoordinatorError("upgrade target cannot precede installed software")
        if software is not None and (
            software.source_release.software_version != source_version
            or software.target_release.software_version != target_software_version
        ):
            raise UpgradeCoordinatorError("cached release pair does not match upgrade endpoints")
        plan = self._registry.build_plan(
            source_data_generation=manifest.data_generation,
            target_data_generation=target_data_generation,
        )
        preflights = self._registry.preflight(user_data_dir=self._root)
        backup_targets = _backup_targets_for_plan(plan, preflights, managed=managed)
        selected_operation = operation_id or uuid4().hex
        backup_manifest = (
            backup_directory(self._root, selected_operation) / BACKUP_MANIFEST_FILENAME
        )
        journal = create_upgrade_journal(
            user_data_dir=self._root,
            installation_id=manifest.installation_id,
            operation_id=selected_operation,
            source_software_version=source_version,
            target_software_version=target_software_version,
            plan=plan,
            backup_manifest_path=backup_manifest,
            backup_targets=backup_targets,
            software=software,
            managed=managed,
        )
        self._hit("journal_created", journal)
        transition_installation_manifest(
            self._root,
            manifest,
            lock=self._lock,
            migration_state="prepared",
            operation_id=selected_operation,
            lifecycle_version=str(source_version),
        )
        self._hit("manifest_prepared", journal)
        return journal

    def resume(self) -> UpgradeJournal:
        """Resume the exact original forward plan without rebuilding live intent."""

        self._require_lock()
        manifest = self._bound_manifest(require_clean=False)
        journal = self._load_bound_journal(manifest)
        try:
            journal = self._resume_failed_journal(journal)
            # A rollback that has begun may have already restored part of the
            # data toward the source, so resuming forward would migrate a
            # half-restored installation.  Refuse before touching the manifest.
            if journal.state == "rolling_back":
                raise _ForwardResumeRefused(
                    "this upgrade is rolling back and cannot resume forward; "
                    "run `ricky upgrade --rollback --yes`"
                )
            manifest = self._advance_manifest_for_journal(manifest, journal)

            if journal.state == "prepared":
                backup = create_targeted_backup(
                    user_data_dir=self._root,
                    operation_id=journal.operation_id,
                    installation_id=journal.installation_id,
                    source_data_generation=journal.source_data_generation,
                    source_software_version=journal.source_software_version,
                    plan_digest=journal.operation_digest,
                    targets=journal.backup_targets,
                )
                self._hit("backup_published", journal)
                journal = transition_upgrade_journal(
                    user_data_dir=self._root,
                    current=journal,
                    state="backup_verified",
                    backup_manifest_digest=backup.manifest_sha256,
                )
                self._hit("journal_backup_verified", journal)

            if journal.state == "backup_verified":
                installed = require_installed_release_version(self._software.inspect_version())
                if installed == journal.source_software_version:
                    self._hit("before_target_install", journal)
                    self._software.install_target(journal)
                    self._hit("after_target_install", journal)
                    installed = require_installed_release_version(self._software.inspect_version())
                if installed != journal.target_software_version:
                    raise UpgradeCoordinatorError(
                        "installed software matches neither journaled source nor target"
                    )
                journal = transition_upgrade_journal(
                    user_data_dir=self._root,
                    current=journal,
                    state="software_replaced",
                )
                self._hit("journal_software_replaced", journal)
                manifest = self._advance_manifest_for_journal(manifest, journal)
                self._hit("manifest_software_replaced", journal)

            if journal.state == "software_replaced":
                self._require_installed(journal.target_software_version)
                journal = transition_upgrade_journal(
                    user_data_dir=self._root,
                    current=journal,
                    state="migrating",
                )
                self._hit("journal_migrating", journal)
                manifest = self._advance_manifest_for_journal(manifest, journal)
                self._hit("manifest_migrating", journal)

            if journal.state == "migrating":
                manifest, journal = self._apply_steps(manifest, journal)
                self._verify_target(journal)
                self._hit("whole_target_verified", journal)
                self._integrations.reconcile_target(journal)
                self._hit("managed_target_reconciled", journal)
                journal = transition_upgrade_journal(
                    user_data_dir=self._root,
                    current=journal,
                    state="commit_ready",
                )
                self._hit("journal_commit_ready", journal)

            if journal.state == "commit_ready":
                self._require_installed(journal.target_software_version)
                self._verify_target(journal)
                # Past the commit fence retention is only disk-space
                # housekeeping.  A corrupt unrelated operation directory or an
                # unverifiable retained backup must never fail an upgrade whose
                # target is already installed, migrated, and verified.
                with suppress(Exception):
                    enforce_backup_retention(
                        user_data_dir=self._root,
                        in_progress_operation_id=journal.operation_id,
                        successful_operation_ids=(
                            *self._retention_protected_operation_ids(),
                            journal.operation_id,
                        ),
                    )
                self._hit("backup_retention_enforced", journal)
                manifest = self._manifest_ready_to_clean(manifest, journal)
                transition_installation_manifest(
                    self._root,
                    manifest,
                    lock=self._lock,
                    migration_state="clean",
                    operation_id=None,
                    data_generation=journal.target_data_generation,
                    lifecycle_version=str(journal.target_software_version),
                )
                self._hit("manifest_forward_clean", journal)
                journal = transition_upgrade_journal(
                    user_data_dir=self._root,
                    current=journal,
                    state="completed",
                )
                self._hit("journal_completed", journal)
            # Every forward branch falls through to the next one, so a resume
            # that raised nothing must have reached the forward terminal state.
            # Any other state means the operation was never resumed forward and
            # must not be reported as a finished recovery.
            if journal.state != _FORWARD_RESUME_TERMINAL_STATE:
                raise UpgradeCoordinatorError(
                    f"forward resume cannot recover a {journal.state} upgrade operation"
                )
            return journal
        except _ForwardResumeRefused:
            raise
        except Exception as exc:
            self._record_failure(manifest=manifest, journal=journal, error=exc)
            if isinstance(exc, UpgradeCoordinatorError):
                raise
            raise UpgradeCoordinatorError(
                "upgrade failed safely; run `ricky upgrade --resume` or "
                "`ricky upgrade --rollback --yes`"
            ) from exc

    def _retention_protected_operation_ids(self) -> tuple[str, ...]:
        """Return prior operations whose retained backup must survive pruning.

        A completed operation is protected because its backup is the newest
        verified restore point.  An operation whose journal cannot be read is
        protected too: it can no longer drive an automated rollback, but its
        backup tree is self-describing and may still be the only copy of the
        user's pre-upgrade data.  Deleting a backup is irreversible, so an
        unreadable journal is treated as a reason to keep bytes, never to
        discard them.
        """

        upgrades = self._root / "upgrades"
        if not upgrades.is_dir():
            return ()
        protected: list[str] = []
        for operation_root in sorted(upgrades.iterdir()):
            if not operation_root.is_dir():
                continue
            if _OPERATION_DIRECTORY_PATTERN.fullmatch(operation_root.name) is None:
                continue
            journal_path = operation_root / "journal.json"
            if not journal_path.exists():
                continue
            try:
                prior = load_upgrade_journal(
                    user_data_dir=self._root,
                    operation_id=operation_root.name,
                )
            except (UpgradeJournalError, OSError):
                protected.append(operation_root.name)
                continue
            if prior.state == "completed":
                protected.append(prior.operation_id)
        return tuple(protected)

    def rollback(self) -> UpgradeJournal:
        """Restore journaled data first and exact source software last."""

        self._require_lock()
        manifest = self._bound_manifest(require_clean=False)
        journal = self._load_bound_journal(manifest)
        try:
            journal = self._resume_failed_journal(journal)
            if journal.commit_ready or journal.state in {"commit_ready", "completed"}:
                raise UpgradeCoordinatorError(
                    "rollback is closed because the upgrade reached its commit fence"
                )
            if journal.state == "rolled_back":
                raise UpgradeCoordinatorError("upgrade rollback is already complete")
            if journal.state != "rolling_back":
                journal = transition_upgrade_journal(
                    user_data_dir=self._root,
                    current=journal,
                    state="rolling_back",
                )
                self._hit("journal_rolling_back", journal)
            manifest = self._manifest_rolling_back(manifest, journal)
            self._hit("manifest_rolling_back", journal)

            if journal.backup.manifest_digest is not None:
                backup = verify_backup(
                    Path(journal.backup.manifest_path).parent,
                    expected_user_data_dir=self._root,
                    expected_installation_id=journal.installation_id,
                    expected_operation_id=journal.operation_id,
                    expected_source_data_generation=journal.source_data_generation,
                    expected_source_software_version=journal.source_software_version,
                    expected_plan_digest=journal.operation_digest,
                )
                if backup.manifest_sha256 != journal.backup.manifest_digest:
                    raise UpgradeCoordinatorError(
                        "verified backup digest does not match the upgrade journal"
                    )
                self._hit("rollback_backup_verified", journal)
                restore_backup(
                    Path(journal.backup.manifest_path).parent,
                    expected_user_data_dir=self._root,
                    expected_installation_id=journal.installation_id,
                    expected_operation_id=journal.operation_id,
                    expected_source_data_generation=journal.source_data_generation,
                    expected_source_software_version=journal.source_software_version,
                    expected_plan_digest=journal.operation_digest,
                )
                self._hit("rollback_data_restored", journal)
                self._verify_source(journal)
                self._hit("rollback_data_verified", journal)

            installed = require_installed_release_version(self._software.inspect_version())
            if installed == journal.target_software_version:
                self._hit("before_source_install", journal)
                self._software.install_source(journal)
                self._hit("after_source_install", journal)
                installed = require_installed_release_version(self._software.inspect_version())
            if installed != journal.source_software_version:
                raise UpgradeCoordinatorError(
                    "installed software matches neither recoverable rollback endpoint"
                )
            self._integrations.reconcile_source(journal)
            self._hit("managed_source_reconciled", journal)
            transition_installation_manifest(
                self._root,
                manifest,
                lock=self._lock,
                migration_state="clean",
                operation_id=None,
                data_generation=journal.source_data_generation,
                lifecycle_version=str(journal.source_software_version),
            )
            self._hit("manifest_rollback_clean", journal)
            journal = transition_upgrade_journal(
                user_data_dir=self._root,
                current=journal,
                state="rolled_back",
            )
            self._hit("journal_rolled_back", journal)
            return journal
        except Exception as exc:
            self._record_failure(manifest=manifest, journal=journal, error=exc)
            if isinstance(exc, UpgradeCoordinatorError):
                raise
            raise UpgradeCoordinatorError(
                "rollback failed safely; run `ricky upgrade --rollback --yes` again"
            ) from exc

    def _apply_steps(
        self,
        manifest: InstallationManifest,
        journal: UpgradeJournal,
    ) -> tuple[InstallationManifest, UpgradeJournal]:
        manifest = self._manifest_ready_to_migrate(manifest, journal)
        while (index := upgrade_resume_cursor(journal)) < len(journal.ordered_steps):
            step = journal.ordered_steps[index]
            identity = f"{step.adapter_id}:{step.step_id}"
            journal = mark_upgrade_step_applying(
                user_data_dir=self._root,
                current=journal,
                step_index=index,
            )
            self._hit("step_applying", journal, step_identity=identity)
            inspection = self._registry.inspect_step(step, user_data_dir=self._root)
            if (
                inspection.state == "current"
                and inspection.found_schema_version == step.target_schema_version
            ):
                self._registry.verify_step(step, user_data_dir=self._root)
            elif (
                inspection.state == "migration_required"
                and inspection.found_schema_version == step.source_schema_version
            ):
                self._registry.apply_step(step)
                self._hit("step_applied", journal, step_identity=identity)
                self._registry.verify_step(step, user_data_dir=self._root)
            else:
                raise UpgradeCoordinatorError(
                    f"journaled migration target is not at source or target: {identity}"
                )
            self._hit("step_verified", journal, step_identity=identity)
            journal = mark_upgrade_step_verified(
                user_data_dir=self._root,
                current=journal,
                step_index=index,
            )
            self._hit("step_progress_committed", journal, step_identity=identity)
        return manifest, journal

    def _verify_target(self, journal: UpgradeJournal) -> None:
        for step in journal.ordered_steps:
            inspection = self._registry.verify_step(step, user_data_dir=self._root)
            if (
                inspection.state != "current"
                or inspection.found_schema_version != step.target_schema_version
            ):
                raise UpgradeCoordinatorError("migration target verification failed")
        for inspection in self._registry.inspect(user_data_dir=self._root):
            if inspection.state not in {"absent", "current"}:
                raise UpgradeCoordinatorError(
                    f"whole-installation verification failed for {inspection.target.adapter_id}"
                )

    def _verify_source(self, journal: UpgradeJournal) -> None:
        for step in journal.ordered_steps:
            inspection = self._registry.inspect_step(step, user_data_dir=self._root)
            if step.source_schema_version == step.target_schema_version:
                valid = (
                    inspection.state == "current"
                    and inspection.found_schema_version == step.source_schema_version
                )
            else:
                valid = (
                    inspection.state == "migration_required"
                    and inspection.found_schema_version == step.source_schema_version
                )
            if not valid:
                raise UpgradeCoordinatorError(
                    f"restored source verification failed for {step.adapter_id}:{step.step_id}"
                )

    def _advance_manifest_for_journal(
        self,
        manifest: InstallationManifest,
        journal: UpgradeJournal,
    ) -> InstallationManifest:
        if journal.state == "software_replaced" and manifest.migration_state == "prepared":
            return transition_installation_manifest(
                self._root,
                manifest,
                lock=self._lock,
                migration_state="software_replaced",
                operation_id=journal.operation_id,
                lifecycle_version=str(journal.target_software_version),
            )
        if journal.state in {"migrating", "commit_ready"}:
            if manifest.migration_state == "prepared":
                manifest = transition_installation_manifest(
                    self._root,
                    manifest,
                    lock=self._lock,
                    migration_state="software_replaced",
                    operation_id=journal.operation_id,
                    lifecycle_version=str(journal.target_software_version),
                )
            if manifest.migration_state in {"software_replaced", "failed"}:
                return transition_installation_manifest(
                    self._root,
                    manifest,
                    lock=self._lock,
                    migration_state="migrating",
                    operation_id=journal.operation_id,
                    lifecycle_version=str(journal.target_software_version),
                )
        return manifest

    def _manifest_ready_to_migrate(
        self,
        manifest: InstallationManifest,
        journal: UpgradeJournal,
    ) -> InstallationManifest:
        advanced = self._advance_manifest_for_journal(manifest, journal)
        if advanced.migration_state != "migrating":
            raise UpgradeCoordinatorError("manifest is not ready for journaled migration")
        return advanced

    def _manifest_ready_to_clean(
        self,
        manifest: InstallationManifest,
        journal: UpgradeJournal,
    ) -> InstallationManifest:
        advanced = self._advance_manifest_for_journal(manifest, journal)
        if advanced.migration_state != "migrating":
            raise UpgradeCoordinatorError("manifest is not ready for forward commit")
        return advanced

    def _manifest_rolling_back(
        self,
        manifest: InstallationManifest,
        journal: UpgradeJournal,
    ) -> InstallationManifest:
        if manifest.migration_state == "rolling_back":
            return manifest
        if manifest.migration_state not in {
            "prepared",
            "software_replaced",
            "migrating",
            "failed",
        }:
            raise UpgradeCoordinatorError("manifest is not recoverable by rollback")
        return transition_installation_manifest(
            self._root,
            manifest,
            lock=self._lock,
            migration_state="rolling_back",
            operation_id=journal.operation_id,
            lifecycle_version=str(self._software.inspect_version()),
        )

    def _resume_failed_journal(self, journal: UpgradeJournal) -> UpgradeJournal:
        if journal.state != "failed":
            return journal
        if journal.failure is None:  # pragma: no cover - strict model establishes this.
            raise UpgradeJournalError("failed journal has no resume state")
        return transition_upgrade_journal(
            user_data_dir=self._root,
            current=journal,
            state=journal.failure.resume_state,
        )

    def _record_failure(
        self,
        *,
        manifest: InstallationManifest,
        journal: UpgradeJournal,
        error: Exception,
    ) -> None:
        if journal.state not in {"failed", "completed", "rolled_back"}:
            with suppress(OSError, ValueError):
                transition_upgrade_journal(
                    user_data_dir=self._root,
                    current=journal,
                    state="failed",
                    failure_summary=str(error),
                )
        if manifest.migration_state in {"software_replaced", "migrating", "rolling_back"}:
            with suppress(InstallationError, OSError, ValueError):
                transition_installation_manifest(
                    self._root,
                    manifest,
                    lock=self._lock,
                    migration_state="failed",
                    operation_id=manifest.operation_id,
                    lifecycle_version=str(self._software.inspect_version()),
                )

    def _load_bound_journal(self, manifest: InstallationManifest) -> UpgradeJournal:
        if manifest.operation_id is None:
            raise UpgradeCoordinatorError("installation has no active upgrade operation")
        return load_upgrade_journal(
            user_data_dir=self._root,
            operation_id=manifest.operation_id,
            installation_id=manifest.installation_id,
        )

    def _bound_manifest(self, *, require_clean: bool) -> InstallationManifest:
        pointer, manifest = require_installation()
        if Path(pointer.user_data_dir) != self._root:
            raise UpgradeCoordinatorError("upgrade data root changed under the operation lock")
        if pointer.installation_id != manifest.installation_id:
            raise UpgradeCoordinatorError("upgrade installation identity is inconsistent")
        if require_clean and manifest.migration_state != "clean":
            raise UpgradeCoordinatorError("another upgrade operation is already active")
        if not require_clean and manifest.migration_state == "clean":
            raise UpgradeCoordinatorError("there is no upgrade operation to recover")
        return manifest

    def _require_lock(self) -> None:
        self._lock.require_owned_exclusive()

    def _require_installed(self, expected: ReleaseVersion) -> None:
        if require_installed_release_version(self._software.inspect_version()) != expected:
            raise UpgradeCoordinatorError("installed Ricky version does not match the journal")

    def _hit(
        self,
        boundary: str,
        journal: UpgradeJournal,
        *,
        step_identity: str | None = None,
    ) -> None:
        self._faults.hit(
            boundary,
            operation_id=journal.operation_id,
            step_identity=step_identity,
        )


def _backup_targets_for_plan(
    plan: MigrationPlan,
    preflights: tuple[AdapterPreflight, ...],
    *,
    managed: UpgradeManagedBinding | None = None,
) -> tuple[BackupTarget, ...]:
    """Bind each mutable physical step target exactly once before preparation."""

    kinds_by_path: dict[str, str] = {}
    for preflight in preflights:
        physical = preflight.target.physical_path
        observed = kinds_by_path.setdefault(physical, preflight.target.kind)
        if observed != preflight.target.kind:
            raise UpgradeCoordinatorError("co-located upgrade owners disagree on target kind")
    targets: dict[str, BackupTarget] = {}
    for step in plan.steps:
        if step.physical_path is None:
            raise UpgradeCoordinatorError("migration step has no physical backup target")
        kind = kinds_by_path.get(step.physical_path)
        if kind not in {"sqlite", "file", "tree"}:
            raise UpgradeCoordinatorError("migration step has no backup-capable preflight target")
        if kind == "sqlite":
            target = BackupTarget(source_path=step.physical_path, kind="sqlite")
        elif kind == "file":
            target = BackupTarget(source_path=step.physical_path, kind="file")
        else:
            target = BackupTarget(source_path=step.physical_path, kind="tree")
        targets[step.physical_path] = target
    if managed is not None and managed.update_jobs:
        for preflight in preflights:
            if preflight.target.adapter_id != "schedules":
                continue
            for rendered in preflight.backup_paths:
                path = Path(rendered)
                if path != Path(preflight.target.physical_path):
                    raise UpgradeCoordinatorError(
                        "schedule reconciliation backup path differs from its owned target"
                    )
                targets[rendered] = BackupTarget(source_path=rendered, kind="file")
    return tuple(targets[path] for path in sorted(targets))
