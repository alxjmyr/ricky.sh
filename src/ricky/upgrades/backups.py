"""Targeted, durable backups for released-installation upgrades.

The upgrade coordinator decides which subsystem-owned paths are mutable.  This
module snapshots only those paths, records enough identity to restore them
exactly, and never interprets a subsystem schema beyond SQLite's own integrity
and logical snapshot boundaries.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import math
import os
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Iterable, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Self
from urllib.parse import quote
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ricky.installation import fsync_directory
from ricky.upgrades.versions import ReleaseVersion, require_installed_release_version

BACKUP_FORMAT_VERSION = 1
BACKUP_MANIFEST_FILENAME = "manifest.json"
DEFAULT_FREE_SPACE_MARGIN_BYTES = 64 * 1024 * 1024
DEFAULT_FREE_SPACE_MARGIN_RATIO = 0.10
_OPERATION_ID_PATTERN = r"^[0-9a-f]{32}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_COPY_CHUNK_BYTES = 1024 * 1024


class BackupError(RuntimeError):
    """A targeted backup or restore failed closed."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class BackupTarget(_StrictModel):
    """One coordinator-declared mutable source."""

    source_path: str = Field(min_length=1, max_length=4_096)
    kind: Literal["sqlite", "file", "tree"]

    @field_validator("source_path")
    @classmethod
    def _absolute_canonical_source(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or path != path.resolve():
            raise ValueError("backup source paths must be absolute and canonical")
        return value


class BackupTreeEntry(_StrictModel):
    """One original file or directory below a tree backup root."""

    relative_path: str = Field(min_length=1, max_length=4_096)
    kind: Literal["file", "directory"]
    size: int = Field(ge=0)
    mode: int = Field(ge=0, le=0o7777)
    sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @field_validator("relative_path")
    @classmethod
    def _confined_relative_path(cls, value: str) -> str:
        return _validate_relative_path(value, label="tree entry")

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> Self:
        if self.kind == "directory" and (self.size != 0 or self.sha256 is not None):
            raise ValueError("directory entries cannot have content size or digest")
        if self.kind == "file" and self.sha256 is None:
            raise ValueError("file entries require a digest")
        return self


class BackupItem(_StrictModel):
    """One immutable artifact and the source identity it can restore."""

    source_path: str = Field(min_length=1, max_length=4_096)
    backup_path: str | None = Field(default=None, min_length=1, max_length=4_096)
    kind: Literal["sqlite", "file", "tree"]
    source_present: bool = True
    size: int = Field(ge=0)
    mode: int | None = Field(default=None, ge=0, le=0o7777)
    sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    logical_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    entries: tuple[BackupTreeEntry, ...] = ()

    @field_validator("source_path")
    @classmethod
    def _absolute_canonical_source(cls, value: str) -> str:
        if not _is_lexically_canonical_absolute(value):
            raise ValueError("backup source paths must be absolute and canonical")
        return value

    @field_validator("backup_path")
    @classmethod
    def _confined_backup_path(cls, value: str | None) -> str | None:
        return None if value is None else _validate_relative_path(value, label="backup")

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> Self:
        if not self.source_present:
            if any(
                (
                    self.backup_path is not None,
                    self.size != 0,
                    self.mode is not None,
                    self.sha256 is not None,
                    self.logical_sha256 is not None,
                    bool(self.entries),
                )
            ):
                raise ValueError("absent backup items cannot describe an artifact")
            return self
        if self.backup_path is None or self.mode is None or self.sha256 is None:
            raise ValueError("present backup items require artifact, mode, and digest metadata")
        if self.kind == "sqlite":
            if self.logical_sha256 is None or self.entries:
                raise ValueError("SQLite backup items require only a logical digest")
        elif self.kind == "file":
            if self.logical_sha256 is not None or self.entries:
                raise ValueError("file backup items cannot have logical or tree metadata")
        elif self.logical_sha256 is not None:
            raise ValueError("tree backup items cannot have a SQLite logical digest")

        paths = tuple(entry.relative_path for entry in self.entries)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("tree entries must be sorted and unique")
        return self


class BackupManifest(_StrictModel):
    """Strict digest-bound description of one complete targeted backup."""

    format_version: Literal[1] = BACKUP_FORMAT_VERSION
    operation_id: str = Field(pattern=_OPERATION_ID_PATTERN)
    user_data_dir: str = Field(min_length=1, max_length=4_096)
    installation_id: str = Field(pattern=_OPERATION_ID_PATTERN)
    source_data_generation: int = Field(ge=1)
    source_software_version: ReleaseVersion
    plan_digest: str = Field(pattern=_SHA256_PATTERN)
    created_at: datetime
    items: tuple[BackupItem, ...]
    total_size: int = Field(ge=0)
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("user_data_dir")
    @classmethod
    def _absolute_canonical_root(cls, value: str) -> str:
        if not _is_lexically_canonical_absolute(value):
            raise ValueError("backup user_data_dir must be absolute and canonical")
        return value

    @field_validator("created_at")
    @classmethod
    def _utc_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("backup created_at must be timezone-aware UTC")
        return value

    @model_validator(mode="after")
    def _verify_complete_manifest(self) -> Self:
        require_installed_release_version(self.source_software_version)
        sources = tuple(item.source_path for item in self.items)
        artifacts = tuple(item.backup_path for item in self.items if item.backup_path is not None)
        if sources != tuple(sorted(sources)) or len(sources) != len(set(sources)):
            raise ValueError("backup items must have sorted, unique source paths")
        if len(artifacts) != len(set(artifacts)):
            raise ValueError("backup artifact paths must be unique")
        root = Path(self.user_data_dir)
        source_paths = tuple(Path(source) for source in sources)
        for source in source_paths:
            if not source.is_relative_to(root) or source == root:
                raise ValueError("backup sources must be strictly below user_data_dir")
            if source == root / "installation.json" or source.is_relative_to(root / "upgrades"):
                raise ValueError("backup sources cannot include upgrade control state")
        for index, source in enumerate(source_paths):
            for other in source_paths[index + 1 :]:
                if other.is_relative_to(source):
                    raise ValueError("backup sources cannot overlap")
        if self.total_size != sum(item.size for item in self.items):
            raise ValueError("backup total_size does not match its items")
        if self.manifest_sha256 != _manifest_digest(self):
            raise ValueError("backup manifest digest does not match its payload")
        return self

    @classmethod
    def create(
        cls,
        *,
        operation_id: str,
        user_data_dir: Path,
        installation_id: str,
        source_data_generation: int,
        source_software_version: ReleaseVersion,
        plan_digest: str,
        items: tuple[BackupItem, ...],
        created_at: datetime | None = None,
    ) -> BackupManifest:
        """Create a canonical manifest and bind its complete payload to a digest."""

        root = user_data_dir.resolve()
        canonical_items = tuple(sorted(items, key=lambda item: item.source_path))
        timestamp = created_at or datetime.now(UTC)
        unsigned = {
            "format_version": BACKUP_FORMAT_VERSION,
            "operation_id": operation_id,
            "user_data_dir": str(root),
            "installation_id": installation_id,
            "source_data_generation": source_data_generation,
            "source_software_version": source_software_version,
            "plan_digest": plan_digest,
            "created_at": timestamp,
            "items": canonical_items,
            "total_size": sum(item.size for item in canonical_items),
        }
        digest = _manifest_payload_digest(unsigned)
        return cls(**unsigned, manifest_sha256=digest)


def backup_directory(user_data_dir: Path, operation_id: str) -> Path:
    """Return the one contract-defined backup directory for an operation."""

    _validate_operation_id(operation_id)
    root = _canonical_existing_root(user_data_dir)
    return root / "upgrades" / operation_id / "backup"


def estimate_backup_bytes(targets: Sequence[BackupTarget], *, user_data_dir: Path) -> int:
    """Estimate bytes for existing declared targets without creating anything."""

    root = _canonical_existing_root(user_data_dir)
    canonical = _validate_targets(targets, root=root)
    return sum(_source_size(path, kind) for path, kind in canonical)


def create_targeted_backup(
    *,
    user_data_dir: Path,
    operation_id: str,
    installation_id: str,
    source_data_generation: int,
    source_software_version: ReleaseVersion,
    plan_digest: str,
    targets: Sequence[BackupTarget],
    free_space_margin_bytes: int = DEFAULT_FREE_SPACE_MARGIN_BYTES,
    free_space_margin_ratio: float = DEFAULT_FREE_SPACE_MARGIN_RATIO,
) -> BackupManifest:
    """Create, verify, fsync, and atomically publish one targeted backup.

    Existing backup directories are accepted only when their strict manifest
    verifies and names the exact same target set. This gives journal resume a
    safe idempotency boundary without adopting partial backup state.
    """

    if free_space_margin_bytes < 0:
        raise BackupError("backup free-space margin cannot be negative")
    if not math.isfinite(free_space_margin_ratio) or free_space_margin_ratio < 0:
        raise BackupError("backup free-space margin ratio must be finite and non-negative")
    _validate_operation_id(operation_id)
    root = _canonical_existing_root(user_data_dir)
    canonical = _validate_targets(targets, root=root)
    final = root / "upgrades" / operation_id / "backup"
    if final.is_symlink():
        raise BackupError("backup directory cannot be a symbolic link")
    if final.exists():
        manifest = verify_backup(
            final,
            expected_user_data_dir=root,
            expected_installation_id=installation_id,
            expected_operation_id=operation_id,
            expected_source_data_generation=source_data_generation,
            expected_source_software_version=source_software_version,
            expected_plan_digest=plan_digest,
        )
        expected = tuple(str(path) for path, _kind in canonical)
        actual = tuple(item.source_path for item in manifest.items)
        if actual != expected:
            raise BackupError("existing backup does not match the requested target set")
        return manifest

    estimate = sum(_source_size(path, kind) for path, kind in canonical)
    margin = max(free_space_margin_bytes, math.ceil(estimate * free_space_margin_ratio))
    required = estimate + margin

    operation_root = _prepare_operation_root(root, operation_id)
    available = shutil.disk_usage(operation_root).free
    if available < required:
        raise BackupError(
            f"insufficient free space for upgrade backup: need {required} bytes, "
            f"have {available} bytes"
        )

    staged = Path(tempfile.mkdtemp(prefix=".backup.", dir=operation_root))
    os.chmod(staged, 0o700)
    try:
        items: list[BackupItem] = []
        for index, (source, kind) in enumerate(canonical):
            if not source.exists():
                items.append(
                    BackupItem(
                        source_path=str(source),
                        kind=kind,
                        source_present=False,
                        size=0,
                    )
                )
                continue
            suffix = ".sqlite3" if kind == "sqlite" else (".file" if kind == "file" else ".tree")
            relative = f"artifacts/{index:04d}{suffix}"
            artifact = staged / relative
            artifact.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _ensure_private_directory(artifact.parent)
            if kind == "sqlite":
                items.append(_backup_sqlite(source, artifact, relative))
            elif kind == "file":
                items.append(_backup_file(source, artifact, relative))
            else:
                items.append(_backup_tree(source, artifact, relative))

        manifest = BackupManifest.create(
            operation_id=operation_id,
            user_data_dir=root,
            installation_id=installation_id,
            source_data_generation=source_data_generation,
            source_software_version=source_software_version,
            plan_digest=plan_digest,
            items=tuple(items),
        )
        _write_manifest(staged / BACKUP_MANIFEST_FILENAME, manifest)
        _fsync_tree(staged)
        verify_backup(
            staged,
            expected_user_data_dir=root,
            expected_installation_id=installation_id,
            expected_operation_id=operation_id,
            expected_source_data_generation=source_data_generation,
            expected_source_software_version=source_software_version,
            expected_plan_digest=plan_digest,
        )
        os.replace(staged, final)
        fsync_directory(operation_root)
        return verify_backup(
            final,
            expected_user_data_dir=root,
            expected_installation_id=installation_id,
            expected_operation_id=operation_id,
            expected_source_data_generation=source_data_generation,
            expected_source_software_version=source_software_version,
            expected_plan_digest=plan_digest,
        )
    except Exception:
        if staged.exists() and not staged.is_symlink():
            shutil.rmtree(staged)
        raise


def load_backup_manifest(backup_dir: Path) -> BackupManifest:
    """Load one strict backup manifest without trusting artifact paths."""

    directory = _canonical_real_directory(backup_dir, label="backup directory")
    path = directory / BACKUP_MANIFEST_FILENAME
    if path.is_symlink():
        raise BackupError("backup manifest cannot be a symbolic link")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise BackupError("backup manifest is unavailable") from exc
    try:
        return BackupManifest.model_validate_json(payload, strict=True)
    except ValueError as exc:
        raise BackupError("backup manifest is invalid") from exc


def _verify_self_bound_backup(backup_dir: Path, *, root: Path) -> BackupManifest:
    """Verify retained bytes using identities already established by its own journal."""

    manifest = load_backup_manifest(backup_dir)
    return verify_backup(
        backup_dir,
        expected_user_data_dir=root,
        expected_installation_id=manifest.installation_id,
        expected_operation_id=manifest.operation_id,
        expected_source_data_generation=manifest.source_data_generation,
        expected_source_software_version=manifest.source_software_version,
        expected_plan_digest=manifest.plan_digest,
    )


def verify_backup(
    backup_dir: Path,
    *,
    expected_user_data_dir: Path,
    expected_installation_id: str,
    expected_operation_id: str,
    expected_source_data_generation: int,
    expected_source_software_version: ReleaseVersion,
    expected_plan_digest: str,
) -> BackupManifest:
    """Verify manifest identity, artifact confinement, modes, hashes, and SQLite data."""

    directory = _canonical_real_directory(backup_dir, label="backup directory")
    manifest = load_backup_manifest(directory)
    root = _canonical_existing_root(expected_user_data_dir)
    if manifest.user_data_dir != str(root):
        raise BackupError("backup belongs to a different user_data_dir")
    if manifest.installation_id != expected_installation_id:
        raise BackupError("backup belongs to a different installation")
    if manifest.operation_id != expected_operation_id:
        raise BackupError("backup belongs to a different upgrade operation")
    if manifest.source_data_generation != expected_source_data_generation:
        raise BackupError("backup belongs to a different source data generation")
    if manifest.source_software_version != expected_source_software_version:
        raise BackupError("backup belongs to a different source software version")
    if manifest.plan_digest != expected_plan_digest:
        raise BackupError("backup belongs to a different migration plan")
    expected_directory = root / "upgrades" / manifest.operation_id / "backup"
    # Staged backups are verified immediately before atomic publication.  They
    # have the same real operation parent and a deliberately temporary name.
    staged = directory.parent == expected_directory.parent and directory.name.startswith(".backup.")
    if directory != expected_directory and not staged:
        raise BackupError("backup directory does not match its manifest identity")

    expected_artifacts: set[Path] = set()
    for item in manifest.items:
        source = Path(item.source_path)
        if not source.is_relative_to(root) or source == root:
            raise BackupError("backup source escapes user_data_dir")
        if not item.source_present:
            continue
        if item.backup_path is None:  # pragma: no cover - strict model establishes this.
            raise BackupError("present backup item has no artifact path")
        artifact = _confined_artifact(directory, item.backup_path)
        expected_artifacts.add(artifact)
        if item.kind == "tree":
            _verify_tree_artifact(artifact, item)
        else:
            _verify_file_artifact(artifact, item)
            if item.kind == "sqlite":
                logical = _sqlite_logical_digest(artifact)
                if logical != item.logical_sha256:
                    raise BackupError("SQLite backup logical content does not match its manifest")

    artifacts_root = directory / "artifacts"
    actual_artifacts = set(artifacts_root.iterdir()) if artifacts_root.exists() else set()
    if actual_artifacts != expected_artifacts:
        raise BackupError("backup contains undeclared or missing artifacts")
    return manifest


def restore_backup(
    backup_dir: Path,
    *,
    expected_user_data_dir: Path,
    expected_installation_id: str,
    expected_operation_id: str,
    expected_source_data_generation: int,
    expected_source_software_version: ReleaseVersion,
    expected_plan_digest: str,
) -> BackupManifest:
    """Verify first, then restore every declared source with exact content and modes."""

    manifest = verify_backup(
        backup_dir,
        expected_user_data_dir=expected_user_data_dir,
        expected_installation_id=expected_installation_id,
        expected_operation_id=expected_operation_id,
        expected_source_data_generation=expected_source_data_generation,
        expected_source_software_version=expected_source_software_version,
        expected_plan_digest=expected_plan_digest,
    )
    root = _canonical_existing_root(Path(manifest.user_data_dir))
    directory = _canonical_real_directory(backup_dir, label="backup directory")

    # Complete all destination safety checks before the first write.
    for item in manifest.items:
        source = Path(item.source_path)
        _validate_restore_destination(source, root=root, kind=item.kind)

    for item in manifest.items:
        source = Path(item.source_path)
        if not item.source_present:
            _restore_absence(source, kind=item.kind)
            continue
        if item.backup_path is None:  # pragma: no cover - strict model establishes this.
            raise BackupError("present backup item has no artifact path")
        artifact = _confined_artifact(directory, item.backup_path)
        if item.kind == "tree":
            _restore_tree(artifact, source, item)
        else:
            if item.kind == "sqlite":
                _remove_sqlite_sidecars(source)
            _restore_file(artifact, source, item)
            if item.kind == "sqlite":
                if _sqlite_logical_digest(source) != item.logical_sha256:
                    raise BackupError("restored SQLite logical content failed verification")
            elif _hash_file(source) != item.sha256:
                raise BackupError("restored file content failed verification")
            if stat.S_IMODE(source.stat().st_mode) != item.mode:
                raise BackupError("restored file mode failed verification")
    return manifest


def enforce_backup_retention(
    *,
    user_data_dir: Path,
    in_progress_operation_id: str | None,
    successful_operation_ids: Iterable[str],
) -> tuple[Path, ...]:
    """Retain only the active backup and newest verified successful backup.

    The coordinator supplies journal-derived successful operation identities;
    this layer never guesses success from directory timestamps.  Only verified
    ``backup/`` trees are removed. Operation journals remain intact.
    """

    root = _canonical_existing_root(user_data_dir)
    if in_progress_operation_id is not None:
        _validate_operation_id(in_progress_operation_id)
    successful = set(successful_operation_ids)
    for operation_id in successful:
        _validate_operation_id(operation_id)

    upgrades = root / "upgrades"
    if upgrades.is_symlink():
        raise BackupError("upgrade state directory cannot be a symbolic link")
    if not upgrades.exists():
        return ()
    _canonical_real_directory(upgrades, label="upgrade state directory")

    backups: list[tuple[Path, BackupManifest]] = []
    for operation_root in sorted(upgrades.iterdir()):
        if operation_root.is_symlink():
            raise BackupError("upgrade operation directory cannot be a symbolic link")
        if not operation_root.is_dir():
            continue
        candidate = operation_root / "backup"
        if candidate.is_symlink():
            raise BackupError("backup directory cannot be a symbolic link")
        if candidate.exists():
            backups.append(
                (
                    candidate,
                    _verify_self_bound_backup(candidate, root=root),
                )
            )

    successful_backups = [entry for entry in backups if entry[1].operation_id in successful]
    newest_success = max(
        successful_backups,
        key=lambda entry: (entry[1].created_at, entry[1].operation_id),
        default=None,
    )
    keep = {in_progress_operation_id}
    if newest_success is not None:
        keep.add(newest_success[1].operation_id)

    removed: list[Path] = []
    for path, manifest in backups:
        if manifest.operation_id in keep:
            continue
        shutil.rmtree(path)
        fsync_directory(path.parent)
        removed.append(path)
    return tuple(removed)


def _backup_sqlite(source: Path, artifact: Path, relative: str) -> BackupItem:
    source_stat = _regular_source_stat(source, label="SQLite source")
    _refuse_sqlite_sidecar_symlinks(source)
    descriptor = os.open(artifact, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(_read_only_uri(source), uri=True)) as source_connection:
            source_connection.execute("PRAGMA query_only = ON")
            # The destination handle closes before the artifact is measured and
            # hashed so the recorded size and digest describe a fully flushed
            # file.
            with closing(sqlite3.connect(artifact)) as destination_connection:
                source_connection.backup(destination_connection)
                # A source in WAL mode can transfer that journal-mode header to
                # the snapshot. Normalize the immutable backup to one standalone
                # database file so verification and restore never depend on
                # transient backup-side WAL/SHM files.
                destination_connection.execute("PRAGMA journal_mode = DELETE")
            source_logical = _sqlite_logical_digest_connection(source_connection)
        backup_logical = _sqlite_logical_digest(artifact)
    except sqlite3.Error as exc:
        raise BackupError("SQLite backup failed structural or integrity validation") from exc
    if source_logical != backup_logical:
        raise BackupError("SQLite backup does not match the source logical content")
    os.chmod(artifact, 0o600)
    _fsync_file(artifact)
    return BackupItem(
        source_path=str(source),
        backup_path=relative,
        kind="sqlite",
        size=artifact.stat().st_size,
        mode=stat.S_IMODE(source_stat.st_mode),
        sha256=_hash_file(artifact),
        logical_sha256=backup_logical,
    )


def _backup_file(source: Path, artifact: Path, relative: str) -> BackupItem:
    source_stat = _regular_source_stat(source, label="file source")
    digest, size = _copy_regular_file(source, artifact, destination_mode=0o600)
    after = source.stat(follow_symlinks=False)
    if _source_identity(source_stat) != _source_identity(after):
        raise BackupError("backup source changed while it was copied")
    return BackupItem(
        source_path=str(source),
        backup_path=relative,
        kind="file",
        size=size,
        mode=stat.S_IMODE(source_stat.st_mode),
        sha256=digest,
    )


def _backup_tree(source: Path, artifact: Path, relative: str) -> BackupItem:
    source_stat = source.stat(follow_symlinks=False)
    if not stat.S_ISDIR(source_stat.st_mode):
        raise BackupError("tree backup source must be a real directory")
    artifact.mkdir(mode=0o700)
    entries: list[BackupTreeEntry] = []
    total_size = 0
    for path in _walk_real_tree(source):
        relative_source = path.relative_to(source).as_posix()
        target = artifact / relative_source
        metadata = path.stat(follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            target.mkdir(mode=0o700)
            entries.append(
                BackupTreeEntry(
                    relative_path=relative_source,
                    kind="directory",
                    size=0,
                    mode=stat.S_IMODE(metadata.st_mode),
                )
            )
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise BackupError("tree backup supports only regular files and directories")
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        digest, size = _copy_regular_file(path, target, destination_mode=0o600)
        after = path.stat(follow_symlinks=False)
        if _source_identity(metadata) != _source_identity(after):
            raise BackupError("backup source changed while it was copied")
        entries.append(
            BackupTreeEntry(
                relative_path=relative_source,
                kind="file",
                size=size,
                mode=stat.S_IMODE(metadata.st_mode),
                sha256=digest,
            )
        )
        total_size += size
    entries.sort(key=lambda entry: entry.relative_path)
    tree_digest = _tree_digest(tuple(entries))
    return BackupItem(
        source_path=str(source),
        backup_path=relative,
        kind="tree",
        size=total_size,
        mode=stat.S_IMODE(source_stat.st_mode),
        sha256=tree_digest,
        entries=tuple(entries),
    )


def _verify_file_artifact(path: Path, item: BackupItem) -> None:
    metadata = _regular_source_stat(path, label="backup artifact")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise BackupError("backup file artifact must use mode 0600")
    if metadata.st_size != item.size or _hash_file(path) != item.sha256:
        raise BackupError("backup file artifact does not match its manifest")


def _verify_tree_artifact(path: Path, item: BackupItem) -> None:
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise BackupError("backup tree artifact must be a private real directory")
    expected = {entry.relative_path: entry for entry in item.entries}
    actual_paths = _walk_real_tree(path)
    if {candidate.relative_to(path).as_posix() for candidate in actual_paths} != set(expected):
        raise BackupError("backup tree contains undeclared or missing entries")
    total_size = 0
    for candidate in actual_paths:
        relative = candidate.relative_to(path).as_posix()
        entry = expected[relative]
        candidate_stat = candidate.stat(follow_symlinks=False)
        if entry.kind == "directory":
            private_directory = stat.S_ISDIR(candidate_stat.st_mode) and (
                stat.S_IMODE(candidate_stat.st_mode) == 0o700
            )
            if not private_directory:
                raise BackupError("backup tree directory is not private")
            continue
        private_file = stat.S_ISREG(candidate_stat.st_mode) and (
            stat.S_IMODE(candidate_stat.st_mode) == 0o600
        )
        if not private_file:
            raise BackupError("backup tree file is not private")
        if candidate_stat.st_size != entry.size or _hash_file(candidate) != entry.sha256:
            raise BackupError("backup tree file does not match its manifest")
        total_size += entry.size
    if total_size != item.size or _tree_digest(item.entries) != item.sha256:
        raise BackupError("backup tree metadata does not match its manifest")


def _restore_file(artifact: Path, source: Path, item: BackupItem) -> None:
    if item.mode is None:  # pragma: no cover - strict model establishes this.
        raise BackupError("present backup item has no source mode")
    source.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _ensure_no_symlink_components(source.parent, stop=Path(item.source_path).anchor)
    descriptor, rendered = tempfile.mkstemp(prefix=f".{source.name}.", dir=source.parent)
    os.close(descriptor)
    staged = Path(rendered)
    try:
        _copy_regular_file(artifact, staged, destination_mode=item.mode, replace=True)
        os.replace(staged, source)
        fsync_directory(source.parent)
    finally:
        if staged.exists() and not staged.is_symlink():
            staged.unlink()


def _restore_absence(source: Path, *, kind: str) -> None:
    """Restore an originally absent target by atomically detaching created state."""

    if source.is_symlink():
        raise BackupError("restore refuses a symbolic-link destination")
    quarantine = source.parent / f".{source.name}.remove.{uuid4().hex}"
    if source.exists():
        metadata = source.stat(follow_symlinks=False)
        if kind == "tree":
            if not stat.S_ISDIR(metadata.st_mode):
                raise BackupError("created restore target has an incompatible file type")
            _walk_real_tree(source)
        elif not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise BackupError("created restore target is not a private regular file")
        os.replace(source, quarantine)
        fsync_directory(source.parent)
        if quarantine.is_dir():
            shutil.rmtree(quarantine)
        else:
            quarantine.unlink()
        fsync_directory(source.parent)
    if kind == "sqlite":
        _remove_sqlite_sidecars(source)


def _restore_tree(artifact: Path, source: Path, item: BackupItem) -> None:
    if item.mode is None:  # pragma: no cover - strict model establishes this.
        raise BackupError("present backup item has no source mode")
    source.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=f".{source.name}.restore.", dir=source.parent))
    quarantine = source.parent / f".{source.name}.replaced.{uuid4().hex}"
    try:
        os.chmod(staged, item.mode)
        expected = {entry.relative_path: entry for entry in item.entries}
        for relative in sorted(expected, key=lambda value: (value.count("/"), value)):
            entry = expected[relative]
            source_artifact = artifact / relative
            destination = staged / relative
            if entry.kind == "directory":
                destination.mkdir(mode=entry.mode, parents=True, exist_ok=True)
                os.chmod(destination, entry.mode)
            else:
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _copy_regular_file(
                    source_artifact,
                    destination,
                    destination_mode=entry.mode,
                )
        # Parent creation above can temporarily override a declared directory's
        # mode, so apply directory modes again from deepest to shallowest.
        for relative in sorted(expected, key=lambda value: (-value.count("/"), value)):
            entry = expected[relative]
            if entry.kind == "directory":
                os.chmod(staged / relative, entry.mode)
        os.chmod(staged, item.mode)
        _fsync_tree(staged)

        had_source = source.exists()
        exchanged = had_source and _atomic_exchange_paths(staged, source)
        if not exchanged:
            if had_source:
                os.replace(source, quarantine)
            try:
                os.replace(staged, source)
            except Exception:
                if had_source and quarantine.exists() and not source.exists():
                    os.replace(quarantine, source)
                raise
        fsync_directory(source.parent)
        if exchanged:
            # RENAME_EXCHANGE leaves the replaced tree at the staging name.
            shutil.rmtree(staged)
            fsync_directory(source.parent)
        if quarantine.exists():
            shutil.rmtree(quarantine)
            fsync_directory(source.parent)
        _verify_restored_tree(source, item)
    finally:
        if staged.exists() and not staged.is_symlink():
            shutil.rmtree(staged)


