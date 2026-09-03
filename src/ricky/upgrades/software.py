"""Exact uv-tool replacement, recovery artifacts, and executable handoff."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import httpx

from ricky.installation import InstallationOperationLock
from ricky.upgrades.environment import InstalledToolEnvironment
from ricky.upgrades.journal import UpgradeJournal, UpgradeSoftwareBinding
from ricky.upgrades.models import ReleaseArtifact, ReleaseDescriptor
from ricky.upgrades.releases import cache_release_artifacts
from ricky.upgrades.versions import ReleaseVersion

_UV_TIMEOUT_SECONDS = 600
_VERIFY_TIMEOUT_SECONDS = 20


class SoftwareReplacementError(RuntimeError):
    """The exact uv tool replacement or verification failed safely."""


class UpgradeHandoffComplete(BaseException):
    """The inherited-lock child completed the remainder of the operation."""

    def __init__(self, exit_code: int) -> None:
        super().__init__(f"upgrade handoff exited with status {exit_code}")
        self.exit_code = exit_code


async def create_software_binding(
    *,
    user_data_dir: Path,
    operation_id: str,
    source_release: ReleaseDescriptor,
    target_release: ReleaseDescriptor,
    client: httpx.AsyncClient | None = None,
) -> UpgradeSoftwareBinding:
    """Cache both exact release pairs before replacement can begin."""

    root = user_data_dir.expanduser().resolve()
    operation_root = root / "upgrades" / operation_id
    if target_release.software_version <= source_release.software_version:
        raise SoftwareReplacementError("upgrade target must be newer than source release")
    source_wheel, source_constraints = await cache_release_artifacts(
        source_release,
        operation_root / "artifacts" / "source",
        client=client,
    )
    target_wheel, target_constraints = await cache_release_artifacts(
        target_release,
        operation_root / "artifacts" / "target",
        client=client,
    )
    return UpgradeSoftwareBinding(
        source_release=source_release,
        target_release=target_release,
        source_wheel_path=str(source_wheel),
        source_constraints_path=str(source_constraints),
        target_wheel_path=str(target_wheel),
        target_constraints_path=str(target_constraints),
    )


def discover_uv_version(executable: Path | None = None) -> ReleaseVersion:
    """Return the exact release-form uv version or fail with bounded guidance."""

    selected = executable or _uv_executable()
    try:
        process = subprocess.run(
            [str(selected), "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=_VERIFY_TIMEOUT_SECONDS,
            env=_sanitized_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SoftwareReplacementError("uv is unavailable or did not respond") from exc
    fields = process.stdout.decode("utf-8", errors="replace").strip().split()
    if process.returncode != 0 or len(fields) < 2 or fields[0] != "uv":
        raise SoftwareReplacementError("uv version could not be verified")
    try:
        return ReleaseVersion.parse(fields[1])
    except ValueError as exc:
        raise SoftwareReplacementError("uv reported an unsupported version identity") from exc


class UvToolSoftwareController:
    """Replace only the discovered Ricky uv tool and hand authority to its new code."""

    def __init__(
        self,
        *,
        environment: InstalledToolEnvironment,
        lock: InstallationOperationLock,
        uv_executable: Path | None = None,
        as_json: bool = False,
    ) -> None:
        self._environment = environment
        self._lock = lock
        self._uv = (uv_executable or _uv_executable()).resolve(strict=True)
        self._as_json = as_json

    def inspect_version(self) -> ReleaseVersion:
        """Ask the canonical installed entry point for its current exact version."""

        try:
            process = subprocess.run(
                [self._environment.executable, "--version"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=_VERIFY_TIMEOUT_SECONDS,
                env=self._environment_variables(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SoftwareReplacementError(
                "installed Ricky executable could not be verified"
            ) from exc
        fields = process.stdout.decode("utf-8", errors="replace").strip().split()
        if process.returncode != 0 or len(fields) != 2 or fields[0] != "ricky":
            raise SoftwareReplacementError(
                "installed Ricky executable reported an invalid identity"
            )
        try:
            return ReleaseVersion.parse(fields[1])
        except ValueError as exc:
            raise SoftwareReplacementError(
                "installed Ricky version is not a release version"
            ) from exc

    def install_target(self, journal: UpgradeJournal) -> None:
        software = self._software(journal)
        self._install(
            wheel=Path(software.target_wheel_path),
            constraints=Path(software.target_constraints_path),
            wheel_artifact=software.target_release.wheel,
            constraints_artifact=software.target_release.constraints,
            expected=journal.target_software_version,
            minimum_uv=software.target_release.minimum_uv_version,
        )
        self._handoff(journal, action="resume")

    def install_source(self, journal: UpgradeJournal) -> None:
        software = self._software(journal)
        self._install(
            wheel=Path(software.source_wheel_path),
            constraints=Path(software.source_constraints_path),
            wheel_artifact=software.source_release.wheel,
            constraints_artifact=software.source_release.constraints,
            expected=journal.source_software_version,
            minimum_uv=software.source_release.minimum_uv_version,
        )
        self._handoff(journal, action="rollback")

    def _install(
        self,
        *,
        wheel: Path,
        constraints: Path,
        wheel_artifact: ReleaseArtifact,
        constraints_artifact: ReleaseArtifact,
        expected: ReleaseVersion,
        minimum_uv: ReleaseVersion,
    ) -> None:
        _verify_artifact(wheel, wheel_artifact)
        _verify_artifact(constraints, constraints_artifact)
        current_uv = discover_uv_version(self._uv)
        if current_uv < minimum_uv:
            raise SoftwareReplacementError(
                f"release requires uv {minimum_uv} or newer; found {current_uv}"
            )
        python = Path(self._environment.environment) / "bin" / "python"
        if not python.is_file():
            raise SoftwareReplacementError("uv tool Python executable is missing")
        command = [
            str(self._uv),
            "tool",
            "install",
            "--force",
            "--python",
            str(python),
            "--constraints",
            str(constraints),
            "--no-config",
            "--no-progress",
            str(wheel),
        ]
        try:
            process = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=_UV_TIMEOUT_SECONDS,
                env=self._environment_variables(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SoftwareReplacementError("uv tool replacement failed or timed out") from exc
        if process.returncode != 0:
            raise SoftwareReplacementError(
                "uv tool replacement failed; cached artifacts were preserved"
            )
        if self.inspect_version() != expected:
            raise SoftwareReplacementError(
                "uv installed Ricky does not match the exact target release"
            )

    def _handoff(self, journal: UpgradeJournal, *, action: str) -> None:
        self._lock.require_owned_exclusive()
        arguments = [
            self._environment.executable,
            "_upgrade-handoff",
            "--operation-id",
            journal.operation_id,
            "--action",
            action,
            "--lock-fd",
            str(self._lock.descriptor),
        ]
        if self._as_json:
            arguments.append("--json")
        try:
            child = subprocess.Popen(
                arguments,
                stdin=subprocess.DEVNULL,
                env=self._environment_variables(),
                pass_fds=(self._lock.descriptor,),
            )
        except OSError as exc:
            raise SoftwareReplacementError("target Ricky process could not be started") from exc
        self._lock.transfer_to_child()
        try:
            exit_code = child.wait(timeout=_UV_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            child.kill()
            exit_code = child.wait()
        raise UpgradeHandoffComplete(exit_code)

    def _software(self, journal: UpgradeJournal) -> UpgradeSoftwareBinding:
        if journal.software is None:
            raise SoftwareReplacementError("upgrade journal has no exact release artifact binding")
        return journal.software

    def _environment_variables(self) -> dict[str, str]:
        environment = _sanitized_environment()
        environment["UV_TOOL_DIR"] = self._environment.tool_root
        environment["UV_TOOL_BIN_DIR"] = self._environment.bin
        return environment


def _uv_executable() -> Path:
    found = shutil.which("uv")
    if found is None:
        raise SoftwareReplacementError("uv is required for a released Ricky upgrade")
    try:
        selected = Path(found).resolve(strict=True)
    except OSError as exc:
        raise SoftwareReplacementError("uv executable is invalid") from exc
    if not selected.is_file() or not os.access(selected, os.X_OK):
        raise SoftwareReplacementError("uv executable is invalid")
    return selected


def _verify_artifact(path: Path, artifact: ReleaseArtifact) -> None:
    if (
        path.name != artifact.name
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_size != artifact.size
    ):
        raise SoftwareReplacementError("cached release artifact identity changed")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != artifact.sha256:
        raise SoftwareReplacementError("cached release artifact checksum changed")


def _sanitized_environment() -> dict[str, str]:
    allowed = {
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_BIN_HOME",
        "UV_CACHE_DIR",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}
