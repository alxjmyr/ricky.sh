"""Targeted backup and exact-restore boundaries for released upgrades."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

import ricky.upgrades.backups as backups
from ricky.upgrades.backups import (
    BackupError,
    BackupItem,
    BackupManifest,
    BackupTarget,
    enforce_backup_retention,
    estimate_backup_bytes,
)
from ricky.upgrades.backups import (
    create_targeted_backup as _create_targeted_backup,
)
from ricky.upgrades.backups import (
    restore_backup as _restore_backup,
)
from ricky.upgrades.backups import (
    verify_backup as _verify_backup,
)
from ricky.upgrades.versions import ReleaseVersion

INSTALLATION_ID = "0" * 32
SOURCE_VERSION = ReleaseVersion.parse("0.6.0")
PLAN_DIGEST = "f" * 64


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "ricky-data"
    root.mkdir(mode=0o700)
    return root.resolve()


def _target(path: Path, kind: str) -> BackupTarget:
    return BackupTarget(source_path=str(path.resolve()), kind=kind)  # type: ignore[arg-type]


def create_targeted_backup(
    *,
    user_data_dir: Path,
    operation_id: str,
    targets: tuple[BackupTarget, ...],
    free_space_margin_bytes: int = 0,
) -> BackupManifest:
    return _create_targeted_backup(
        user_data_dir=user_data_dir,
        operation_id=operation_id,
        installation_id=INSTALLATION_ID,
        source_data_generation=1,
        source_software_version=SOURCE_VERSION,
        plan_digest=PLAN_DIGEST,
        targets=targets,
        free_space_margin_bytes=free_space_margin_bytes,
    )


def verify_backup(
    backup_dir: Path, *, expected_user_data_dir: Path | None = None
) -> BackupManifest:
    root = expected_user_data_dir or backup_dir.parents[2]
    return _verify_backup(
        backup_dir,
        expected_user_data_dir=root,
        expected_installation_id=INSTALLATION_ID,
        expected_operation_id=backup_dir.parent.name,
        expected_source_data_generation=1,
        expected_source_software_version=SOURCE_VERSION,
        expected_plan_digest=PLAN_DIGEST,
    )


def restore_backup(
    backup_dir: Path, *, expected_user_data_dir: Path | None = None
) -> BackupManifest:
    root = expected_user_data_dir or backup_dir.parents[2]
    return _restore_backup(
        backup_dir,
        expected_user_data_dir=root,
        expected_installation_id=INSTALLATION_ID,
        expected_operation_id=backup_dir.parent.name,
        expected_source_data_generation=1,
        expected_source_software_version=SOURCE_VERSION,
        expected_plan_digest=PLAN_DIGEST,
    )


def _uri_significant_root(tmp_path: Path) -> Path:
    """Build a data root whose path holds characters SQLite would percent-decode."""

    container = tmp_path / "My%20Data#1?v=2"
    container.mkdir()
    root = container / "ricky-data"
    root.mkdir(mode=0o700)
    return root.resolve()


def _descriptors_below(root: Path) -> list[str]:
    """Report this process's open file descriptors that point below ``root``."""

    descriptors = Path("/proc/self/fd")
    if not descriptors.is_dir():  # pragma: no cover - Linux exposes this directory.
        return []
    prefix = f"{root}{os.sep}"
    found: list[str] = []
    for entry in descriptors.iterdir():
        try:
            target = os.readlink(entry)
        except OSError:  # The descriptor for the listing itself can already be gone.
            continue
        if target.startswith(prefix):
            found.append(target)
    return found


