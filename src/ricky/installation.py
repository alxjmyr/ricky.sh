"""Installation bootstrap, initialization, and private-data lifecycle.

The Python environment containing Ricky is owned by the external installer
(``uv tool`` for released builds).  This module owns only Ricky's machine-local
bootstrap pointer and the initialized ``user_data_dir`` scaffold.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
import time
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ricky import __version__

try:  # pragma: no cover - released installation support is Linux/POSIX only.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

BOOTSTRAP_FILENAME = "bootstrap.toml"
INSTALLATION_FILENAME = "installation.json"
INSTALLATION_FORMAT_VERSION = 1
BOOTSTRAP_FORMAT_VERSION = 1
_INIT_LOCK_FILENAME = ".init.lock"
_OPERATION_INFO_FILENAME = ".operation.json"
CURRENT_DATA_GENERATION = 1

type MigrationState = Literal[
    "clean",
    "prepared",
    "software_replaced",
    "migrating",
    "failed",
    "rolling_back",
]
type OperationLockMode = Literal["shared", "exclusive"]


class InstallationError(ValueError):
    """An installation lifecycle operation failed safely."""


class BootstrapPointer(BaseModel):
    """Non-secret, machine-local pointer to one initialized data root."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    format_version: Literal[1] = BOOTSTRAP_FORMAT_VERSION
    user_data_dir: str = Field(min_length=1, max_length=4_096)
    installation_id: str = Field(pattern=r"^[0-9a-f]{32}$")

    @field_validator("user_data_dir")
    @classmethod
    def _absolute_root(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or path != path.resolve():
            raise ValueError("bootstrap user_data_dir must be an absolute canonical path")
        return value


class InstallationManifest(BaseModel):
    """Versioned identity and compatibility marker for one Ricky data root."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    format_version: Literal[1] = INSTALLATION_FORMAT_VERSION
    installation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    created_by_version: str = Field(min_length=1, max_length=100)
    # The version of the last lifecycle operation that wrote this manifest while
    # holding the exclusive installation lock. It is an audit identity, never a
    # compatibility gate; ordinary commands read the installation without
    # rewriting it, so this is not "the last version that ran".
    last_lifecycle_version: str = Field(min_length=1, max_length=100)
    created_at: datetime
    data_generation: int = Field(default=CURRENT_DATA_GENERATION, ge=1)
    migration_state: MigrationState = "clean"
    operation_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")

    @field_validator("created_at")
    @classmethod
    def _utc_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("installation created_at must be timezone-aware UTC")
        return value

    @model_validator(mode="after")
    def _operation_matches_state(self) -> InstallationManifest:
        if self.migration_state == "clean" and self.operation_id is not None:
            raise ValueError("a clean installation cannot have an active operation_id")
        if self.migration_state != "clean" and self.operation_id is None:
            raise ValueError("a non-clean installation requires an operation_id")
        return self


class InstallationOperationInfo(BaseModel):
    """Sanitized identity for one exclusive lifecycle operation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operation: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_-]+$")
    operation_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    pid: int = Field(ge=1)
    started_at: datetime

    @field_validator("started_at")
    @classmethod
    def _utc_started_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("operation started_at must be timezone-aware UTC")
        return value


class InstallationOperationLock:
    """Held kernel-lock authority for one Ricky process or lifecycle operation."""

    def __init__(self, *, descriptor: int, path: Path, mode: OperationLockMode) -> None:
        self.descriptor = descriptor
        self.path = path
        self.mode = mode
        self._transferred = False

    @property
    def transferred(self) -> bool:
        """Whether final lock cleanup was delegated to an inherited child descriptor."""

        return self._transferred

    def transfer_to_child(self) -> int:
        """Detach exclusive cleanup after the descriptor is inherited by a target child."""

        if self.mode != "exclusive":
            raise InstallationError("only an exclusive operation lock can be transferred")
        if self._transferred:
            raise InstallationError("installation operation lock was already transferred")
        self._transferred = True
        return self.descriptor

    def require_owned_exclusive(self) -> None:
        """Fail unless this process still owns Ricky's live exclusive lock."""

        if self.mode != "exclusive" or self._transferred:
            raise InstallationError("lifecycle mutation requires an owned exclusive operation lock")
        try:
            descriptor_stat = os.fstat(self.descriptor)
            expected_stat = self.path.stat()
        except OSError as exc:
            raise InstallationError("installation operation lock is no longer active") from exc
        if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (
            expected_stat.st_dev,
            expected_stat.st_ino,
        ):
            raise InstallationError("installation operation lock identity changed")