def _verify_restored_tree(source: Path, item: BackupItem) -> None:
    if stat.S_IMODE(source.stat(follow_symlinks=False).st_mode) != item.mode:
        raise BackupError("restored tree root mode failed verification")
    expected = {entry.relative_path: entry for entry in item.entries}
    actual = _walk_real_tree(source)
    if {path.relative_to(source).as_posix() for path in actual} != set(expected):
        raise BackupError("restored tree content failed exact verification")
    for path in actual:
        entry = expected[path.relative_to(source).as_posix()]
        metadata = path.stat(follow_symlinks=False)
        if stat.S_IMODE(metadata.st_mode) != entry.mode:
            raise BackupError("restored tree entry mode failed verification")
        if entry.kind == "file" and (
            metadata.st_size != entry.size or _hash_file(path) != entry.sha256
        ):
            raise BackupError("restored tree file failed content verification")


def _remove_sqlite_sidecars(source: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{source}{suffix}")
        if sidecar.is_symlink():
            raise BackupError("SQLite restore refuses a symbolic-link sidecar")
        if not sidecar.exists():
            continue
        metadata = sidecar.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise BackupError("SQLite restore refuses a non-file sidecar")
        sidecar.unlink()
    if source.parent.is_dir():
        fsync_directory(source.parent)


def _atomic_exchange_paths(left: Path, right: Path) -> bool:
    """Atomically exchange two Linux paths, or report an unsupported filesystem."""

    try:
        library = ctypes.CDLL(None, use_errno=True)
        renameat2 = library.renameat2
    except (AttributeError, OSError):  # pragma: no cover - old non-Linux libc.
        return False
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_exchange = 2
    result = renameat2(
        at_fdcwd,
        os.fsencode(left),
        at_fdcwd,
        os.fsencode(right),
        rename_exchange,
    )
    if result == 0:
        return True
    error_number = ctypes.get_errno()
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.EXDEV}:
        return False
    raise BackupError("atomic tree restore exchange failed") from OSError(
        error_number, os.strerror(error_number)
    )