def _create_sqlite(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA user_version = 7")
    connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO records (value) VALUES ('checkpointed')")
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("INSERT INTO records (value) VALUES ('wal-only')")
    connection.commit()
    return connection


def test_manifest_is_strict_json_round_trip_and_paths_are_confined(tmp_path: Path) -> None:
    root = _root(tmp_path)
    source = root / "settings.toml"
    source.write_text("enabled = true\n", encoding="utf-8")

    manifest = create_targeted_backup(
        user_data_dir=root,
        operation_id="1" * 32,
        targets=(_target(source, "file"),),
        free_space_margin_bytes=0,
    )

    assert BackupManifest.model_validate_json(manifest.model_dump_json(), strict=True) == manifest
    assert manifest.installation_id == INSTALLATION_ID
    assert manifest.source_data_generation == 1
    assert manifest.source_software_version == SOURCE_VERSION
    assert manifest.plan_digest == PLAN_DIGEST
    with pytest.raises(ValidationError):
        BackupItem(
            source_path=str(source),
            backup_path="../escape",
            kind="file",
            size=1,
            mode=0o600,
            sha256="a" * 64,
        )
    document = json.loads(manifest.model_dump_json())
    document["unexpected"] = True
    with pytest.raises(ValidationError):
        BackupManifest.model_validate(document)

    directory = root / "upgrades" / ("1" * 32) / "backup"
    with pytest.raises(BackupError, match="migration plan"):
        _verify_backup(
            directory,
            expected_user_data_dir=root,
            expected_installation_id=INSTALLATION_ID,
            expected_operation_id="1" * 32,
            expected_source_data_generation=1,
            expected_source_software_version=SOURCE_VERSION,
            expected_plan_digest="e" * 64,
        )


def test_targeted_file_and_tree_restore_exact_content_modes_and_scope(tmp_path: Path) -> None:
    root = _root(tmp_path)
    settings = root / "ricky.toml"
    settings.write_text("before\n", encoding="utf-8")
    settings.chmod(0o640)
    tree = root / "profiles" / "shared" / "memory"
    nested = tree / "archive"
    nested.mkdir(mode=0o710, parents=True)
    tree.chmod(0o750)
    note = tree / "note.md"
    note.write_text("original note\n", encoding="utf-8")
    note.chmod(0o640)
    archived = nested / "old.md"
    archived.write_text("old\n", encoding="utf-8")
    archived.chmod(0o600)
    untouched = root / "attachments" / "large.bin"
    untouched.parent.mkdir(mode=0o700)
    untouched.write_bytes(b"outside-plan")

    manifest = create_targeted_backup(
        user_data_dir=root,
        operation_id="2" * 32,
        targets=(_target(tree, "tree"), _target(settings, "file")),
        free_space_margin_bytes=0,
    )

    settings.write_text("after\n", encoding="utf-8")
    settings.chmod(0o600)
    note.write_text("changed\n", encoding="utf-8")
    archived.unlink()
    (tree / "new.md").write_text("must disappear\n", encoding="utf-8")
    untouched.write_bytes(b"still-outside-plan")
    restore_backup(
        root / "upgrades" / ("2" * 32) / "backup",
        expected_user_data_dir=root,
    )

    assert settings.read_text(encoding="utf-8") == "before\n"
    assert stat.S_IMODE(settings.stat().st_mode) == 0o640
    assert note.read_text(encoding="utf-8") == "original note\n"
    assert archived.read_text(encoding="utf-8") == "old\n"
    assert not (tree / "new.md").exists()
    assert stat.S_IMODE(tree.stat().st_mode) == 0o750
    assert stat.S_IMODE(nested.stat().st_mode) == 0o710
    assert stat.S_IMODE(note.stat().st_mode) == 0o640
    assert untouched.read_bytes() == b"still-outside-plan"
    assert all(item.source_present for item in manifest.items)


def test_sqlite_backup_captures_wal_and_verifies_logical_snapshot(tmp_path: Path) -> None:
    root = _root(tmp_path)
    database = root / "stores" / "state.sqlite3"
    connection = _create_sqlite(database)
    try:
        assert Path(f"{database}-wal").stat().st_size > 0
        manifest = create_targeted_backup(
            user_data_dir=root,
            operation_id="3" * 32,
            targets=(_target(database, "sqlite"),),
            free_space_margin_bytes=0,
        )
    finally:
        connection.close()

    item = manifest.items[0]
    assert item.kind == "sqlite"
    assert item.logical_sha256 is not None
    artifact = root / "upgrades" / ("3" * 32) / "backup" / str(item.backup_path)
    with sqlite3.connect(artifact) as snapshot:
        assert snapshot.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert snapshot.execute("PRAGMA user_version").fetchone() == (7,)
        assert snapshot.execute("SELECT value FROM records ORDER BY id").fetchall() == [
            ("checkpointed",),
            ("wal-only",),
        ]

    with sqlite3.connect(database) as changed:
        changed.execute("DELETE FROM records")
        changed.execute("INSERT INTO records (value) VALUES ('changed')")
        changed.commit()
    restore_backup(artifact.parents[1], expected_user_data_dir=root)
    with sqlite3.connect(database) as restored:
        assert restored.execute("SELECT value FROM records ORDER BY id").fetchall() == [
            ("checkpointed",),
            ("wal-only",),
        ]


def test_absent_marker_removes_state_created_after_backup(tmp_path: Path) -> None:
    root = _root(tmp_path)
    absent_file = (root / "future" / "generated.json").resolve()
    absent_tree = (root / "future-tree").resolve()
    absent_database = (root / "future-db" / "state.sqlite3").resolve()

    manifest = create_targeted_backup(
        user_data_dir=root,
        operation_id="4" * 32,
        targets=(
            _target(absent_file, "file"),
            _target(absent_tree, "tree"),
            _target(absent_database, "sqlite"),
        ),
        free_space_margin_bytes=0,
    )
    assert all(not item.source_present for item in manifest.items)

    absent_file.parent.mkdir(mode=0o700)
    absent_file.write_text("created by migration", encoding="utf-8")
    absent_tree.mkdir(mode=0o700)
    (absent_tree / "created.txt").write_text("created", encoding="utf-8")
    created_database = _create_sqlite(absent_database)
    created_database.close()
    restore_backup(root / "upgrades" / ("4" * 32) / "backup")
    assert not absent_file.exists()
    assert not absent_tree.exists()
    assert not absent_database.exists()


def test_duplicate_physical_sqlite_target_is_backed_up_once(tmp_path: Path) -> None:
    root = _root(tmp_path)
    database = root / "notifications" / "notifications.sqlite3"
    connection = _create_sqlite(database)
    connection.close()
    target = _target(database, "sqlite")

    manifest = create_targeted_backup(
        user_data_dir=root,
        operation_id="5" * 32,
        targets=(target, target),
        free_space_margin_bytes=0,
    )

    assert len(manifest.items) == 1


@pytest.mark.parametrize("protected", ["installation.json", "upgrades/other/state.json"])
def test_backup_rejects_upgrade_control_state(tmp_path: Path, protected: str) -> None:
    root = _root(tmp_path)
    source = root / protected
    source.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    source.write_text("control", encoding="utf-8")

    with pytest.raises(BackupError, match="control state"):
        create_targeted_backup(
            user_data_dir=root,
            operation_id="6" * 32,
            targets=(_target(source, "file"),),
            free_space_margin_bytes=0,
        )


def test_backup_rejects_parent_child_symlink_and_hardlink_targets(tmp_path: Path) -> None:
    root = _root(tmp_path)
    tree = root / "tree"
    tree.mkdir()
    child = tree / "child.txt"
    child.write_text("content", encoding="utf-8")

    with pytest.raises(BackupError, match="overlap"):
        estimate_backup_bytes(
            (_target(tree, "tree"), _target(child, "file")),
            user_data_dir=root,
        )

    link = root / "linked.txt"
    link.symlink_to(child)
    with pytest.raises(ValidationError):
        BackupTarget(source_path=str(link), kind="file")

    hardlink = root / "hardlink.txt"
    os.link(child, hardlink)
    with pytest.raises(BackupError, match="hard link"):
        create_targeted_backup(
            user_data_dir=root,
            operation_id="7" * 32,
            targets=(_target(child, "file"),),
            free_space_margin_bytes=0,
        )


def test_space_check_includes_safety_margin_and_does_not_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    source = root / "state.bin"
    source.write_bytes(b"12345678")
    monkeypatch.setattr(
        backups.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=100, free=0),
    )

    with pytest.raises(BackupError, match="insufficient free space"):
        create_targeted_backup(
            user_data_dir=root,
            operation_id="8" * 32,
            targets=(_target(source, "file"),),
            free_space_margin_bytes=16,
        )
    assert not (root / "upgrades" / ("8" * 32) / "backup").exists()


