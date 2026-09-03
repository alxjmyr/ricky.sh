"""Durable, operation-bound progress for one released-installation upgrade.

The installation manifest is the compatibility gate.  This module deliberately
keeps the more detailed operation record separate so a corrupt journal cannot
make the installation identity unreadable.  Callers must still hold the
exclusive installation operation lock while using the mutation functions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ricky.installation import fsync_directory, write_private_file
from ricky.upgrades.backups import BackupTarget
from ricky.upgrades.models import MigrationPlan, MigrationStep, ReleaseDescriptor
from ricky.upgrades.versions import ReleaseVersion, require_installed_release_version

UPGRADE_JOURNAL_FORMAT_VERSION = 1
UPGRADES_DIRECTORY_NAME = "upgrades"
UPGRADE_JOURNAL_FILENAME = "journal.json"
UPGRADE_BACKUP_DIRECTORY_NAME = "backup"
MAX_JOURNAL_BYTES = 4 * 1024 * 1024
MAX_FAILURE_SUMMARY_LENGTH = 500

_OPERATION_ID_PATTERN = r"^[0-9a-f]{32}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_STEP_IDENTITY_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*:[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
_REDACTION = "<redacted>"
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|authorization|credential|password|secret|token)\b"
    r"\s*[:=]\s*([^\s,;]+)"
)
_URL_CREDENTIAL = re.compile(r"(?i)\b(https?://)[^/@\s]+@")
_OPAQUE_VALUE = re.compile(r"(?<![0-9a-f])[A-Za-z0-9_+/=-]{40,}(?![0-9a-f])")

type UpgradeJournalState = Literal[
    "prepared",
    "backup_verified",
    "software_replaced",
    "migrating",
    "failed",
    "rolling_back",
    "commit_ready",
    "completed",
    "rolled_back",
]
type ResumableJournalState = Literal[
    "prepared",
    "backup_verified",
    "software_replaced",
    "migrating",
    "rolling_back",
    "commit_ready",
]
type UpgradeStepStatus = Literal["pending", "applying", "verified"]


class UpgradeJournalError(ValueError):
    """A durable upgrade journal is absent, corrupt, stale, or incoherent."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class UpgradeBackupBinding(_StrictModel):
    """Identity and eventual digest of the operation's targeted backup."""

    backup_id: str = Field(pattern=_OPERATION_ID_PATTERN)
    manifest_path: str = Field(min_length=1, max_length=4_096)
    manifest_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @field_validator("manifest_path")
    @classmethod
    def _canonical_manifest_path(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or path != path.resolve():
            raise ValueError("backup manifest path must be absolute and canonical")
        return value


class UpgradeSoftwareBinding(_StrictModel):
    """Exact source and target release artifacts cached for recovery."""

    source_release: ReleaseDescriptor
    target_release: ReleaseDescriptor
    source_wheel_path: str = Field(min_length=1, max_length=4_096)
    source_constraints_path: str = Field(min_length=1, max_length=4_096)
    target_wheel_path: str = Field(min_length=1, max_length=4_096)
    target_constraints_path: str = Field(min_length=1, max_length=4_096)

    @field_validator(
        "source_wheel_path",
        "source_constraints_path",
        "target_wheel_path",
        "target_constraints_path",
    )
    @classmethod
    def _canonical_artifact_path(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or path != path.resolve():
            raise ValueError("cached release artifact path must be absolute and canonical")
        return value


class UpgradeManagedBinding(_StrictModel):
    """Pre-upgrade launch state and explicit job-update authorization."""

    update_jobs: bool = False
    gateway_unit_path: str | None = Field(default=None, max_length=4_096)
    gateway_unit_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    gateway_was_enabled: bool = False
    gateway_was_active: bool = False
    managed_crontab_was_installed: bool = False

    @field_validator("gateway_unit_path")
    @classmethod
    def _canonical_unit_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        if not path.is_absolute() or path != path.resolve():
            raise ValueError("gateway unit path must be absolute and canonical")
        return value

    @model_validator(mode="after")
    def _coherent_gateway_state(self) -> Self:
        installed = self.gateway_unit_path is not None
        if installed != (self.gateway_unit_sha256 is not None):
            raise ValueError("installed gateway unit requires its exact digest")
        if not installed and (self.gateway_was_enabled or self.gateway_was_active):
            raise ValueError("an absent gateway unit cannot be enabled or active")
        return self


class UpgradeStepProgress(_StrictModel):
    """Durable progress for exactly one ordered migration step."""

    step_identity: str = Field(pattern=_STEP_IDENTITY_PATTERN, max_length=201)
    status: UpgradeStepStatus = "pending"


class UpgradeJournalFailure(_StrictModel):
    """Bounded recovery information that contains no raw exception payload."""

    summary: str = Field(min_length=1, max_length=MAX_FAILURE_SUMMARY_LENGTH)
    failed_at: datetime
    resume_state: ResumableJournalState

    @field_validator("summary")
    @classmethod
    def _single_line_printable_summary(cls, value: str) -> str:
        if value != sanitize_failure_summary(value):
            raise ValueError("failure summary must be bounded and sanitized")
        return value

    @field_validator("failed_at")
    @classmethod
    def _utc_failed_at(cls, value: datetime) -> datetime:
        return _require_utc(value, "failed_at")


class UpgradeJournal(_StrictModel):
    """Strict JSON-safe durable record for one exact upgrade operation."""

    format_version: Literal[1] = UPGRADE_JOURNAL_FORMAT_VERSION
    user_data_dir: str = Field(min_length=1, max_length=4_096)
    installation_id: str = Field(pattern=_OPERATION_ID_PATTERN)
    operation_id: str = Field(pattern=_OPERATION_ID_PATTERN)
    state: UpgradeJournalState
    source_software_version: ReleaseVersion
    target_software_version: ReleaseVersion
    source_data_generation: int = Field(ge=1)
    target_data_generation: int = Field(ge=1)
    plan_digest: str = Field(pattern=_SHA256_PATTERN)
    operation_digest: str = Field(pattern=_SHA256_PATTERN)
    ordered_steps: tuple[MigrationStep, ...] = ()
    backup_targets: tuple[BackupTarget, ...] = ()
    step_progress: tuple[UpgradeStepProgress, ...] = ()
    last_verified_step: str | None = Field(
        default=None,
        pattern=_STEP_IDENTITY_PATTERN,
        max_length=201,
    )
    backup: UpgradeBackupBinding
    software: UpgradeSoftwareBinding | None = None
    managed: UpgradeManagedBinding | None = None
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    failure: UpgradeJournalFailure | None = None
    rollback_eligible: bool
    commit_ready: bool

    @field_validator("user_data_dir")
    @classmethod
    def _canonical_user_data_dir(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or path != path.resolve():
            raise ValueError("journal user_data_dir must be absolute and canonical")
        return value

    @field_validator("started_at", "updated_at", "completed_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime | None, info: object) -> datetime | None:
        if value is None:
            return None
        name = getattr(info, "field_name", "timestamp")
        return _require_utc(value, name)

    @model_validator(mode="after")
    def _coherent_journal(self) -> Self:
        require_installed_release_version(self.source_software_version)
        require_installed_release_version(self.target_software_version)
        if self.target_software_version < self.source_software_version:
            raise ValueError("upgrade target software cannot precede source software")
        if self.target_data_generation < self.source_data_generation:
            raise ValueError("upgrade target data generation cannot precede source generation")
        if self.backup.backup_id != self.operation_id:
            raise ValueError("backup identity must match the upgrade operation")
        if self.software is not None:
            if self.software.source_release.software_version != self.source_software_version:
                raise ValueError("source release does not match journal source software")
            if self.software.target_release.software_version != self.target_software_version:
                raise ValueError("target release does not match journal target software")
            operation_root = Path(self.user_data_dir) / UPGRADES_DIRECTORY_NAME / self.operation_id
            bindings = (
                (self.software.source_wheel_path, self.software.source_release.wheel.name),
                (
                    self.software.source_constraints_path,
                    self.software.source_release.constraints.name,
                ),
                (self.software.target_wheel_path, self.software.target_release.wheel.name),
                (
                    self.software.target_constraints_path,
                    self.software.target_release.constraints.name,
                ),
            )
            for rendered, expected_name in bindings:
                path = Path(rendered)
                if not path.is_relative_to(operation_root) or path.name != expected_name:
                    raise ValueError("cached software artifact is outside its upgrade operation")

        plan = MigrationPlan.create(
            source_data_generation=self.source_data_generation,
            target_data_generation=self.target_data_generation,
            steps=self.ordered_steps,
        )
        if plan.plan_digest != self.plan_digest:
            raise ValueError("journal plan digest does not match its ordered steps")
        if self.operation_digest != _operation_digest(
            user_data_dir=self.user_data_dir,
            installation_id=self.installation_id,
            operation_id=self.operation_id,
            source_software_version=self.source_software_version,
            target_software_version=self.target_software_version,
            source_data_generation=self.source_data_generation,
            target_data_generation=self.target_data_generation,
            plan_digest=self.plan_digest,
            ordered_steps=self.ordered_steps,
            backup_targets=self.backup_targets,
            software=self.software,
            managed=self.managed,
        ):
            raise ValueError("journal operation digest does not match its immutable plan")
        backup_sources = tuple(target.source_path for target in self.backup_targets)
        if backup_sources != tuple(sorted(set(backup_sources))):
            raise ValueError("journal backup targets must be sorted and unique")

        expected_identities = tuple(_step_identity(step) for step in self.ordered_steps)
        found_identities = tuple(item.step_identity for item in self.step_progress)
        if found_identities != expected_identities:
            raise ValueError("journal step progress does not match the ordered migration plan")

        statuses = tuple(item.status for item in self.step_progress)
        verified_count = 0
        while verified_count < len(statuses) and statuses[verified_count] == "verified":
            verified_count += 1
        cursor = verified_count
        if cursor < len(statuses) and statuses[cursor] == "applying":
            cursor += 1
        if any(status != "pending" for status in statuses[cursor:]):
            raise ValueError("journal progress must be a verified prefix and one applying step")
        expected_last = expected_identities[verified_count - 1] if verified_count else None
        if self.last_verified_step != expected_last:
            raise ValueError("journal last_verified_step does not match durable step progress")

        terminal = self.state in {"completed", "rolled_back"}
        if terminal != (self.completed_at is not None):
            raise ValueError("only a terminal journal has a completed timestamp")
        if self.completed_at is not None and self.completed_at != self.updated_at:
            raise ValueError("journal completion and update timestamps must match")
        if self.updated_at < self.started_at:
            raise ValueError("journal updated_at cannot precede started_at")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("journal completed_at cannot precede started_at")

        if (self.state == "failed") != (self.failure is not None):
            raise ValueError("only a failed journal has a failure record")
        if self.failure is not None:
            if self.failure.failed_at != self.updated_at:
                raise ValueError("failure and update timestamps must match")
            if self.failure.failed_at < self.started_at:
                raise ValueError("journal failure cannot precede its start")

        backup_required = self.state in {
            "backup_verified",
            "software_replaced",
            "migrating",
            "commit_ready",
            "completed",
        }
        if self.state == "failed" and self.failure is not None:
            backup_required = self.failure.resume_state in {
                "backup_verified",
                "software_replaced",
                "migrating",
                "rolling_back",
                "commit_ready",
            }
        if backup_required and self.backup.manifest_digest is None:
            raise ValueError("journal state requires a verified backup manifest")
        if self.state == "completed" and any(status != "verified" for status in statuses):
            raise ValueError("an upgrade cannot complete before every step is verified")
        commit_ready_state = self.state in {"commit_ready", "completed"} or (
            self.state == "failed"
            and self.failure is not None
            and self.failure.resume_state == "commit_ready"
        )
        if self.commit_ready != commit_ready_state:
            raise ValueError("journal commit_ready fence does not match its durable phase")
        if self.commit_ready and any(status != "verified" for status in statuses):
            raise ValueError("commit_ready requires every migration step to be verified")
        eligible_state = self.state in {
            "backup_verified",
            "software_replaced",
            "migrating",
        } or (
            self.state == "failed"
            and self.failure is not None
            and self.failure.resume_state in {"backup_verified", "software_replaced", "migrating"}
        )
        if self.rollback_eligible != eligible_state:
            raise ValueError("journal rollback eligibility does not match its durable phase")
        return self


def sanitize_failure_summary(value: str) -> str:
    """Return one bounded single-line diagnostic with common secret forms redacted."""

    printable = "".join(character if character.isprintable() else " " for character in value)
    single_line = " ".join(printable.split())
    redacted = _URL_CREDENTIAL.sub(rf"\1{_REDACTION}@", single_line)
    redacted = _SENSITIVE_ASSIGNMENT.sub(rf"\1={_REDACTION}", redacted)
    redacted = _OPAQUE_VALUE.sub(_REDACTION, redacted)
    bounded = redacted[:MAX_FAILURE_SUMMARY_LENGTH]
    if len(redacted) > MAX_FAILURE_SUMMARY_LENGTH:
        bounded = _drop_split_redaction(bounded)
    return bounded.rstrip() or "upgrade operation failed"


def _drop_split_redaction(value: str) -> str:
    """Remove a redaction marker that the length bound cut in half.

    ``UpgradeJournalFailure`` validates that a stored summary already equals its
    own sanitized form, so sanitizing must stay idempotent.  A bound that lands
    inside ``<redacted>`` leaves a fragment that the sensitive-assignment
    pattern would match again on the next pass.
    """

    start = value.rfind("<")
    if start == -1:
        return value
    fragment = value[start:]
    if fragment != _REDACTION and _REDACTION.startswith(fragment):
        return value[:start]
    return value


def upgrade_journal_path(user_data_dir: Path, operation_id: str) -> Path:
    """Return the confined canonical path for an operation journal without writing."""

    _validate_operation_id(operation_id)
    root = _canonical_existing_root(user_data_dir)
    return root / UPGRADES_DIRECTORY_NAME / operation_id / UPGRADE_JOURNAL_FILENAME


def create_upgrade_journal(
    *,
    user_data_dir: Path,
    installation_id: str,
    operation_id: str,
    source_software_version: ReleaseVersion,
    target_software_version: ReleaseVersion,
    plan: MigrationPlan,
    backup_manifest_path: Path,
    backup_targets: tuple[BackupTarget, ...] = (),
    software: UpgradeSoftwareBinding | None = None,
    managed: UpgradeManagedBinding | None = None,
    now: datetime | None = None,
) -> UpgradeJournal:
    """Atomically create the prepared journal for one exact deterministic plan."""

    _validate_operation_id(installation_id)
    path = upgrade_journal_path(user_data_dir, operation_id)
    root = _canonical_existing_root(user_data_dir)
    operation_dir = path.parent
    expected_backup_parent = operation_dir / UPGRADE_BACKUP_DIRECTORY_NAME
    canonical_backup = _canonical_future_path(backup_manifest_path)
    if canonical_backup.parent != expected_backup_parent or canonical_backup.name in {
        "",
        ".",
        "..",
    }:
        raise UpgradeJournalError("backup manifest path is outside the upgrade operation")

    _ensure_private_directory(root / UPGRADES_DIRECTORY_NAME, parent=root)
    _ensure_private_directory(operation_dir, parent=operation_dir.parent)
    if path.is_symlink():
        raise UpgradeJournalError("upgrade journal cannot be a symbolic link")
    moment = now or datetime.now(UTC)
    canonical_targets = tuple(sorted(backup_targets, key=lambda item: item.source_path))
    operation_digest = _operation_digest(
        user_data_dir=str(root),
        installation_id=installation_id,
        operation_id=operation_id,
        source_software_version=source_software_version,
        target_software_version=target_software_version,
        source_data_generation=plan.source_data_generation,
        target_data_generation=plan.target_data_generation,
        plan_digest=plan.plan_digest,
        ordered_steps=plan.steps,
        backup_targets=canonical_targets,
        software=software,
        managed=managed,
    )
    journal = UpgradeJournal(
        user_data_dir=str(root),
        installation_id=installation_id,
        operation_id=operation_id,
        state="prepared",
        source_software_version=source_software_version,
        target_software_version=target_software_version,
        source_data_generation=plan.source_data_generation,
        target_data_generation=plan.target_data_generation,
        plan_digest=plan.plan_digest,
        operation_digest=operation_digest,
        ordered_steps=plan.steps,
        backup_targets=canonical_targets,
        step_progress=tuple(
            UpgradeStepProgress(step_identity=_step_identity(step)) for step in plan.steps
        ),
        backup=UpgradeBackupBinding(
            backup_id=operation_id,
            manifest_path=str(canonical_backup),
        ),
        software=software,
        managed=managed,
        started_at=moment,
        updated_at=moment,
        rollback_eligible=False,
        commit_ready=False,
    )
    if path.exists():
        existing = _load_journal_file(path)
        try:
            verify_upgrade_journal_binding(
                existing,
                user_data_dir=root,
                operation_id=operation_id,
                installation_id=installation_id,
                plan=plan,
                source_software_version=source_software_version,
                target_software_version=target_software_version,
                backup_targets=canonical_targets,
                software=software,
                managed=managed,
            )
        except UpgradeJournalError as exc:
            raise UpgradeJournalError(
                "an upgrade journal already exists for this operation"
            ) from exc
        if (
            existing.state == "prepared"
            and existing.backup.manifest_digest is None
            and all(item.status == "pending" for item in existing.step_progress)
        ):
            return existing
        raise UpgradeJournalError("an upgrade journal already exists for this operation")
    _write_journal(path, journal)
    return journal


def load_upgrade_journal(
    *,
    user_data_dir: Path,
    operation_id: str,
    installation_id: str | None = None,
    plan: MigrationPlan | None = None,
    source_software_version: ReleaseVersion | None = None,
    target_software_version: ReleaseVersion | None = None,
    backup_targets: tuple[BackupTarget, ...] | None = None,
    software: UpgradeSoftwareBinding | None = None,
    managed: UpgradeManagedBinding | None = None,
) -> UpgradeJournal:
    """Load a strict journal and verify every supplied operation binding."""

    path = upgrade_journal_path(user_data_dir, operation_id)
    journal = _load_journal_file(path)
    verify_upgrade_journal_binding(
        journal,
        user_data_dir=user_data_dir,
        operation_id=operation_id,
        installation_id=installation_id,
        plan=plan,
        source_software_version=source_software_version,
        target_software_version=target_software_version,
        backup_targets=backup_targets,
        software=software,
        managed=managed,
    )
    return journal


def verify_upgrade_journal_binding(
    journal: UpgradeJournal,
    *,
    user_data_dir: Path,
    operation_id: str,
    installation_id: str | None = None,
    plan: MigrationPlan | None = None,
    source_software_version: ReleaseVersion | None = None,
    target_software_version: ReleaseVersion | None = None,
    backup_targets: tuple[BackupTarget, ...] | None = None,
    software: UpgradeSoftwareBinding | None = None,
    managed: UpgradeManagedBinding | None = None,
) -> None:
    """Fail closed unless a journal belongs to the exact installation and plan."""

    path = upgrade_journal_path(user_data_dir, operation_id)
    if journal.operation_id != operation_id:
        raise UpgradeJournalError("upgrade journal operation identity does not match")
    if journal.user_data_dir != str(path.parents[2]):
        raise UpgradeJournalError("upgrade journal data-root identity does not match")
    if installation_id is not None and journal.installation_id != installation_id:
        raise UpgradeJournalError("upgrade journal installation identity does not match")
    expected_backup_parent = path.parent / UPGRADE_BACKUP_DIRECTORY_NAME
    backup_path = Path(journal.backup.manifest_path)
    if backup_path.parent != expected_backup_parent:
        raise UpgradeJournalError("upgrade journal backup binding is outside the operation")
    if plan is not None and (
        journal.plan_digest != plan.plan_digest
        or journal.ordered_steps != plan.steps
        or journal.source_data_generation != plan.source_data_generation
        or journal.target_data_generation != plan.target_data_generation
    ):
        raise UpgradeJournalError("upgrade journal migration plan does not match")
    if (
        source_software_version is not None
        and journal.source_software_version != source_software_version
    ):
        raise UpgradeJournalError("upgrade journal source software does not match")
    if (
        target_software_version is not None
        and journal.target_software_version != target_software_version
    ):
        raise UpgradeJournalError("upgrade journal target software does not match")
    if backup_targets is not None and journal.backup_targets != tuple(
        sorted(backup_targets, key=lambda item: item.source_path)
    ):
        raise UpgradeJournalError("upgrade journal backup targets do not match")
    if software is not None and journal.software != software:
        raise UpgradeJournalError("upgrade journal software artifacts do not match")
    if managed is not None and journal.managed != managed:
        raise UpgradeJournalError("upgrade journal managed integrations do not match")


def transition_upgrade_journal(
    *,
    user_data_dir: Path,
    current: UpgradeJournal,
    state: UpgradeJournalState,
    now: datetime | None = None,
    backup_manifest_digest: str | None = None,
    failure_summary: str | None = None,
) -> UpgradeJournal:
    """Validate and atomically commit one journal lifecycle transition."""

    allowed: dict[UpgradeJournalState, frozenset[UpgradeJournalState]] = {
        "prepared": frozenset({"backup_verified", "failed", "rolling_back"}),
        "backup_verified": frozenset({"software_replaced", "failed", "rolling_back"}),
        "software_replaced": frozenset({"migrating", "failed", "rolling_back"}),
        "migrating": frozenset({"commit_ready", "failed", "rolling_back"}),
        "failed": frozenset(
            {
                "prepared",
                "backup_verified",
                "software_replaced",
                "migrating",
                "rolling_back",
                "commit_ready",
            }
        ),
        "rolling_back": frozenset({"rolled_back", "failed"}),
        "commit_ready": frozenset({"completed", "failed"}),
        "completed": frozenset(),
        "rolled_back": frozenset(),
    }
    resuming_rollback = (
        current.state == "failed"
        and current.failure is not None
        and current.failure.resume_state == "rolling_back"
    )
    cancellation_before_mutation = current.state == "prepared" or (
        current.state == "failed"
        and current.failure is not None
        and current.failure.resume_state == "prepared"
    )
    if (
        state == "rolling_back"
        and not current.rollback_eligible
        and not resuming_rollback
        and not cancellation_before_mutation
    ):
        raise UpgradeJournalError("journal does not prove that rollback is still eligible")
    if state not in allowed[current.state]:
        raise UpgradeJournalError(f"invalid upgrade journal transition: {current.state} -> {state}")
    if current.state == "failed" and state != "rolling_back":
        assert current.failure is not None
        if state != current.failure.resume_state:
            raise UpgradeJournalError("failed journal can resume only its recorded durable state")
    if state == "failed" and failure_summary is None:
        raise UpgradeJournalError("a failed journal transition requires a failure summary")
    if state != "failed" and failure_summary is not None:
        raise UpgradeJournalError("a failure summary is valid only for a failed transition")
    if backup_manifest_digest is not None and not (
        current.state == "prepared" and state == "backup_verified"
    ):
        raise UpgradeJournalError("backup digest can be recorded only when backup verifies")
    if (
        current.state == "prepared"
        and state == "backup_verified"
        and backup_manifest_digest is None
    ):
        raise UpgradeJournalError("backup verification requires its manifest digest")

    moment = _transition_time(current, now)
    backup = current.backup
    if backup_manifest_digest is not None:
        backup = backup.model_copy(update={"manifest_digest": backup_manifest_digest})
    failure = None
    commit_ready = state in {"commit_ready", "completed"}
    rollback_eligible = state in {
        "backup_verified",
        "software_replaced",
        "migrating",
    }
    if state == "failed":
        if current.state in {"failed", "completed", "rolled_back"}:  # pragma: no cover
            raise AssertionError("invalid failure source escaped transition validation")
        resume_state = cast(ResumableJournalState, current.state)
        failure = UpgradeJournalFailure(
            summary=sanitize_failure_summary(failure_summary or ""),
            failed_at=moment,
            resume_state=resume_state,
        )
        rollback_eligible = resume_state in {
            "backup_verified",
            "software_replaced",
            "migrating",
        }
        commit_ready = resume_state == "commit_ready"
    completed = moment if state in {"completed", "rolled_back"} else None
    updated = _validated_copy(
        current,
        state=state,
        backup=backup,
        updated_at=moment,
        completed_at=completed,
        failure=failure,
        rollback_eligible=rollback_eligible,
        commit_ready=commit_ready,
    )
    _commit_journal(user_data_dir=user_data_dir, current=current, updated=updated)
    return updated


def mark_upgrade_step_applying(
    *,
    user_data_dir: Path,
    current: UpgradeJournal,
    step_index: int,
    now: datetime | None = None,
) -> UpgradeJournal:
    """Durably mark the next unverified step before its adapter may mutate."""

    if current.state != "migrating":
        raise UpgradeJournalError("migration progress requires a migrating journal")
    cursor = upgrade_resume_cursor(current)
    if step_index != cursor or step_index >= len(current.step_progress):
        raise UpgradeJournalError("only the deterministic resume cursor can begin applying")
    progress = current.step_progress[step_index]
    if progress.status == "applying":
        path = upgrade_journal_path(user_data_dir, current.operation_id)
        observed = _load_journal_file(path)
        if observed != current:
            raise UpgradeJournalError("upgrade journal changed while the operation was active")
        return observed
    items = list(current.step_progress)
    items[step_index] = progress.model_copy(update={"status": "applying"})
    updated = _validated_copy(
        current,
        step_progress=tuple(items),
        updated_at=_transition_time(current, now),
    )
    _commit_journal(user_data_dir=user_data_dir, current=current, updated=updated)
    return updated


def mark_upgrade_step_verified(
    *,
    user_data_dir: Path,
    current: UpgradeJournal,
    step_index: int,
    now: datetime | None = None,
) -> UpgradeJournal:
    """Durably advance only the currently applying step after owner verification."""

    if current.state != "migrating":
        raise UpgradeJournalError("migration progress requires a migrating journal")
    cursor = upgrade_resume_cursor(current)
    if step_index != cursor or step_index >= len(current.step_progress):
        raise UpgradeJournalError("only the deterministic resume cursor can be verified")
    progress = current.step_progress[step_index]
    if progress.status != "applying":
        raise UpgradeJournalError("a migration step must be marked applying before verification")
    items = list(current.step_progress)
    items[step_index] = progress.model_copy(update={"status": "verified"})
    updated = _validated_copy(
        current,
        step_progress=tuple(items),
        last_verified_step=progress.step_identity,
        updated_at=_transition_time(current, now),
    )
    _commit_journal(user_data_dir=user_data_dir, current=current, updated=updated)
    return updated


def upgrade_resume_cursor(
    journal: UpgradeJournal,
    *,
    plan: MigrationPlan | None = None,
) -> int:
    """Return the deterministic index whose target must next be inspected."""

    if journal.state in {"completed", "rolled_back"}:
        raise UpgradeJournalError("a terminal upgrade journal cannot be resumed")
    if plan is not None and (
        journal.plan_digest != plan.plan_digest or journal.ordered_steps != plan.steps
    ):
        raise UpgradeJournalError("resume plan does not match the journaled operation")
    for index, progress in enumerate(journal.step_progress):
        if progress.status != "verified":
            return index
    return len(journal.step_progress)


def _step_identity(step: MigrationStep) -> str:
    return f"{step.adapter_id}:{step.step_id}"


def _operation_digest(
    *,
    user_data_dir: str,
    installation_id: str,
    operation_id: str,
    source_software_version: ReleaseVersion,
    target_software_version: ReleaseVersion,
    source_data_generation: int,
    target_data_generation: int,
    plan_digest: str,
    ordered_steps: tuple[MigrationStep, ...],
    backup_targets: tuple[BackupTarget, ...],
    software: UpgradeSoftwareBinding | None,
    managed: UpgradeManagedBinding | None,
) -> str:
    payload = {
        "user_data_dir": user_data_dir,
        "installation_id": installation_id,
        "operation_id": operation_id,
        "source_software_version": str(source_software_version),
        "target_software_version": str(target_software_version),
        "source_data_generation": source_data_generation,
        "target_data_generation": target_data_generation,
        "plan_digest": plan_digest,
        "ordered_steps": [step.model_dump(mode="json") for step in ordered_steps],
        "backup_targets": [target.model_dump(mode="json") for target in backup_targets],
        "software": None if software is None else software.model_dump(mode="json"),
        "managed": None if managed is None else managed.model_dump(mode="json"),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validated_copy(journal: UpgradeJournal, **updates: object) -> UpgradeJournal:
    document = journal.model_dump(mode="python")
    document.update(updates)
    return UpgradeJournal.model_validate(document)


def _commit_journal(
    *,
    user_data_dir: Path,
    current: UpgradeJournal,
    updated: UpgradeJournal,
) -> None:
    path = upgrade_journal_path(user_data_dir, current.operation_id)
    observed = _load_journal_file(path)
    if observed != current:
        raise UpgradeJournalError("upgrade journal changed while the operation was active")
    _write_journal(path, updated)


def _write_journal(path: Path, journal: UpgradeJournal) -> None:
    content = (
        json.dumps(
            journal.model_dump(mode="json"),
            ensure_ascii=True,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    write_private_file(path, content)


def _load_journal_file(path: Path) -> UpgradeJournal:
    if path.is_symlink():
        raise UpgradeJournalError("upgrade journal cannot be a symbolic link")
    try:
        stat = path.stat()
        if not path.is_file() or stat.st_size > MAX_JOURNAL_BYTES:
            raise UpgradeJournalError(f"invalid upgrade journal: {path}")
        if os.name == "posix" and stat.st_uid != os.getuid():
            raise UpgradeJournalError(f"invalid upgrade journal: {path}")
        payload = path.read_bytes()
        return UpgradeJournal.model_validate_json(payload)
    except UpgradeJournalError:
        raise
    except (OSError, ValueError) as exc:
        raise UpgradeJournalError(f"invalid upgrade journal: {path}") from exc


def _validate_operation_id(value: str) -> None:
    if re.fullmatch(_OPERATION_ID_PATTERN, value) is None:
        raise UpgradeJournalError("upgrade operation identity is invalid")


def _canonical_existing_root(value: Path) -> Path:
    expanded = value.expanduser()
    if not expanded.is_absolute() or expanded.is_symlink():
        raise UpgradeJournalError("upgrade user_data_dir must be a canonical real directory")
    resolved = expanded.resolve()
    if expanded != resolved or not resolved.is_dir():
        raise UpgradeJournalError("upgrade user_data_dir must be a canonical real directory")
    if os.name == "posix" and resolved.stat().st_uid != os.getuid():
        raise UpgradeJournalError("upgrade user_data_dir must be owned by the current user")
    return resolved


def _canonical_future_path(value: Path) -> Path:
    expanded = value.expanduser()
    if not expanded.is_absolute() or expanded != expanded.resolve():
        raise UpgradeJournalError("upgrade path must be absolute and canonical")
    return expanded


def _ensure_private_directory(path: Path, *, parent: Path) -> None:
    if path.is_symlink():
        raise UpgradeJournalError("upgrade operation directories cannot be symbolic links")
    created = False
    try:
        path.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    if not path.is_dir():
        raise UpgradeJournalError("upgrade operation path is not a directory")
    if os.name == "posix":
        if path.stat().st_uid != os.getuid():
            raise UpgradeJournalError(
                "upgrade operation directory must be owned by the current user"
            )
        os.chmod(path, 0o700)
    if created:
        fsync_directory(parent)


def _transition_time(current: UpgradeJournal, value: datetime | None) -> datetime:
    moment = value or datetime.now(UTC)
    _require_utc(moment, "transition time")
    if moment < current.updated_at:
        raise UpgradeJournalError("journal transition time cannot move backward")
    return moment


def _require_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"journal {name} must be timezone-aware UTC")
    return value
