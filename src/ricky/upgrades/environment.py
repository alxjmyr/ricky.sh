"""Read-only detection of Ricky's owning uv tool environment."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from ricky.upgrades.versions import ReleaseVersion, require_installed_release_version

_APPLY_REQUIRES_UV_TOOL = (
    "upgrade apply requires a uv-tool-installed Ricky release; "
    "read-only upgrade check remains available"
)
_BOOTSTRAP_INVALID = (
    "upgrade bootstrap requires an isolated, non-editable released Ricky wheel and "
    "the exact installed uv tool executable"
)
_SOURCE_PROBE = """
import json
import sys
from importlib import metadata
from pathlib import Path
d = metadata.distribution('ricky')
direct = json.loads(d.read_text('direct_url.json') or '{}')
assert not direct.get('dir_info', {}).get('editable', False)
assert d.read_text('WHEEL') is not None
print(json.dumps({'version': d.version, 'prefix': str(Path(sys.prefix).resolve()),
                  'package_root': str(Path(d.locate_file('ricky')).resolve())}))
"""


class _SourceProbe(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: str
    prefix: str
    package_root: str


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


def discover_bootstrap_tool_environment(installed_executable: Path) -> InstalledToolEnvironment:
    """Bind an isolated released coordinator to one explicitly selected old tool.

    The source interpreter reads distribution metadata only; it never imports
    Ricky or opens application data. Its bounded probe is killed and awaited by
    subprocess.run if the timeout expires.
    """

    try:
        distribution = metadata.distribution("ricky")
        require_installed_release_version(distribution.version)
        direct = json.loads(distribution.read_text("direct_url.json") or "{}")
        if direct.get("dir_info", {}).get("editable", False):
            raise ValueError("editable bootstrap")
        if distribution.read_text("WHEEL") is None:
            raise ValueError("bootstrap is not a wheel")
        prefix = Path(sys.prefix).resolve(strict=True)
        package = Path(str(distribution.locate_file("ricky"))).resolve(strict=True)
        entry = _invoked_executable()
        if (
            not package.is_dir()
            or not _is_within(package, prefix)
            or not _is_within(entry, prefix)
            or entry != (prefix / "bin" / "ricky").resolve(strict=True)
            or not entry.is_file()
            or not os.access(entry, os.X_OK)
        ):
            raise ValueError("invalid bootstrap identity")
        tool_root = _tool_root()
        bin_root = _tool_bin()
        environment = (tool_root / "ricky").resolve(strict=True)
        if prefix == environment:
            raise ValueError("bootstrap must be isolated")
        supplied = installed_executable.expanduser()
        if not supplied.is_absolute():
            raise ValueError("source executable must be absolute")
        executable = supplied.resolve(strict=True)
        if (
            executable != (environment / "bin" / "ricky").resolve(strict=True)
            or executable != (bin_root / "ricky").resolve(strict=True)
            or not _is_within(executable, environment)
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
        ):
            raise ValueError("invalid source executable")
        python = environment / "bin" / "python"
        if not python.is_file() or not os.access(python, os.X_OK):
            raise ValueError("source Python is unavailable")
        process = subprocess.run(
            [str(python), "-I", "-B", "-c", _SOURCE_PROBE],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=20,
            check=False,
            env={key: value for key, value in os.environ.items() if not key.startswith("PYTHON")},
        )
        if process.returncode != 0 or len(process.stdout) > 16_384:
            raise ValueError("source metadata probe failed")
        probe = _SourceProbe.model_validate_json(process.stdout)
        source_package = Path(probe.package_root)
        if (
            probe.prefix != str(environment)
            or source_package != source_package.resolve(strict=True)
            or not source_package.is_dir()
            or not _is_within(source_package, environment)
        ):
            raise ValueError("source metadata identity mismatch")
        return InstalledToolEnvironment(
            tool_root=str(tool_root),
            environment=str(environment),
            bin=str(bin_root),
            executable=str(executable),
            package_root=str(source_package),
            current_version=require_installed_release_version(probe.version),
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
        AttributeError,
        metadata.PackageNotFoundError,
        subprocess.TimeoutExpired,
    ) as exc:
        raise UpgradeEnvironmentError(_BOOTSTRAP_INVALID) from exc


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
