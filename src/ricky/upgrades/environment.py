"""Read-only detection of Ricky's owning uv tool environment."""

from __future__ import annotations

import os
import shutil
import sys
from importlib import metadata
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from ricky.upgrades.versions import ReleaseVersion, require_installed_release_version

_APPLY_REQUIRES_UV_TOOL = (
    "upgrade apply requires a uv-tool-installed Ricky release; "
    "read-only upgrade check remains available"
)


class UpgradeEnvironmentError(RuntimeError):
    """The running process is not the released uv tool Ricky installation."""


class InstalledToolEnvironment(BaseModel):
    """Canonical identity of the uv tool installation running this process."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    tool_root: str
    environment: str
    bin: str
    executable: str
    package_root: str
    current_version: ReleaseVersion

    @field_validator("tool_root", "environment", "bin", "executable", "package_root")
    @classmethod
    def _canonical_absolute_path(cls, value: str) -> str:
        if not value or len(value) > 4_096:
            raise ValueError("tool environment path must be nonempty and bounded")
        path = Path(value)
        if not path.is_absolute() or path != path.resolve():
            raise ValueError("tool environment path must be absolute and canonical")
        return value

    @field_validator("current_version")
    @classmethod
    def _released_version(cls, value: ReleaseVersion) -> ReleaseVersion:
        return require_installed_release_version(value)

    @classmethod
    def discover(cls) -> InstalledToolEnvironment:
        """Discover the current process through the module-level facade."""

        return discover_installed_tool_environment()


def discover_installed_tool_environment() -> InstalledToolEnvironment:
    """Prove that the current process is Ricky's canonical uv tool executable.

    Detection performs metadata and filesystem reads only. It never asks uv for
    paths, because invoking uv would make development-checkout detection depend
    on mutable external command behavior.
    """

    try:
        distribution = metadata.distribution("ricky")
    except metadata.PackageNotFoundError as exc:
        raise UpgradeEnvironmentError(_APPLY_REQUIRES_UV_TOOL) from exc

    try:
        current_version = require_installed_release_version(distribution.version)
        tool_root = _tool_root()
        bin_root = _tool_bin()
        environment = (tool_root / "ricky").resolve(strict=True)
        running_prefix = Path(sys.prefix).expanduser().resolve(strict=True)
        expected_entry = (bin_root / "ricky").resolve(strict=True)
        invoked_entry = _invoked_executable()
        package_root = Path(str(distribution.locate_file("ricky"))).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise UpgradeEnvironmentError(_APPLY_REQUIRES_UV_TOOL) from exc

    if running_prefix != environment or invoked_entry != expected_entry:
        raise UpgradeEnvironmentError(_APPLY_REQUIRES_UV_TOOL)
    if not expected_entry.is_file() or not os.access(expected_entry, os.X_OK):
        raise UpgradeEnvironmentError(_APPLY_REQUIRES_UV_TOOL)
    if not package_root.is_dir() or not _is_within(package_root, environment):
        raise UpgradeEnvironmentError(_APPLY_REQUIRES_UV_TOOL)
    if not _is_within(expected_entry, environment):
        raise UpgradeEnvironmentError(_APPLY_REQUIRES_UV_TOOL)

    return InstalledToolEnvironment(
        tool_root=str(tool_root),
        environment=str(environment),
        bin=str(bin_root),
        executable=str(expected_entry),
        package_root=str(package_root),
        current_version=current_version,
    )


def _tool_root() -> Path:
    configured = os.environ.get("UV_TOOL_DIR")
    if configured is not None:
        return _configured_absolute_path(configured)
    data_home = os.environ.get("XDG_DATA_HOME")
    base = (
        _configured_absolute_path(data_home)
        if data_home is not None
        else Path.home().resolve() / ".local" / "share"
    )
    return (base / "uv" / "tools").resolve()


def _tool_bin() -> Path:
    configured = os.environ.get("UV_TOOL_BIN_DIR")
    if configured is not None:
        return _configured_absolute_path(configured)
    xdg_bin_home = os.environ.get("XDG_BIN_HOME")
    if xdg_bin_home is not None:
        return _configured_absolute_path(xdg_bin_home)
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home is not None:
        return (_configured_absolute_path(data_home).parent / "bin").resolve()
    return (Path.home().resolve() / ".local" / "bin").resolve()


def _configured_absolute_path(value: str) -> Path:
    selected = Path(value).expanduser()
    if not selected.is_absolute():
        raise ValueError("uv tool paths must be absolute")
    return selected.resolve()


def _invoked_executable() -> Path:
    raw = sys.argv[0]
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        found = shutil.which(raw)
        if found is None:
            raise ValueError("running Ricky executable is not resolvable")
        candidate = Path(found)
    return candidate.resolve(strict=True)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