def _read_only_uri(path: Path) -> str:
    # SQLite percent-decodes URI paths, so a literal `%`, `#`, or `?` in a real
    # path would otherwise select a different file or inject query parameters.
    return f"file:{quote(str(path))}?mode=ro"


def _sqlite_logical_digest(path: Path) -> str:
    _regular_source_stat(path, label="SQLite database")
    try:
        with closing(sqlite3.connect(_read_only_uri(path), uri=True)) as connection:
            connection.execute("PRAGMA query_only = ON")
            return _sqlite_logical_digest_connection(connection)
    except sqlite3.Error as exc:
        raise BackupError("SQLite database failed logical verification") from exc


def _sqlite_logical_digest_connection(connection: sqlite3.Connection) -> str:
    check = connection.execute("PRAGMA quick_check").fetchall()
    if check != [("ok",)]:
        raise BackupError("SQLite database failed quick_check")
    digest = hashlib.sha256()
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    digest.update(f"user_version:{user_version}\napplication_id:{application_id}\n".encode())
    for statement in connection.iterdump():
        digest.update(statement.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _validate_targets(
    targets: Sequence[BackupTarget], *, root: Path
) -> tuple[tuple[Path, Literal["sqlite", "file", "tree"]], ...]:
    canonical: list[tuple[Path, Literal["sqlite", "file", "tree"]]] = []
    for target in targets:
        path = Path(target.source_path)
        if not path.is_relative_to(root) or path == root:
            raise BackupError("backup source must be strictly below user_data_dir")
        if path == root / "installation.json" or path.is_relative_to(root / "upgrades"):
            raise BackupError("backup targets cannot include upgrade control state")
        _ensure_no_symlink_components(path, stop=root)
        if not path.exists():
            canonical.append((path, target.kind))
            continue
        metadata = path.stat(follow_symlinks=False)
        if target.kind in {"sqlite", "file"} and not stat.S_ISREG(metadata.st_mode):
            raise BackupError("declared file backup source is not a regular file")
        if target.kind == "tree" and not stat.S_ISDIR(metadata.st_mode):
            raise BackupError("declared tree backup source is not a directory")
        canonical.append((path, target.kind))
    canonical.sort(key=lambda item: str(item[0]))
    deduplicated: list[tuple[Path, Literal["sqlite", "file", "tree"]]] = []
    for path, kind in canonical:
        if deduplicated and deduplicated[-1][0] == path:
            if deduplicated[-1][1] != kind:
                raise BackupError("one backup source cannot have conflicting target kinds")
            continue
        deduplicated.append((path, kind))
    canonical = deduplicated
    paths = [path for path, _kind in canonical]
    for index, path in enumerate(paths):
        for other in paths[index + 1 :]:
            if other.is_relative_to(path):
                raise BackupError("backup source paths cannot overlap")
    return tuple(canonical)


def _source_size(path: Path, kind: str) -> int:
    if not path.exists():
        return 0
    if kind == "tree":
        return sum(
            candidate.stat(follow_symlinks=False).st_size
            for candidate in _walk_real_tree(path)
            if candidate.is_file() and not candidate.is_symlink()
        )
    size = path.stat(follow_symlinks=False).st_size
    if kind == "sqlite":
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{path}{suffix}")
            if sidecar.is_symlink():
                raise BackupError("SQLite backup refuses a symbolic-link sidecar")
            if sidecar.exists():
                sidecar_stat = sidecar.stat(follow_symlinks=False)
                if not stat.S_ISREG(sidecar_stat.st_mode):
                    raise BackupError("SQLite backup refuses a non-file sidecar")
                if sidecar_stat.st_nlink != 1:
                    raise BackupError("SQLite backup refuses a hard-linked sidecar")
                size += sidecar_stat.st_size
        try:
            with closing(sqlite3.connect(_read_only_uri(path), uri=True)) as connection:
                page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
                page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            size = max(size, page_count * page_size)
        except sqlite3.Error as exc:
            raise BackupError("SQLite backup size could not be estimated safely") from exc
    return size


def _prepare_operation_root(root: Path, operation_id: str) -> Path:
    upgrades = root / "upgrades"
    if upgrades.is_symlink():
        raise BackupError("upgrade state directory cannot be a symbolic link")
    upgrades.mkdir(mode=0o700, exist_ok=True)
    _ensure_private_directory(upgrades)
    operation_root = upgrades / operation_id
    if operation_root.is_symlink():
        raise BackupError("upgrade operation directory cannot be a symbolic link")
    operation_root.mkdir(mode=0o700, exist_ok=True)
    _ensure_private_directory(operation_root)
    fsync_directory(root)
    fsync_directory(upgrades)
    return operation_root


def _validate_restore_destination(source: Path, *, root: Path, kind: str) -> None:
    if not source.is_relative_to(root) or source == root:
        raise BackupError("restore destination escapes user_data_dir")
    if source == root / "installation.json" or source.is_relative_to(root / "upgrades"):
        raise BackupError("restore destination cannot be upgrade control state")
    _ensure_no_symlink_components(source, stop=root)
    if source.exists():
        metadata = source.stat(follow_symlinks=False)
        expected_directory = kind == "tree"
        if expected_directory != stat.S_ISDIR(metadata.st_mode):
            raise BackupError("restore destination has an incompatible file type")
        if expected_directory:
            _walk_real_tree(source)
        elif not stat.S_ISREG(metadata.st_mode):
            raise BackupError("restore destination must be a regular file")
        elif metadata.st_nlink != 1:
            raise BackupError("restore destination cannot be hard linked")
    if kind == "sqlite":
        _refuse_sqlite_sidecar_symlinks(source)


def _confined_artifact(directory: Path, relative: str) -> Path:
    validated = _validate_relative_path(relative, label="backup")
    artifact = directory.joinpath(*PurePosixPath(validated).parts)
    if not artifact.is_relative_to(directory) or artifact.is_symlink():
        raise BackupError("backup artifact escapes its directory or is a symbolic link")
    _ensure_no_symlink_components(artifact, stop=directory)
    return artifact


def _walk_real_tree(root: Path) -> list[Path]:
    if root.is_symlink():
        raise BackupError("backup and restore trees cannot contain symbolic links")
    paths: list[Path] = []
    try:
        for current, directories, files in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            directories.sort()
            files.sort()
            for name in directories:
                child = current_path / name
                if child.is_symlink():
                    raise BackupError("backup and restore trees cannot contain symbolic links")
                metadata = child.stat(follow_symlinks=False)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise BackupError("backup and restore trees contain an invalid directory")
                paths.append(child)
            for name in files:
                child = current_path / name
                if child.is_symlink():
                    raise BackupError("backup and restore trees cannot contain symbolic links")
                metadata = child.stat(follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode):
                    raise BackupError("backup and restore trees contain a special file")
                if metadata.st_nlink != 1:
                    raise BackupError("backup and restore trees cannot contain hard links")
                paths.append(child)
    except OSError as exc:
        raise BackupError("backup tree could not be inspected safely") from exc
    paths.sort(key=lambda path: path.relative_to(root).as_posix())
    return paths


def _copy_regular_file(
    source: Path,
    destination: Path,
    *,
    destination_mode: int,
    replace: bool = False,
) -> tuple[str, int]:
    source_stat = _regular_source_stat(source, label="copy source")
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if replace else os.O_EXCL)
    descriptor = os.open(destination, flags, destination_mode)
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as input_stream, os.fdopen(descriptor, "wb") as output_stream:
            descriptor = -1
            while chunk := input_stream.read(_COPY_CHUNK_BYTES):
                output_stream.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            output_stream.flush()
            os.fchmod(output_stream.fileno(), destination_mode)
            os.fsync(output_stream.fileno())
    except OSError as exc:
        raise BackupError("backup file copy failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if size != source_stat.st_size:
        raise BackupError("backup source changed size while it was copied")
    return digest.hexdigest(), size


def _regular_source_stat(path: Path, *, label: str) -> os.stat_result:
    if path.is_symlink():
        raise BackupError(f"{label} cannot be a symbolic link")
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise BackupError(f"{label} is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise BackupError(f"{label} must be a regular file")
    if metadata.st_nlink != 1:
        raise BackupError(f"{label} cannot be hard linked")
    return metadata


def _source_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        stat.S_IMODE(metadata.st_mode),
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(_COPY_CHUNK_BYTES):
                digest.update(chunk)
    except OSError as exc:
        raise BackupError("backup artifact could not be hashed") from exc
    return digest.hexdigest()


def _tree_digest(entries: tuple[BackupTreeEntry, ...]) -> str:
    payload = [entry.model_dump(mode="json") for entry in entries]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _manifest_digest(manifest: BackupManifest) -> str:
    payload = manifest.model_dump(mode="python", exclude={"manifest_sha256"})
    return _manifest_payload_digest(payload)


def _manifest_payload_digest(payload: object) -> str:
    if isinstance(payload, dict):
        serializable = dict(payload)
        timestamp = serializable.get("created_at")
        if isinstance(timestamp, datetime):
            serializable["created_at"] = timestamp.isoformat().replace("+00:00", "Z")
        source_version = serializable.get("source_software_version")
        if isinstance(source_version, ReleaseVersion):
            serializable["source_software_version"] = str(source_version)
        items = serializable.get("items")
        if isinstance(items, tuple):
            serializable["items"] = [
                item.model_dump(mode="json") if isinstance(item, BaseModel) else item
                for item in items
            ]
    else:  # pragma: no cover - only mapping payloads are constructed internally.
        serializable = payload
    encoded = json.dumps(serializable, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _write_manifest(path: Path, manifest: BackupManifest) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        payload = manifest.model_dump_json(indent=2).encode("utf-8") + b"\n"
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    paths = _walk_real_tree(root)
    for path in paths:
        metadata = path.stat(follow_symlinks=False)
        if stat.S_ISREG(metadata.st_mode):
            _fsync_file(path)
    directories = [path for path in paths if path.is_dir()]
    for path in sorted(directories, key=lambda value: len(value.parts), reverse=True):
        fsync_directory(path)
    fsync_directory(root)


def _ensure_private_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise BackupError("backup directory must be a real directory")
    os.chmod(path, 0o700)


def _canonical_existing_root(path: Path) -> Path:
    if not path.is_absolute():
        raise BackupError("user_data_dir must be absolute")
    if path.is_symlink():
        raise BackupError("user_data_dir cannot be a symbolic link")
    canonical = path.resolve()
    if canonical != path or not canonical.is_dir():
        raise BackupError("user_data_dir must be an existing canonical directory")
    return canonical


def _canonical_real_directory(path: Path, *, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise BackupError(f"{label} must be an absolute real directory")
    canonical = path.resolve()
    if canonical != path or not canonical.is_dir():
        raise BackupError(f"{label} must be an existing canonical directory")
    return canonical


def _ensure_no_symlink_components(path: Path, *, stop: Path | str) -> None:
    boundary = Path(stop)
    current = path
    while current != boundary:
        if current.is_symlink():
            raise BackupError("backup and restore paths cannot contain symbolic links")
        parent = current.parent
        if parent == current:
            raise BackupError("path does not descend from its confinement boundary")
        current = parent
    if boundary.is_symlink():
        raise BackupError("confinement boundary cannot be a symbolic link")


def _refuse_sqlite_sidecar_symlinks(source: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(f"{source}{suffix}").is_symlink():
            raise BackupError("SQLite sidecars cannot be symbolic links")


def _validate_relative_path(value: str, *, label: str) -> str:
    if "\\" in value or "\x00" in value:
        raise ValueError(f"{label} path must use safe POSIX components")
    path = PurePosixPath(value)
    unsafe_component = any(part in {"", ".", ".."} for part in path.parts)
    if path.is_absolute() or value in {"", "."} or unsafe_component:
        raise ValueError(f"{label} path must be confined and relative")
    if path.as_posix() != value:
        raise ValueError(f"{label} path must be canonical")
    return value


def _is_lexically_canonical_absolute(value: str) -> bool:
    path = Path(value)
    return (
        path.is_absolute()
        and "\x00" not in value
        and os.path.normpath(value) == value
        and all(part not in {"", ".", ".."} for part in path.parts[1:])
    )


def _validate_operation_id(value: str) -> None:
    if len(value) != 32 or any(character not in "0123456789abcdef" for character in value):
        raise BackupError("upgrade operation_id must be 32 lowercase hexadecimal characters")


__all__ = [
    "BACKUP_FORMAT_VERSION",
    "BACKUP_MANIFEST_FILENAME",
    "BackupError",
    "BackupItem",
    "BackupManifest",
    "BackupTarget",
    "BackupTreeEntry",
    "backup_directory",
    "create_targeted_backup",
    "enforce_backup_retention",
    "estimate_backup_bytes",
    "load_backup_manifest",
    "restore_backup",
    "verify_backup",
]
