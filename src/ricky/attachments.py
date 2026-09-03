"""Typed, permission-aware local-file attachments shared by outbound adapters."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import tempfile
from contextlib import suppress
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ricky.config import (
    RickySettings,
    config_file,
    find_project_root,
    profile_data_subpath,
    project_data_path,
    secrets_file,
    user_data_path,
    user_data_subpath,
)
from ricky.durable_tasks.artifacts import resolve_task_artifact_source
from ricky.profiles import ProfileName, ProfileScope
from ricky.tools.base import EffectIdentity
from ricky.tools.paths import resolve_host_path

_SAFE_FILENAME = re.compile(r"^[^/\\\x00-\x1f\x7f]+$")
_MEDIA_TYPE = re.compile(r"^[^\s/]+/[^\s/]+$")

TASK_ARTIFACT_ATTACHMENT_HELP = (
    "For an artifact returned by list_task_artifacts, pass an object with task_id, "
    "task_artifact_path, and profile. Never guess or "
    "construct its storage path."
)


class BrowserDownloadRef(BaseModel):
    """Provider-safe logical identity for one confined browser download."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(pattern=r"^browser_download_[0-9a-f]{32}$")
    profile: ProfileName
    filename: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=3, max_length=255)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("filename")
    @classmethod
    def _plain_filename(cls, value: str) -> str:
        if not _SAFE_FILENAME.fullmatch(value) or value in {".", ".."}:
            raise ValueError("browser download filename must be a plain filename")
        return value

    @field_validator("media_type")
    @classmethod
    def _media_type(cls, value: str) -> str:
        value = value.strip().lower()
        if not _MEDIA_TYPE.fullmatch(value):
            raise ValueError("browser download media_type must have type/subtype form")
        return value


