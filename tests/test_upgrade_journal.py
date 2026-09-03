"""Durability and recovery contracts for released-installation journals."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import ricky.installation as installation_module
from ricky.upgrades.backups import BackupTarget
from ricky.upgrades.journal import (
    MAX_FAILURE_SUMMARY_LENGTH,
    UpgradeJournal,
    UpgradeJournalError,
    UpgradeJournalFailure,
    create_upgrade_journal,
    load_upgrade_journal,
    mark_upgrade_step_applying,
    mark_upgrade_step_verified,
    sanitize_failure_summary,
    transition_upgrade_journal,
    upgrade_journal_path,
    upgrade_resume_cursor,
    verify_upgrade_journal_binding,
)
from ricky.upgrades.models import MigrationPlan, MigrationStep
from ricky.upgrades.versions import ReleaseVersion

START = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
INSTALLATION_ID = "a" * 32
OPERATION_ID = "b" * 32
BACKUP_DIGEST = "c" * 64


def _plan(root: Path) -> MigrationPlan:
    return MigrationPlan.create(
        source_data_generation=1,
        target_data_generation=2,
        steps=(
            MigrationStep(
                adapter_id="authority",
                step_id="schema-v4-v5",
                target_id="installation",
                physical_path=str((root / "authority.sqlite3").resolve()),
                source_schema_version=4,
                target_schema_version=5,
            ),
            MigrationStep(
                adapter_id="durable_tasks",
                step_id="schema-v2-v3",
                target_id="profile.shared",
                physical_path=str((root / "profiles/shared/tasks/tasks.sqlite3").resolve()),
                source_schema_version=2,
                target_schema_version=3,
                depends_on=("authority:schema-v4-v5",),
            ),
        ),
    )


def _create(root: Path, *, now: datetime = START) -> UpgradeJournal:
    root.mkdir(mode=0o700)
    return create_upgrade_journal(
        user_data_dir=root,
        installation_id=INSTALLATION_ID,
        operation_id=OPERATION_ID,
        source_software_version=ReleaseVersion.parse("0.6.0"),
        target_software_version=ReleaseVersion.parse("0.7.0"),
        plan=_plan(root),
        backup_manifest_path=root / "upgrades" / OPERATION_ID / "backup" / "manifest.json",
        now=now,
    )


def _migrating(root: Path) -> UpgradeJournal:
    journal = _create(root)
    journal = transition_upgrade_journal(
        user_data_dir=root,
        current=journal,
        state="backup_verified",
        backup_manifest_digest=BACKUP_DIGEST,
        now=START + timedelta(seconds=1),
    )
    journal = transition_upgrade_journal(
        user_data_dir=root,
        current=journal,
        state="software_replaced",
        now=START + timedelta(seconds=2),
    )
    return transition_upgrade_journal(
        user_data_dir=root,
        current=journal,
        state="migrating",
        now=START + timedelta(seconds=3),
    )


def _raw(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_create_persists_full_plan_bindings_and_private_modes(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()

    journal = _create(root)
    path = upgrade_journal_path(root, OPERATION_ID)

    assert journal.user_data_dir == str(root)
    assert journal.installation_id == INSTALLATION_ID
    assert journal.operation_id == OPERATION_ID
    assert journal.ordered_steps == _plan(root).steps
    assert journal.plan_digest == _plan(root).plan_digest
    assert [item.status for item in journal.step_progress] == ["pending", "pending"]
    assert journal.last_verified_step is None
    assert journal.backup.backup_id == OPERATION_ID
    assert journal.backup.manifest_digest is None
    assert journal.rollback_eligible is False
    assert journal.commit_ready is False
    assert UpgradeJournal.model_validate_json(path.read_bytes()) == journal
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert path.parent.parent.stat().st_mode & 0o777 == 0o700


def test_create_is_idempotent_only_for_the_exact_same_journal(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    first = _create(root)

    second = create_upgrade_journal(
        user_data_dir=root,
        installation_id=INSTALLATION_ID,
        operation_id=OPERATION_ID,
        source_software_version=ReleaseVersion.parse("0.6.0"),
        target_software_version=ReleaseVersion.parse("0.7.0"),
        plan=_plan(root),
        backup_manifest_path=root / "upgrades" / OPERATION_ID / "backup" / "manifest.json",
        now=START,
    )

    assert second == first
    with pytest.raises(UpgradeJournalError, match="already exists"):
        create_upgrade_journal(
            user_data_dir=root,
            installation_id=INSTALLATION_ID,
            operation_id=OPERATION_ID,
            source_software_version=ReleaseVersion.parse("0.6.0"),
            target_software_version=ReleaseVersion.parse("0.8.0"),
            plan=_plan(root),
            backup_manifest_path=(root / "upgrades" / OPERATION_ID / "backup" / "manifest.json"),
            now=START,
        )


def test_load_verifies_root_installation_operation_versions_and_plan(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    journal = _create(root)

    loaded = load_upgrade_journal(
        user_data_dir=root,
        operation_id=OPERATION_ID,
        installation_id=INSTALLATION_ID,
        plan=_plan(root),
        source_software_version=ReleaseVersion.parse("0.6.0"),
        target_software_version=ReleaseVersion.parse("0.7.0"),
    )

    assert loaded == journal
    with pytest.raises(UpgradeJournalError, match="installation identity"):
        verify_upgrade_journal_binding(
            loaded,
            user_data_dir=root,
            operation_id=OPERATION_ID,
            installation_id="d" * 32,
        )
    with pytest.raises(UpgradeJournalError, match="target software"):
        verify_upgrade_journal_binding(
            loaded,
            user_data_dir=root,
            operation_id=OPERATION_ID,
            target_software_version=ReleaseVersion.parse("0.8.0"),
        )
    other_plan = MigrationPlan.create(
        source_data_generation=1,
        target_data_generation=1,
    )
    with pytest.raises(UpgradeJournalError, match="migration plan"):
        verify_upgrade_journal_binding(
            loaded,
            user_data_dir=root,
            operation_id=OPERATION_ID,
            plan=other_plan,
        )


def test_backup_path_must_be_confined_to_the_operation(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    root.mkdir()

    with pytest.raises(UpgradeJournalError, match="outside"):
        create_upgrade_journal(
            user_data_dir=root,
            installation_id=INSTALLATION_ID,
            operation_id=OPERATION_ID,
            source_software_version=ReleaseVersion.parse("0.6.0"),
            target_software_version=ReleaseVersion.parse("0.7.0"),
            plan=_plan(root),
            backup_manifest_path=root / "somewhere-else" / "manifest.json",
            now=START,
        )


def test_journal_refuses_tampered_plan_payload_or_digest(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    _create(root)
    path = upgrade_journal_path(root, OPERATION_ID)
    document = _raw(path)
    document["ordered_steps"][0]["target_schema_version"] = 100
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(UpgradeJournalError, match="invalid upgrade journal"):
        load_upgrade_journal(user_data_dir=root, operation_id=OPERATION_ID)


def test_operation_digest_binds_exact_backup_targets(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    root.mkdir()
    target = BackupTarget(
        source_path=str(root / "authority.sqlite3"),
        kind="sqlite",
    )
    journal = create_upgrade_journal(
        user_data_dir=root,
        installation_id=INSTALLATION_ID,
        operation_id=OPERATION_ID,
        source_software_version=ReleaseVersion.parse("0.6.0"),
        target_software_version=ReleaseVersion.parse("0.7.0"),
        plan=_plan(root),
        backup_manifest_path=root / "upgrades" / OPERATION_ID / "backup" / "manifest.json",
        backup_targets=(target,),
        now=START,
    )
    assert journal.backup_targets == (target,)
    path = upgrade_journal_path(root, OPERATION_ID)
    document = _raw(path)
    document["backup_targets"][0]["source_path"] = str(root / "different.sqlite3")
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(UpgradeJournalError, match="invalid upgrade journal"):
        load_upgrade_journal(user_data_dir=root, operation_id=OPERATION_ID)


def test_progress_is_a_verified_prefix_with_one_ambiguous_applying_step(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "data").resolve()
    journal = _migrating(root)

    assert upgrade_resume_cursor(journal, plan=_plan(root)) == 0
    applying = mark_upgrade_step_applying(
        user_data_dir=root,
        current=journal,
        step_index=0,
        now=START + timedelta(seconds=4),
    )
    assert upgrade_resume_cursor(applying) == 0
    assert applying.step_progress[0].status == "applying"

    verified = mark_upgrade_step_verified(
        user_data_dir=root,
        current=applying,
        step_index=0,
        now=START + timedelta(seconds=5),
    )
    assert upgrade_resume_cursor(verified) == 1
    assert verified.last_verified_step == "authority:schema-v4-v5"
    with pytest.raises(UpgradeJournalError, match="resume cursor"):
        mark_upgrade_step_applying(
            user_data_dir=root,
            current=verified,
            step_index=0,
        )
    with pytest.raises(UpgradeJournalError, match="marked applying"):
        mark_upgrade_step_verified(
            user_data_dir=root,
            current=verified,
            step_index=1,
        )


def test_commit_ready_is_an_irreversible_rollback_fence(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    journal = _migrating(root)
    for index in range(2):
        journal = mark_upgrade_step_applying(
            user_data_dir=root,
            current=journal,
            step_index=index,
            now=START + timedelta(seconds=4 + index * 2),
        )
        journal = mark_upgrade_step_verified(
            user_data_dir=root,
            current=journal,
            step_index=index,
            now=START + timedelta(seconds=5 + index * 2),
        )

    ready = transition_upgrade_journal(
        user_data_dir=root,
        current=journal,
        state="commit_ready",
        now=START + timedelta(seconds=8),
    )
    assert ready.commit_ready is True
    assert ready.rollback_eligible is False
    with pytest.raises(UpgradeJournalError, match="rollback is still eligible"):
        transition_upgrade_journal(
            user_data_dir=root,
            current=ready,
            state="rolling_back",
            now=START + timedelta(seconds=9),
        )

    completed = transition_upgrade_journal(
        user_data_dir=root,
        current=ready,
        state="completed",
        now=START + timedelta(seconds=9),
    )
    assert completed.completed_at == completed.updated_at
    assert completed.commit_ready is True
    with pytest.raises(UpgradeJournalError, match="terminal"):
        upgrade_resume_cursor(completed)


def test_commit_ready_refuses_unverified_steps(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    journal = _migrating(root)

    with pytest.raises(ValidationError, match="commit_ready"):
        # Exercise the strict durable model independently of transition helpers.
        UpgradeJournal.model_validate(
            journal.model_dump(mode="python")
            | {"state": "commit_ready", "commit_ready": True, "rollback_eligible": False}
        )


def test_failure_is_sanitized_and_resumes_only_the_precise_phase(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    journal = _migrating(root)
    secret = "sk_" + "x" * 80

    failed = transition_upgrade_journal(
        user_data_dir=root,
        current=journal,
        state="failed",
        failure_summary=f"request failed\npassword={secret} token={secret}",
        now=START + timedelta(seconds=4),
    )

    assert failed.failure is not None
    assert failed.failure.resume_state == "migrating"
    assert secret not in failed.failure.summary
    assert "\n" not in failed.failure.summary
    assert len(failed.failure.summary) <= MAX_FAILURE_SUMMARY_LENGTH
    assert failed.rollback_eligible is True
    with pytest.raises(UpgradeJournalError, match="recorded durable state"):
        transition_upgrade_journal(
            user_data_dir=root,
            current=failed,
            state="software_replaced",
        )
    resumed = transition_upgrade_journal(
        user_data_dir=root,
        current=failed,
        state="migrating",
        now=START + timedelta(seconds=5),
    )
    assert resumed.failure is None


def test_verified_backup_rollback_is_resumable(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    prepared = _create(root)
    backed_up = transition_upgrade_journal(
        user_data_dir=root,
        current=prepared,
        state="backup_verified",
        backup_manifest_digest=BACKUP_DIGEST,
        now=START + timedelta(seconds=1),
    )
    rolling_back = transition_upgrade_journal(
        user_data_dir=root,
        current=backed_up,
        state="rolling_back",
        now=START + timedelta(seconds=2),
    )
    failed = transition_upgrade_journal(
        user_data_dir=root,
        current=rolling_back,
        state="failed",
        failure_summary="restore interrupted",
        now=START + timedelta(seconds=3),
    )
    assert failed.failure is not None
    assert failed.failure.resume_state == "rolling_back"
    resumed = transition_upgrade_journal(
        user_data_dir=root,
        current=failed,
        state="rolling_back",
        now=START + timedelta(seconds=4),
    )
    rolled_back = transition_upgrade_journal(
        user_data_dir=root,
        current=resumed,
        state="rolled_back",
        now=START + timedelta(seconds=5),
    )
    assert rolled_back.completed_at == rolled_back.updated_at


def test_pre_backup_rollback_is_a_safe_cancellation(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    prepared = _create(root)

    rolling_back = transition_upgrade_journal(
        user_data_dir=root,
        current=prepared,
        state="rolling_back",
        now=START + timedelta(seconds=1),
    )
    rolled_back = transition_upgrade_journal(
        user_data_dir=root,
        current=rolling_back,
        state="rolled_back",
        now=START + timedelta(seconds=2),
    )

    assert rolling_back.backup.manifest_digest is None
    assert rolled_back.state == "rolled_back"


def test_invalid_transition_and_backward_time_leave_journal_unchanged(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    journal = _create(root)
    path = upgrade_journal_path(root, OPERATION_ID)
    before = path.read_bytes()

    with pytest.raises(UpgradeJournalError, match="invalid upgrade journal transition"):
        transition_upgrade_journal(
            user_data_dir=root,
            current=journal,
            state="software_replaced",
        )
    with pytest.raises(UpgradeJournalError, match="move backward"):
        transition_upgrade_journal(
            user_data_dir=root,
            current=journal,
            state="failed",
            failure_summary="failed",
            now=START - timedelta(seconds=1),
        )
    assert path.read_bytes() == before


def test_stale_in_memory_journal_cannot_overwrite_new_progress(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    stale = _create(root)
    current = transition_upgrade_journal(
        user_data_dir=root,
        current=stale,
        state="backup_verified",
        backup_manifest_digest=BACKUP_DIGEST,
        now=START + timedelta(seconds=1),
    )

    with pytest.raises(UpgradeJournalError, match="changed while"):
        transition_upgrade_journal(
            user_data_dir=root,
            current=stale,
            state="failed",
            failure_summary="stale writer",
            now=START + timedelta(seconds=2),
        )
    assert load_upgrade_journal(user_data_dir=root, operation_id=OPERATION_ID) == current


def test_corrupt_journal_error_never_echoes_its_payload(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    _create(root)
    path = upgrade_journal_path(root, OPERATION_ID)
    secret = "top-secret-value"
    path.write_text(f'{{"password": "{secret}"}}', encoding="utf-8")

    with pytest.raises(UpgradeJournalError) as captured:
        load_upgrade_journal(user_data_dir=root, operation_id=OPERATION_ID)

    assert "invalid upgrade journal" in str(captured.value)
    assert secret not in str(captured.value)


def test_symlinked_journal_is_refused(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    _create(root)
    path = upgrade_journal_path(root, OPERATION_ID)
    target = tmp_path / "outside.json"
    target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.unlink()
    path.symlink_to(target)

    with pytest.raises(UpgradeJournalError, match="symbolic link"):
        load_upgrade_journal(user_data_dir=root, operation_id=OPERATION_ID)


def test_replace_fault_preserves_prior_durable_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = (tmp_path / "data").resolve()
    journal = _create(root)
    path = upgrade_journal_path(root, OPERATION_ID)
    before = path.read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("injected before atomic replace")

    monkeypatch.setattr(installation_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        transition_upgrade_journal(
            user_data_dir=root,
            current=journal,
            state="backup_verified",
            backup_manifest_digest=BACKUP_DIGEST,
            now=START + timedelta(seconds=1),
        )

    assert path.read_bytes() == before
    assert not list(path.parent.glob(f".{path.name}-*"))


def test_temporary_file_fsync_fault_preserves_prior_durable_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = (tmp_path / "data").resolve()
    journal = _create(root)
    path = upgrade_journal_path(root, OPERATION_ID)
    before = path.read_bytes()
    real_fsync = installation_module.os.fsync
    calls = 0

    def fail_first_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected temporary-file fsync")
        real_fsync(descriptor)

    monkeypatch.setattr(installation_module.os, "fsync", fail_first_fsync)
    with pytest.raises(OSError, match="injected"):
        transition_upgrade_journal(
            user_data_dir=root,
            current=journal,
            state="backup_verified",
            backup_manifest_digest=BACKUP_DIGEST,
            now=START + timedelta(seconds=1),
        )

    assert path.read_bytes() == before
    assert not list(path.parent.glob(f".{path.name}-*"))


def test_directory_fsync_fault_leaves_a_complete_reloadable_new_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = (tmp_path / "data").resolve()
    journal = _create(root)

    def fail_fsync(path: Path) -> None:
        raise OSError(f"injected directory fsync at {path.name}")

    monkeypatch.setattr(installation_module, "fsync_directory", fail_fsync)
    with pytest.raises(OSError, match="injected"):
        transition_upgrade_journal(
            user_data_dir=root,
            current=journal,
            state="backup_verified",
            backup_manifest_digest=BACKUP_DIGEST,
            now=START + timedelta(seconds=1),
        )

    reloaded = load_upgrade_journal(user_data_dir=root, operation_id=OPERATION_ID)
    assert reloaded.state == "backup_verified"
    assert reloaded.backup.manifest_digest == BACKUP_DIGEST


@pytest.mark.parametrize(
    "value",
    [
        "password=hunter2\nsecond line",
        "https://alice:password@example.test/failure",
        "token=" + "a" * 100,
        "\x00\x01",
        "token=" + "a" * 600,
        "word " * 120 + "password=hunter2 and a much longer trailing tail",
    ],
)
def test_failure_sanitizer_is_bounded_idempotent_and_single_line(value: str) -> None:
    sanitized = sanitize_failure_summary(value)

    assert sanitized == sanitize_failure_summary(sanitized)
    assert sanitized
    assert len(sanitized) <= MAX_FAILURE_SUMMARY_LENGTH
    assert all(character.isprintable() for character in sanitized)
    assert "\n" not in sanitized


def test_failure_sanitizer_survives_a_bound_that_splits_a_redaction() -> None:
    """The length bound must never leave half of a redaction marker behind.

    ``UpgradeJournalFailure`` rejects a summary that does not already equal its
    own sanitized form, and ``UpgradeCoordinator._record_failure`` suppresses
    ``ValueError``.  A sanitizer that is not idempotent would therefore discard
    in silence the very diagnostic it was asked to store.
    """

    marker = "<redacted>"
    fragments = tuple(marker[:length] for length in range(1, len(marker)))

    def summary_for(padding: int) -> str:
        filler = ("word " * 120)[:padding].rstrip()
        return f"{filler} token=supersecretvaluegoeshere and trailing text"

    paddings = range(470, 500)
    for padding in paddings:
        sanitized = sanitize_failure_summary(summary_for(padding))

        assert sanitized == sanitize_failure_summary(sanitized)
        assert len(sanitized) <= MAX_FAILURE_SUMMARY_LENGTH
        assert not any(sanitized.endswith(fragment) for fragment in fragments)
        UpgradeJournalFailure(summary=sanitized, failed_at=START, resume_state="prepared")

    split = [
        padding
        for padding in paddings
        if sanitize_failure_summary(summary_for(padding)).endswith("token=")
    ]
    assert split, "expected at least one bound to land inside the redaction marker"