class InitializationResult(BaseModel):
    """Serializable result of an idempotent initialization."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    created: bool
    pointer_created: bool
    user_data_dir: str
    bootstrap_path: str
    installation_id: str


class PurgeResult(BaseModel):
    """Serializable result of an exact installation-data purge."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    user_data_dir: str
    bootstrap_path: str
    installation_id: str


class CrontabReader(Protocol):
    """The managed-crontab read surface the removal lifecycle depends on.

    Declaring only the read keeps the host command an injectable dependency of
    the lifecycle API, so a caller or test supplies its own backend and no
    lifecycle check silently depends on a real ``crontab`` program.
    """

    async def read(self) -> str: ...


def bootstrap_config_dir() -> Path:
    """Return Ricky's fixed Linux/XDG bootstrap directory."""

    configured = os.environ.get("XDG_CONFIG_HOME")
    root = Path(configured).expanduser() if configured else Path.home() / ".config"
    if not root.is_absolute():
        raise InstallationError("XDG_CONFIG_HOME must be an absolute path")
    selected = root.resolve() / "ricky"
    # ``Path.exists`` follows the link, so it hides a dangling symbolic link,
    # which is the case that lets an unrelated path choose where Ricky writes.
    if selected.is_symlink():
        raise InstallationError("Ricky bootstrap directory cannot be a symbolic link")
    return selected


def bootstrap_file() -> Path:
    """Return the fixed bootstrap pointer path."""

    return bootstrap_config_dir() / BOOTSTRAP_FILENAME


def read_bootstrap_pointer(path: Path | None = None) -> BootstrapPointer | None:
    """Read and validate the optional bootstrap pointer without creating paths."""

    selected = path or bootstrap_file()
    if selected.is_symlink():
        raise InstallationError("Ricky bootstrap pointer cannot be a symbolic link")
    try:
        with selected.open("rb") as stream:
            document = tomllib.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise InstallationError(f"invalid Ricky bootstrap pointer: {selected}") from exc
    try:
        return BootstrapPointer.model_validate(document)
    except ValueError as exc:
        raise InstallationError(f"invalid Ricky bootstrap pointer: {selected}") from exc


def resolve_bootstrap_user_data_dir(explicit: str | Path | None = None) -> Path:
    """Resolve one immutable data root from pointer, explicit input, env, or default.

    Once a pointer exists it is authoritative. An explicit value or environment
    value may confirm that root but cannot silently select a different one.
    """

    pointer = read_bootstrap_pointer()
    requested = explicit
    programmatic_override = explicit is not None
    if requested is None:
        requested = os.environ.get("RICKY_USER_DATA_DIR")
    canonical_requested = (
        None
        if requested is None
        else (
            Path(requested).expanduser().resolve()
            if programmatic_override
            else _canonical_target(requested)
        )
    )
    if pointer is not None:
        selected = Path(pointer.user_data_dir)
        if canonical_requested is not None and canonical_requested != selected:
            raise InstallationError(
                f"Ricky is initialized at {selected}; the requested user_data_dir conflicts "
                f"with {bootstrap_file()}"
            )
        return selected
    return canonical_requested or _canonical_target("~/.ricky")