class AttachmentInput(BaseModel):
    """An ordinary host file or logical durable-task artifact reference."""

    model_config = ConfigDict(extra="forbid", strict=True)

    path: str | None = Field(
        default=None,
        min_length=1,
        max_length=4_096,
        description=(
            "Absolute, home-relative, or project-relative path for an ordinary local file. "
            "Do not use this field for a file returned by list_task_artifacts."
        ),
    )
    task_id: str | None = Field(
        default=None,
        pattern=r"^task_[0-9a-f]{32}$",
        description=(
            "Durable task id from task tools. For a listed task artifact, supply this "
            "together with task_artifact_path and profile, and omit path."
        ),
    )
    task_artifact_path: str | None = Field(
        default=None,
        min_length=1,
        max_length=4_096,
        description=(
            "Exact logical artifact path from list_task_artifacts. Use with task_id and "
            "profile; never replace it with a guessed storage path."
        ),
    )
    profile: ProfileName | None = Field(
        default=None,
        description=(
            "Owning profile for a listed task artifact. Use with task_id and task_artifact_path."
        ),
    )
    filename: str | None = Field(default=None, min_length=1, max_length=255)
    media_type: str | None = Field(default=None, min_length=3, max_length=255)
    browser_download: BrowserDownloadRef | None = Field(
        default=None,
        description=(
            "Exact logical browser download reference returned by browser_download. "
            "Do not guess or replace it with a physical path."
        ),
    )

    @field_validator("path")
    @classmethod
    def _trim_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("attachment path cannot be empty")
        return value

    @model_validator(mode="after")
    def _validate_source(self) -> AttachmentInput:
        local = self.path is not None
        task_values = (self.task_id, self.task_artifact_path, self.profile)
        task = all(value is not None for value in task_values)
        browser = self.browser_download is not None
        if sum((local, task, browser)) != 1:
            raise ValueError(
                "attachment requires exactly one path, task artifact, or browser download"
            )
        if not task and any(value is not None for value in task_values) and not browser:
            raise ValueError("task attachment requires task_id, task_artifact_path, and profile")
        if browser and any(value is not None for value in (self.task_id, self.task_artifact_path)):
            raise ValueError("browser download attachments cannot include task artifact fields")
        if browser and self.profile is not None:
            raise ValueError("browser download owner comes from its exact logical reference")
        return self

    @field_validator("filename")
    @classmethod
    def _validate_filename(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or not _SAFE_FILENAME.fullmatch(value) or value in {".", ".."}:
            raise ValueError("attachment filename must be a plain filename")
        return value

    @field_validator("media_type")
    @classmethod
    def _validate_media_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if not _MEDIA_TYPE.fullmatch(value):
            raise ValueError("attachment media_type must have type/subtype form")
        return value


AttachmentArgument = AttachmentInput


class StoredAttachment(BaseModel):
    """Immutable attachment snapshot stored relative to ``user_data_dir``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    storage_path: str = Field(min_length=1, max_length=4_096)
    filename: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=3, max_length=255)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("storage_path")
    @classmethod
    def _confined_storage_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or value in {".", ".."} or ".." in path.parts:
            raise ValueError("stored attachment path must stay below user_data_dir")
        return value


class LoadedAttachment(BaseModel):
    """Validated bytes ready for one provider request."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )

    filename: str
    media_type: str
    content: bytes
    sha256: str
    source_path: Path

    @property
    def size_bytes(self) -> int:
        return len(self.content)


class PreparedAttachmentEffect(BaseModel):
    """One immutable, JSON-round-trippable attachment effect payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str = Field(min_length=1, max_length=200)
    identity: EffectIdentity
    permission_summary: str | None = Field(default=None, max_length=8_000)
    attachments: tuple[LoadedAttachment, ...] = ()


class AttachmentSnapshotBatch(BaseModel):
    """Stored references plus the exact paths newly owned by this attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attachments: tuple[StoredAttachment, ...] = ()
    created_storage_paths: tuple[str, ...] = ()


def load_attachments(
    inputs: list[AttachmentInput],
    *,
    cwd: Path,
    settings: RickySettings,
    profile_scope: ProfileScope,
    count_limit: int,
    file_byte_limit: int,
    total_byte_limit: int,
) -> list[LoadedAttachment]:
    """Read ordinary host files or in-scope logical task artifacts."""

    if len(inputs) > count_limit:
        raise ValueError(f"at most {count_limit} attachments are allowed")
    project_root = cwd.resolve()
    loaded: list[LoadedAttachment] = []
    total = 0
    for item in inputs:
        if item.path is not None:
            source = resolve_host_path(project_root, item.path)
            _assert_exportable_host_path(
                source,
                cwd=project_root,
                settings=settings,
                profile_scope=profile_scope,
            )
            shown_source = item.path
        elif item.browser_download is None:
            assert item.task_id is not None
            assert item.task_artifact_path is not None
            assert item.profile is not None
            if not profile_scope.includes(item.profile):
                raise ValueError("task attachment profile is outside the active profile scope")
            source = resolve_task_artifact_source(
                settings,
                profile=item.profile,
                task_id=item.task_id,
                path=item.task_artifact_path,
            )
            shown_source = f"{item.task_id}/{item.task_artifact_path}"
        else:
            download = item.browser_download
            if not profile_scope.includes(download.profile):
                raise ValueError("browser download profile is outside the active profile scope")
            source = browser_download_path(settings, download)
            shown_source = download.id
        if not source.is_file():
            raise ValueError(
                f"attachment is not a regular file: {shown_source}. {TASK_ARTIFACT_ATTACHMENT_HELP}"
            )
        size = source.stat().st_size
        if size > file_byte_limit:
            raise ValueError(
                f"attachment {shown_source!r} exceeds the {file_byte_limit}-byte file limit"
            )
        total += size
        if total > total_byte_limit:
            raise ValueError(f"attachments exceed the {total_byte_limit}-byte total limit")
        content = source.read_bytes()
        if len(content) != size:
            raise ValueError(f"attachment changed while being read: {shown_source}")
        if item.browser_download is not None:
            reference = item.browser_download
            if size != reference.size_bytes:
                raise ValueError("browser download size does not match its logical reference")
            if hashlib.sha256(content).hexdigest() != reference.sha256:
                raise ValueError("browser download digest does not match its logical reference")
        filename = item.filename or source.name
        if item.browser_download is not None and item.filename is None:
            filename = item.browser_download.filename
        if not _SAFE_FILENAME.fullmatch(filename) or filename in {".", ".."}:
            raise ValueError(f"attachment has an unsafe filename: {filename!r}")
        media_type = (
            item.media_type
            or (item.browser_download.media_type if item.browser_download is not None else None)
            or mimetypes.guess_type(filename)[0]
        )
        loaded.append(
            LoadedAttachment(
                filename=filename,
                media_type=media_type or "application/octet-stream",
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
                source_path=source,
            )
        )
    return loaded


def browser_download_path(settings: RickySettings, reference: BrowserDownloadRef) -> Path:
    """Resolve one logical browser download to its fixed owner-confined path."""
    root = profile_data_subpath(settings, reference.profile, settings.browser.download_dir)
    path = root / f"{reference.id}-{reference.filename}"
    if path.is_symlink() or not path.resolve(strict=False).is_relative_to(root.resolve()):
        raise ValueError("browser download path is unsafe")
    return path


def _assert_exportable_host_path(
    source: Path,
    *,
    cwd: Path,
    settings: RickySettings,
    profile_scope: ProfileScope,
) -> None:
    """Reject generic access to Ricky-owned configuration and runtime state."""

    export_roots = (
        user_data_subpath(settings, settings.gmail.download_dir),
        user_data_subpath(settings, settings.slack.download_dir),
        *(
            profile_data_subpath(settings, profile, settings.gmail.download_dir)
            for profile in profile_scope.profiles
        ),
    )
    if any(source.is_relative_to(root) for root in export_roots):
        return

    private_files = {
        config_file(user_data_path(settings)).resolve(),
        secrets_file(user_data_path(settings)).resolve(),
    }
    private_roots = {
        user_data_path(settings),
        project_data_path(settings, find_project_root(cwd.resolve())),
    }
    if (
        source.name == ".secrets.toml"
        or source in private_files
        or any(source.is_relative_to(root) for root in private_roots)
    ):
        raise ValueError("attachment path refers to Ricky-owned private state or configuration")


def attachment_source_label(value: object) -> str:
    """Render one untrusted attachment argument without exposing storage layout."""

    if not isinstance(value, dict):
        return "[invalid attachment]"
    path = value.get("path")
    if path:
        return str(path)
    task_id = value.get("task_id")
    artifact_path = value.get("task_artifact_path")
    if task_id and artifact_path:
        return f"task artifact {task_id}/{artifact_path}"
    browser = value.get("browser_download")
    if isinstance(browser, dict) and browser.get("id"):
        return f"browser download {browser['id']}"
    return "[invalid attachment]"


def snapshot_attachments(
    attachments: list[LoadedAttachment],
    *,
    settings: RickySettings,
    notification_id: str,
) -> list[StoredAttachment]:
    """Atomically snapshot files for restart-safe notification delivery."""

    return list(
        snapshot_attachment_batch(
            attachments,
            settings=settings,
            notification_id=notification_id,
        ).attachments
    )


def snapshot_attachment_batch(
    attachments: list[LoadedAttachment] | tuple[LoadedAttachment, ...],
    *,
    settings: RickySettings,
    notification_id: str,
) -> AttachmentSnapshotBatch:
    """Snapshot once and report only files newly created by this attempt."""

    user_root = user_data_path(settings).resolve()
    relative_root = Path(settings.messaging.attachment_dir) / notification_id
    destination_root = (user_root / relative_root).resolve()
    if not destination_root.is_relative_to(user_root):
        raise ValueError("messaging attachment directory escapes user_data_dir")
    if not attachments:
        return AttachmentSnapshotBatch()
    destination_root.mkdir(parents=True, exist_ok=True)
    stored: list[StoredAttachment] = []
    created: list[str] = []
    try:
        for index, attachment in enumerate(attachments, start=1):
            stored_name = f"{index:03d}-{attachment.sha256[:16]}-{attachment.filename}"
            storage_path = str(relative_root / stored_name)
            destination = destination_root / stored_name
            if _write_atomic_once(destination, attachment.content):
                created.append(storage_path)
            stored.append(
                StoredAttachment(
                    storage_path=storage_path,
                    filename=attachment.filename,
                    media_type=attachment.media_type,
                    size_bytes=attachment.size_bytes,
                    sha256=attachment.sha256,
                )
            )
    except BaseException:
        delete_attachment_snapshots(settings, created)
        raise
    return AttachmentSnapshotBatch(
        attachments=tuple(stored),
        created_storage_paths=tuple(created),
    )


def delete_attachment_snapshots(
    settings: RickySettings,
    storage_paths: list[str] | tuple[str, ...],
) -> None:
    """Delete only attempt-owned notification snapshot files, then empty directories."""

    user_root = user_data_path(settings).resolve()
    attachment_root = (user_root / settings.messaging.attachment_dir).resolve()
    if not attachment_root.is_relative_to(user_root):
        raise ValueError("messaging attachment directory escapes user_data_dir")
    parents: set[Path] = set()
    for storage_path in storage_paths:
        relative = Path(storage_path)
        candidate = (user_root / relative).resolve()
        if relative.is_absolute() or not candidate.is_relative_to(attachment_root):
            raise ValueError("stored attachment cleanup path escapes attachment directory")
        with suppress(FileNotFoundError):
            candidate.unlink()
        parents.add(candidate.parent)
    for parent in sorted(parents, key=lambda item: len(item.parts), reverse=True):
        while parent != attachment_root and parent.is_relative_to(attachment_root):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def read_stored_attachment(
    attachment: StoredAttachment,
    *,
    user_root: Path,
) -> bytes:
    """Read and digest-check one durable attachment without escaping its root."""

    root = user_root.resolve()
    path = (root / attachment.storage_path).resolve()
    if not path.is_relative_to(root):
        raise ValueError("stored attachment path escapes user_data_dir")
    content = path.read_bytes()
    if len(content) != attachment.size_bytes:
        raise ValueError("stored attachment size no longer matches its snapshot")
    if hashlib.sha256(content).hexdigest() != attachment.sha256:
        raise ValueError("stored attachment digest no longer matches its snapshot")
    return content


def _write_atomic_once(path: Path, content: bytes) -> bool:
    """Publish immutable bytes without replacing a prior snapshot."""

    if path.exists():
        if path.read_bytes() != content:
            raise ValueError(f"immutable attachment snapshot collision: {path.name}")
        return False
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != content:
                raise ValueError(f"immutable attachment snapshot collision: {path.name}") from None
            return False
        return True
    except BaseException:
        raise
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