def test_verification_rejects_artifact_tamper_and_restore_symlink(tmp_path: Path) -> None:
    root = _root(tmp_path)
    source = root / "state.txt"
    source.write_text("trusted", encoding="utf-8")
    manifest = create_targeted_backup(
        user_data_dir=root,
        operation_id="9" * 32,
        targets=(_target(source, "file"),),
        free_space_margin_bytes=0,
    )
    directory = root / "upgrades" / ("9" * 32) / "backup"
    artifact = directory / str(manifest.items[0].backup_path)
    artifact.write_text("tampered", encoding="utf-8")
    artifact.chmod(0o600)
    with pytest.raises(BackupError, match="manifest"):
        verify_backup(directory)

    shutil.rmtree(directory.parent)
    manifest = create_targeted_backup(
        user_data_dir=root,
        operation_id="a" * 32,
        targets=(_target(source, "file"),),
        free_space_margin_bytes=0,
    )
    source.unlink()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    source.symlink_to(outside)
    with pytest.raises(BackupError, match="symbolic"):
        restore_backup(root / "upgrades" / ("a" * 32) / "backup")
    assert outside.read_text(encoding="utf-8") == "outside"
    assert manifest.items[0].source_path == str(source)


def test_backup_files_and_directories_are_fsynced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    source = root / "state.txt"
    source.write_text("durable", encoding="utf-8")
    kinds: list[str] = []
    real_fsync = os.fsync

    def tracked_fsync(descriptor: int) -> None:
        kinds.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
        real_fsync(descriptor)

    monkeypatch.setattr(backups.os, "fsync", tracked_fsync)
    create_targeted_backup(
        user_data_dir=root,
        operation_id="b" * 32,
        targets=(_target(source, "file"),),
        free_space_margin_bytes=0,
    )
    assert "file" in kinds
    assert "directory" in kinds