def _plan_initialization(
    pointer_path: Path, requested: str | Path | None
) -> tuple[BootstrapPointer | None, Path, InstallationManifest | None]:
    """Select and fully validate one initialization without writing anything."""

    pointer = read_bootstrap_pointer(pointer_path)
    if pointer is not None:
        root = Path(pointer.user_data_dir)
        if requested is not None and _canonical_target(requested) != root:
            raise InstallationError(
                f"Ricky is already initialized at {root}; relocating user_data_dir is not supported"
            )
    else:
        root = _canonical_target(requested if requested is not None else "~/.ricky")

    _validate_target(root)
    manifest = _read_valid_installation(root)
    # Prove identity before writing anything. Creating a scaffold first and
    # then rejecting its id would leave a permanently unusable root that
    # neither `ricky init` nor `ricky data purge` could reconcile.
    if (
        pointer is not None
        and manifest is not None
        and pointer.installation_id != manifest.installation_id
    ):
        raise InstallationError(
            "bootstrap pointer installation_id does not match the selected installation"
        )
    if manifest is not None and manifest.migration_state != "clean":
        raise InstallationError(
            f"installation upgrade {manifest.operation_id} is {manifest.migration_state}; "
            "run `ricky upgrade --resume` or `ricky upgrade --rollback`"
        )
    return pointer, root, manifest


def initialize_installation(
    target: str | Path | None = None,
    *,
    now: datetime | None = None,
) -> InitializationResult:
    """Atomically create or idempotently verify the minimal Ricky scaffold."""

    pointer_path = bootstrap_file()
    requested = target if target is not None else os.environ.get("RICKY_USER_DATA_DIR")
    # Refuse a rejected target before the lock creates the bootstrap directory,
    # so an initialization that never starts leaves the host untouched. This
    # plan is advisory because no lock is held yet; the plan recomputed under
    # the lock is the authoritative one, and nothing is written before it.
    _plan_initialization(pointer_path, requested)
    with _initialization_lock(pointer_path.parent):
        pointer, root, manifest = _plan_initialization(pointer_path, requested)

        created = False
        if manifest is None:
            manifest = InstallationManifest(
                # An existing pointer stays authoritative, so a root that was
                # removed outside Ricky is rebuilt under its recorded identity.
                installation_id=pointer.installation_id if pointer is not None else uuid4().hex,
                created_by_version=__version__,
                last_lifecycle_version=__version__,
                created_at=now or datetime.now(UTC),
            )
            _create_scaffold(root, manifest)
            created = True

        pointer_created = pointer is None
        if pointer_created:
            _write_pointer(
                pointer_path,
                BootstrapPointer(
                    user_data_dir=str(root),
                    installation_id=manifest.installation_id,
                ),
            )

        return InitializationResult(
            created=created,
            pointer_created=pointer_created,
            user_data_dir=str(root),
            bootstrap_path=str(pointer_path),
            installation_id=manifest.installation_id,
        )


def require_installation() -> tuple[BootstrapPointer, InstallationManifest]:
    """Return the current exact pointer and manifest in any known lifecycle state."""

    pointer = read_bootstrap_pointer()
    if pointer is None:
        raise InstallationError("Ricky is not initialized; run `ricky init`")
    root = Path(pointer.user_data_dir)
    _validate_target(root)
    manifest = _read_valid_installation(root)
    if manifest is None:  # pragma: no cover - nonempty valid roots always return a manifest.
        raise InstallationError(f"Ricky installation is missing at {root}")
    if manifest.installation_id != pointer.installation_id:
        raise InstallationError("bootstrap pointer does not match the Ricky installation")
    return pointer, manifest


def require_compatible_installation(
    *,
    supported_generations: frozenset[int] = frozenset({CURRENT_DATA_GENERATION}),
) -> tuple[BootstrapPointer, InstallationManifest]:
    """Require clean installation data supported by the running Ricky release."""

    pointer, manifest = require_installation()
    if manifest.migration_state != "clean":
        operation = manifest.operation_id or "unknown"
        raise InstallationError(
            f"installation upgrade {operation} is {manifest.migration_state}; "
            "run `ricky upgrade --resume` or `ricky upgrade --rollback`"
        )
    if manifest.data_generation not in supported_generations:
        raise InstallationError(
            f"this Ricky release cannot open data generation {manifest.data_generation}; "
            "install or upgrade to a compatible release"
        )
    return pointer, manifest


def _write_installation_manifest(root: Path, manifest: InstallationManifest) -> None:
    """Atomically persist one already validated installation compatibility gate."""

    canonical = root.expanduser().resolve()
    _validate_target(canonical)
    path = canonical / INSTALLATION_FILENAME
    if path.is_symlink() or not path.is_file():
        raise InstallationError(f"invalid installation manifest: {path}")
    write_private_file(
        path,
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
    )


def transition_installation_manifest(
    root: Path,
    current: InstallationManifest,
    *,
    lock: InstallationOperationLock,
    migration_state: MigrationState,
    operation_id: str | None,
    data_generation: int | None = None,
    lifecycle_version: str = __version__,
) -> InstallationManifest:
    """Validate and atomically commit one lifecycle transition under caller-held EX lock."""

    lock.require_owned_exclusive()
    if lock.path != bootstrap_config_dir() / _INIT_LOCK_FILENAME:
        raise InstallationError("manifest transition lock is not Ricky's host operation lock")
    if (
        lifecycle_version == "0.0.0"
        or re.fullmatch(
            r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
            lifecycle_version,
        )
        is None
    ):
        raise InstallationError("lifecycle_version must identify an installed Ricky release")
    allowed: dict[MigrationState, frozenset[MigrationState]] = {
        "clean": frozenset({"prepared"}),
        # Failures before target software is verified remain precisely
        # recoverable as ``prepared`` through the operation journal. Collapsing
        # them to ``failed`` would lose whether backup or replacement should be
        # resumed and would strand the manifest because failed recovery runs
        # only under the verified target executable.
        "prepared": frozenset({"software_replaced", "rolling_back"}),
        "software_replaced": frozenset({"migrating", "failed", "rolling_back"}),
        "migrating": frozenset({"migrating", "clean", "failed", "rolling_back"}),
        "failed": frozenset({"migrating", "rolling_back"}),
        "rolling_back": frozenset({"rolling_back", "clean", "failed"}),
    }
    if migration_state not in allowed[current.migration_state]:
        raise InstallationError(
            f"invalid installation migration transition: "
            f"{current.migration_state} -> {migration_state}"
        )
    if current.operation_id is not None and operation_id not in {current.operation_id, None}:
        raise InstallationError("an active upgrade cannot change its operation_id")
    if (
        current.migration_state != "clean"
        and migration_state != "clean"
        and operation_id != current.operation_id
    ):
        raise InstallationError("an active upgrade must retain its operation_id")
    next_generation = current.data_generation if data_generation is None else data_generation
    generation_can_change = migration_state == "clean" and current.migration_state in {
        "migrating",
        "rolling_back",
    }
    if next_generation != current.data_generation and not generation_can_change:
        raise InstallationError("data generation can change only when a verified operation cleans")
    observed = _read_valid_installation(root)
    if observed != current:
        raise InstallationError("installation manifest changed while the operation was active")
    updated = current.model_copy(
        update={
            "migration_state": migration_state,
            "operation_id": operation_id,
            "data_generation": next_generation,
            "last_lifecycle_version": lifecycle_version,
        }
    )
    # ``model_copy`` intentionally skips validation; round-trip through the
    # strict boundary before a lifecycle gate is written durably.
    validated = InstallationManifest.model_validate(updated.model_dump(mode="python"))
    _write_installation_manifest(root, validated)
    return validated


async def managed_schedules_installed(backend: CrontabReader) -> bool:
    """Report whether a Ricky-managed crontab block exists on this host.

    The read itself is the availability signal. A crontab that cannot be read
    could never have installed the managed block, so an unreadable crontab
    means there is nothing managed to remove. Ricky state directories are not
    a proxy: they can be deleted or restored without the block, and an
    ambiguous marker state is a failure rather than an absence.
    """

    # Import lazily so configuration can depend on this bootstrap module
    # without creating an import cycle during ordinary settings loading.
    from ricky.schedules.cron import CronError, parse_managed_crontab

    try:
        current = await backend.read()
    except CronError:
        return False
    return parse_managed_crontab(current).block is not None