def test_retention_keeps_in_progress_and_newest_successful_backup(tmp_path: Path) -> None:
    root = _root(tmp_path)
    source = root / "state.txt"
    source.write_text("one", encoding="utf-8")
    operation_ids = ("c" * 32, "d" * 32, "e" * 32)
    for operation_id in operation_ids:
        create_targeted_backup(
            user_data_dir=root,
            operation_id=operation_id,
            targets=(_target(source, "file"),),
            free_space_margin_bytes=0,
        )

    removed = enforce_backup_retention(
        user_data_dir=root,
        in_progress_operation_id=operation_ids[2],
        successful_operation_ids=operation_ids[:2],
    )

    assert removed == (root / "upgrades" / operation_ids[0] / "backup",)
    assert not removed[0].exists()
    assert (root / "upgrades" / operation_ids[1] / "backup").is_dir()
    assert (root / "upgrades" / operation_ids[2] / "backup").is_dir()
    assert (root / "upgrades" / operation_ids[0]).is_dir()  # journal root is retained


def test_sqlite_backup_round_trip_survives_uri_significant_path_characters(
    tmp_path: Path,
) -> None:
    root = _uri_significant_root(tmp_path)
    assert {"%", "#", "?"} <= set(str(root))
    database = root / "stores" / "state.sqlite3"
    settings = root / "ricky.toml"
    settings.write_text("before\n", encoding="utf-8")
    operation_id = "1" * 32
    directory = root / "upgrades" / operation_id / "backup"

    connection = _create_sqlite(database)
    try:
        assert Path(f"{database}-wal").stat().st_size > 0
        manifest = create_targeted_backup(
            user_data_dir=root,
            operation_id=operation_id,
            targets=(_target(database, "sqlite"), _target(settings, "file")),
            free_space_margin_bytes=0,
        )
    finally:
        connection.close()

    item = next(entry for entry in manifest.items if entry.kind == "sqlite")
    artifact = directory / str(item.backup_path)
    # The snapshot handle closes before the artifact is measured and hashed, so
    # the manifest describes the fully flushed file.
    assert item.size == artifact.stat().st_size
    assert item.sha256 == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert verify_backup(directory, expected_user_data_dir=root) == manifest

    with closing(sqlite3.connect(database)) as changed:
        changed.execute("DELETE FROM records")
        changed.commit()
    settings.write_text("after\n", encoding="utf-8")
    restore_backup(directory, expected_user_data_dir=root)

    assert settings.read_text(encoding="utf-8") == "before\n"
    with closing(sqlite3.connect(database)) as restored:
        assert restored.execute("SELECT value FROM records ORDER BY id").fetchall() == [
            ("checkpointed",),
            ("wal-only",),
        ]