async def require_decommissioned(root: Path, *, crontab: CrontabReader | None = None) -> None:
    """Fail if a Ricky-owned launcher could outlive the data root's deletion."""

    # Import lazily so configuration can depend on this bootstrap module
    # without creating an import cycle during ordinary settings loading.
    from ricky.config import RickySettings
    from ricky.gateway.service_unit import GatewayServiceUnit

    settings = RickySettings(user_data_dir=str(root))
    unit = GatewayServiceUnit(settings)
    if unit.installed() is not None and unit.owned():
        raise InstallationError(
            "the Ricky gateway service is still installed; run `ricky decommission` first"
        )
    backend = crontab
    if backend is None:
        from ricky.schedules.cron import UserCrontabBackend

        backend = UserCrontabBackend(settings)
    if await managed_schedules_installed(backend):
        raise InstallationError(
            "Ricky-managed schedules are still installed; run `ricky decommission` first"
        )


async def purge_installation_data(
    *,
    expected_installation_id: str,
    crontab: CrontabReader | None = None,
) -> PurgeResult:
    """Irreversibly remove one exact inactive installation and its pointer."""

    pointer_path = bootstrap_file()
    with _initialization_lock(pointer_path.parent):
        pointer = read_bootstrap_pointer(pointer_path)
        if pointer is None:
            raise InstallationError("Ricky is not initialized")
        if expected_installation_id != pointer.installation_id:
            raise InstallationError("installation id confirmation does not match")
        root = Path(pointer.user_data_dir)
        _validate_target(root)
        manifest = _read_valid_installation(root)
        if manifest is None or manifest.installation_id != pointer.installation_id:
            raise InstallationError("bootstrap pointer does not match the Ricky installation")

        # Import lazily so configuration can depend on this bootstrap module
        # without creating an import cycle during ordinary settings loading.
        from ricky.config import RickySettings
        from ricky.gateway.lock import GatewayLock

        if GatewayLock(RickySettings(user_data_dir=str(root))).is_active():
            raise InstallationError("cannot purge while the Ricky gateway is active")
        # A Ricky-owned launch surface outlives the data it points at: systemd
        # restarts the removed root's gateway and the managed crontab block keeps
        # firing. Enforce that here, with the active-gateway invariant, so no
        # caller can delete the root through an unguarded path.
        await require_decommissioned(root, crontab=crontab)

        tombstone = root.with_name(f".{root.name}-purge-{pointer.installation_id[:8]}")
        if tombstone.exists():
            raise InstallationError(f"stale purge staging path requires manual review: {tombstone}")
        # Staging the root is the last reversible step. Once removal starts the
        # staged tree can be partially deleted, and a partially deleted tree is
        # never restored: it can still satisfy the scaffold check while arbitrary
        # user data is already gone. Keep it staged and name it instead.
        os.replace(root, tombstone)
        try:
            shutil.rmtree(tombstone)
        except OSError as exc:
            raise InstallationError(
                f"purge failed after it started removing data; the partially removed "
                f"installation is kept at {tombstone} and requires manual review"
            ) from exc
        pointer_path.unlink()
        fsync_directory(pointer_path.parent)
        return PurgeResult(
            user_data_dir=str(root),
            bootstrap_path=str(pointer_path),
            installation_id=pointer.installation_id,
        )


def _canonical_target(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise InstallationError("user_data_dir must be an absolute or home-relative path")
    for candidate in (raw, *raw.parents):
        # ``is_symlink`` never follows the link, so a dangling symbolic link is
        # refused instead of silently resolving to its unrelated target.
        if candidate.is_symlink():
            raise InstallationError("user_data_dir path cannot contain a symbolic link")
    return raw.resolve()


def _validate_target(root: Path) -> None:
    home = Path.home().resolve()
    if root == Path("/") or root == home:
        raise InstallationError("user_data_dir cannot be the filesystem root or home directory")
    if root.is_symlink():
        raise InstallationError("user_data_dir must be a real directory, not a link or file")
    if root.exists():
        if not root.is_dir():
            raise InstallationError("user_data_dir must be a real directory, not a link or file")
        if os.name == "posix" and root.stat().st_uid != os.getuid():
            raise InstallationError("user_data_dir must be owned by the current user")


def _read_valid_installation(root: Path) -> InstallationManifest | None:
    if not root.exists():
        return None
    if not any(root.iterdir()):
        return None
    manifest_path = root / INSTALLATION_FILENAME
    config_path = root / "ricky.toml"
    profiles_path = root / "profiles"
    shared_path = profiles_path / "shared"
    scaffold_paths = (manifest_path, config_path, profiles_path, shared_path)
    if any(path.is_symlink() for path in scaffold_paths):
        raise InstallationError("Ricky installation scaffold cannot contain symbolic links")
    if not manifest_path.is_file():
        raise InstallationError(
            f"refusing non-empty directory without {INSTALLATION_FILENAME}: {root}"
        )
    try:
        manifest = InstallationManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise InstallationError(f"invalid installation manifest: {manifest_path}") from exc
    missing = [path for path in (config_path, shared_path) if not path.exists()]
    if missing:
        rendered = ", ".join(str(path.relative_to(root)) for path in missing)
        raise InstallationError(f"partial Ricky installation is missing: {rendered}")
    if not config_path.is_file() or not profiles_path.is_dir() or not shared_path.is_dir():
        raise InstallationError("Ricky installation scaffold has invalid path types")
    return manifest


def _create_scaffold(root: Path, manifest: InstallationManifest) -> None:
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.exists() and any(root.iterdir()):
        raise InstallationError(f"refusing to initialize non-empty directory: {root}")

    stage = Path(tempfile.mkdtemp(prefix=f".{root.name}-init-", dir=root.parent))
    prior_empty: Path | None = None
    try:
        os.chmod(stage, 0o700)
        shared = stage / "profiles" / "shared"
        shared.mkdir(parents=True, mode=0o700)
        os.chmod(stage / "profiles", 0o700)
        os.chmod(shared, 0o700)
        write_private_file(stage / "ricky.toml", _initial_config())
        manifest_text = (
            json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        )
        write_private_file(stage / INSTALLATION_FILENAME, manifest_text)

        if root.exists():
            prior_empty = root.with_name(f".{root.name}-empty-{uuid4().hex[:8]}")
            os.replace(root, prior_empty)
        try:
            os.replace(stage, root)
        except BaseException:
            if prior_empty is not None and prior_empty.exists() and not root.exists():
                os.replace(prior_empty, root)
            raise
        # The replace committed the new root, so the staged prior directory can
        # never be restored. Remove it before the durability fsync so no later
        # failure can orphan it beside the data root.
        if prior_empty is not None:
            prior_empty.rmdir()
        fsync_directory(root.parent)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _initial_config() -> str:
    import tomlkit

    document = tomlkit.document()
    profiles = tomlkit.table()
    profiles.add("enabled", ["shared"])
    profiles.add("default", "shared")
    document.add("profiles", profiles)
    return tomlkit.dumps(document)


def _write_pointer(path: Path, pointer: BootstrapPointer) -> None:
    import tomlkit

    document = tomlkit.document()
    document.add("format_version", pointer.format_version)
    document.add("user_data_dir", pointer.user_data_dir)
    document.add("installation_id", pointer.installation_id)
    write_private_file(path, tomlkit.dumps(document))


def write_private_file(path: Path, content: str) -> None:
    """Commit one owner-only document with same-directory replace and fsync."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    posix = os.name == "posix"
    if posix:
        os.chmod(path.parent, 0o700)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(raw)
    try:
        # ``os.fdopen`` takes ownership of the descriptor, so tighten the mode
        # from inside the context manager and never leave a bare descriptor open
        # on a failure path.
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            if posix:
                os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if posix:
            os.chmod(path, 0o600)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def installation_operation_lock(
    directory: Path | None = None,
    *,
    mode: OperationLockMode,
    timeout_seconds: float = 5.0,
    operation: str,
    operation_id: str | None = None,
) -> Iterator[InstallationOperationLock]:
    """Acquire the host-local shared or exclusive lifecycle lock with a bounded wait."""

    if fcntl is None:  # pragma: no cover
        raise InstallationError("installation lifecycle requires a POSIX host")
    if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
        raise InstallationError("installation lock timeout must be finite and non-negative")
    if mode not in {"shared", "exclusive"}:
        raise InstallationError("installation lock mode must be shared or exclusive")
    if (
        not operation
        or len(operation) > 100
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in operation)
    ):
        raise InstallationError("installation operation name is invalid")
    directory = directory or bootstrap_config_dir()
    if directory.is_symlink():
        raise InstallationError("Ricky bootstrap directory cannot be a symbolic link")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix" and directory.stat().st_uid != os.getuid():
        raise InstallationError("Ricky bootstrap directory must be owned by the current user")
    os.chmod(directory, 0o700)
    path = directory / _INIT_LOCK_FILENAME
    info_path = directory / _OPERATION_INFO_FILENAME
    if path.is_symlink() or info_path.is_symlink():
        raise InstallationError("Ricky installation lock paths cannot be symbolic links")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    authority: InstallationOperationLock | None = None
    try:
        os.fchmod(descriptor, 0o600)
        flag = fcntl.LOCK_SH if mode == "shared" else fcntl.LOCK_EX
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(descriptor, flag | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    detail = _active_operation_detail(info_path)
                    raise InstallationError(
                        f"could not start {operation} within {timeout_seconds:g}s; {detail}"
                    ) from exc
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

        if mode == "exclusive":
            info = InstallationOperationInfo(
                operation=operation,
                operation_id=operation_id,
                pid=os.getpid(),
                started_at=datetime.now(UTC),
            )
            write_private_file(
                info_path,
                json.dumps(info.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            )
        authority = InstallationOperationLock(descriptor=descriptor, path=path, mode=mode)
        yield authority
    finally:
        transferred = acquired and authority is not None and authority.transferred
        if acquired and mode == "exclusive" and not transferred:
            info_path.unlink(missing_ok=True)
            fsync_directory(directory)
        if acquired and not transferred:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _active_operation_detail(path: Path) -> str:
    try:
        info = InstallationOperationInfo.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "another Ricky process is active"
    if not _pid_is_alive(info.pid):
        return "another Ricky process is active"
    operation_id = f" {info.operation_id}" if info.operation_id is not None else ""
    return f"Ricky operation {info.operation}{operation_id} is active (pid {info.pid})"


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def adopt_inherited_operation_lock(
    descriptor: int,
    *,
    directory: Path,
    operation_id: str,
) -> Iterator[InstallationOperationLock]:
    """Adopt the exact exclusive lock description inherited from an upgrade parent."""

    if fcntl is None:  # pragma: no cover
        raise InstallationError("installation lifecycle requires a POSIX host")
    lock_path = directory / _INIT_LOCK_FILENAME
    info_path = directory / _OPERATION_INFO_FILENAME
    try:
        descriptor_stat = os.fstat(descriptor)
        path_stat = lock_path.stat()
        info = InstallationOperationInfo.model_validate_json(info_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallationError("invalid inherited installation operation lock") from exc
    if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
        raise InstallationError("inherited descriptor does not identify Ricky's operation lock")
    if info.operation_id != operation_id:
        raise InstallationError("inherited operation lock does not match the upgrade operation")
    authority = InstallationOperationLock(
        descriptor=descriptor,
        path=lock_path,
        mode="exclusive",
    )
    try:
        yield authority
    finally:
        if not authority.transferred:
            info_path.unlink(missing_ok=True)
            fsync_directory(directory)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def _initialization_lock(directory: Path) -> Iterator[None]:
    """Compatibility wrapper for the initialization and purge exclusive boundary."""

    with installation_operation_lock(
        directory,
        mode="exclusive",
        timeout_seconds=0.0,
        operation="installation",
    ):
        yield


def fsync_directory(path: Path) -> None:
    """Flush one directory entry so a rename or a create survives a crash."""

    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