def test_sqlite_backup_reads_the_literal_path_not_its_percent_decoded_twin(
    tmp_path: Path,
) -> None:
    # Only the source directory carries the URI-significant characters, so the
    # backup artifacts keep a plain path. Reading the wrong source is then the
    # single behaviour this test can observe.
    root = _root(tmp_path)
    database = root / "My%20Data" / "state.sqlite3"
    connection = _create_sqlite(database)
    connection.close()

    # `%20` decodes to a space, and a real sibling directory with the decoded
    # name holds a database at the same relative path. An unencoded URI opens
    # this twin instead of the declared source.
    twin = _create_sqlite(root / "My Data" / "state.sqlite3")
    twin.execute("DELETE FROM records")
    twin.execute("INSERT INTO records (value) VALUES ('wrong-file')")
    twin.commit()
    twin.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    twin.close()

    operation_id = "2" * 32
    manifest = create_targeted_backup(
        user_data_dir=root,
        operation_id=operation_id,
        targets=(_target(database, "sqlite"),),
        free_space_margin_bytes=0,
    )

    artifact = root / "upgrades" / operation_id / "backup" / str(manifest.items[0].backup_path)
    with closing(sqlite3.connect(artifact)) as snapshot:
        assert snapshot.execute("SELECT value FROM records ORDER BY id").fetchall() == [
            ("checkpointed",),
            ("wal-only",),
        ]


def test_sqlite_connections_are_closed_by_backup_verification_and_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    database = root / "stores" / "state.sqlite3"
    connection = _create_sqlite(database)
    connection.close()
    operation_id = "f" * 32
    directory = root / "upgrades" / operation_id / "backup"

    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def tracked_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        created = real_connect(*args, **kwargs)
        opened.append(created)
        return created

    monkeypatch.setattr(backups.sqlite3, "connect", tracked_connect)
    create_targeted_backup(
        user_data_dir=root,
        operation_id=operation_id,
        targets=(_target(database, "sqlite"),),
        free_space_margin_bytes=0,
    )
    verify_backup(directory, expected_user_data_dir=root)
    restore_backup(directory, expected_user_data_dir=root)
    monkeypatch.undo()

    # Size estimation, the snapshot source and destination, and every logical
    # digest each open one connection.
    assert len(opened) >= 5
    for created in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            created.execute("SELECT 1")
    assert _descriptors_below(root) == []
