"""Managed ``systemd --user`` unit for the gateway.

Ricky renders one deterministic, marked unit from typed configuration. It never
writes model-authored unit content, never places a secret on a command line, and
never overwrites a unit it does not own.

Every ``systemctl`` invocation goes through an injected command runner, so
automated tests exercise the real command construction against a fake process
table and never touch a live service manager.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ricky.config import RickySettings, find_project_root, user_data_subpath
from ricky.gateway.vault_bootstrap import STARTUP_UNLOCK_FAILURE_EXIT_CODE

MARKER = "# Managed by Ricky. Do not edit; run `ricky gateway service install`."
"""Ricky only ever replaces or removes a unit whose first line is this marker."""


class ServiceUnitError(RuntimeError):
    """A service unit could not be rendered, installed, or controlled."""


@dataclass(frozen=True)
class CommandResult:
    """One completed supervisor command."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[Sequence[str]], CommandResult]


def subprocess_runner(args: Sequence[str]) -> CommandResult:
    """Run one supervisor command with no shell and a bounded timeout."""

    completed = subprocess.run(  # noqa: S603 - argv is built from typed config only
        list(args),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return CommandResult(
        args=tuple(args),
        returncode=completed.returncode,
        stdout=completed.stdout[:8_000],
        stderr=completed.stderr[:8_000],
    )


class InstallResult(BaseModel):
    """Outcome of writing one managed unit to disk."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    unit_path: str = Field(min_length=1, max_length=2_000)
    backup_path: str | None = Field(default=None, max_length=2_000)
    created: bool
    verified: bool
    """True when the bytes read back from disk match the rendered unit exactly."""


class GatewayServiceUnit:
    """Render, install, and control one marked user service."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        project_root: Path | None = None,
        unit_dir: Path | None = None,
        runner: CommandRunner | None = None,
        executable: str | None = None,
    ) -> None:
        self.settings = settings
        self.config = settings.gateway.service
        self.project_root = (
            find_project_root() if project_root is None else project_root.expanduser().resolve()
        )
        if self.config.unit_dir is None:  # pragma: no cover - config resolves the default
            raise ServiceUnitError("gateway.service.unit_dir was not resolved by ricky.config")
        self.unit_dir = unit_dir or Path(self.config.unit_dir)
        self.unit_path = self.unit_dir / self.config.unit_name
        self.log_dir = user_data_subpath(settings, self.config.log_dir)
        self.log_path = self.log_dir / f"{Path(self.config.unit_name).stem}.log"
        self._runner = runner or subprocess_runner
        self._executable = executable

    def executable(self) -> str:
        """Resolve the absolute Ricky console script used by the generated unit."""

        if self._executable is not None:
            return self._executable
        found = shutil.which("ricky")
        if found is None:
            raise ServiceUnitError(
                "Ricky was not found on PATH; a user service needs its absolute console script"
            )
        return str(Path(found).resolve())

    def render(self) -> str:
        """Build the exact unit text. Deterministic for one configuration."""

        executable = shlex.quote(self.executable())
        root = shlex.quote(str(self.project_root))
        restart = _seconds(self.config.restart_seconds)
        start = _seconds(self.config.start_timeout_seconds)
        stop = _seconds(self.config.stop_timeout_seconds)
        log = shlex.quote(str(self.log_path))
        return "\n".join(
            (
                MARKER,
                "[Unit]",
                f"Description={self.config.description}",
                "After=network-online.target",
                "Wants=network-online.target",
                "",
                "[Service]",
                "Type=simple",
                f"WorkingDirectory={root}",
                f"ExecStart={executable} gateway run",
                "Restart=always",
                f"RestartPreventExitStatus={STARTUP_UNLOCK_FAILURE_EXIT_CODE}",
                f"RestartSec={restart}",
                f"TimeoutStartSec={start}",
                f"TimeoutStopSec={stop}",
                "KillSignal=SIGTERM",
                "SyslogIdentifier=ricky-gateway",
                f"StandardOutput=append:{log}",
                f"StandardError=append:{log}",
                "UMask=0077",
                "NoNewPrivileges=true",
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            )
        )

    def installed(self) -> str | None:
        """Read the installed unit text, or None when nothing is installed."""

        try:
            return self.unit_path.read_text(encoding="utf-8")
        except OSError:
            return None

    def owned(self) -> bool:
        """Report whether the installed unit carries the Ricky marker."""

        current = self.installed()
        return current is not None and current.startswith(MARKER)

    def drift(self) -> str | None:
        """Describe any difference between the installed unit and this configuration."""

        current = self.installed()
        if current is None:
            return None
        if not current.startswith(MARKER):
            return f"{self.unit_path} exists but is not managed by Ricky"
        try:
            expected = self.render()
        except ServiceUnitError as exc:
            return str(exc)
        if current != expected:
            return f"{self.unit_path} does not match the current configuration"
        return None

    def install(self) -> InstallResult:
        """Write the managed unit, backing up any prior Ricky-owned version."""

        current = self.installed()
        if current is not None and not current.startswith(MARKER):
            raise ServiceUnitError(
                f"{self.unit_path} was not written by Ricky; move it aside before installing"
            )
        content = self.render()
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(self.log_dir, 0o700)
        backup: Path | None = None
        if current is not None and current != content:
            backup = self.unit_path.with_suffix(self.unit_path.suffix + ".bak")
            backup.write_text(current, encoding="utf-8")
            os.chmod(backup, 0o600)
        descriptor = os.open(self.unit_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(self.unit_path, 0o600)
        return InstallResult(
            unit_path=str(self.unit_path),
            backup_path=None if backup is None else str(backup),
            created=current is None,
            verified=self.unit_path.read_text(encoding="utf-8") == content,
        )

    def uninstall(self) -> bool:
        """Remove only a Ricky-owned unit. Returns False when nothing was removed."""

        current = self.installed()
        if current is None:
            return False
        if not current.startswith(MARKER):
            raise ServiceUnitError(
                f"{self.unit_path} was not written by Ricky and will not be removed"
            )
        self.unit_path.unlink()
        return True

    def daemon_reload(self) -> CommandResult:
        """Ask the user manager to reread unit files."""

        return self._systemctl("daemon-reload")

    def start(self) -> CommandResult:
        return self._systemctl("start", self.config.unit_name)

    def stop(self) -> CommandResult:
        return self._systemctl("stop", self.config.unit_name)

    def restart(self) -> CommandResult:
        return self._systemctl("restart", self.config.unit_name)

    def enable(self) -> CommandResult:
        return self._systemctl("enable", self.config.unit_name)

    def disable(self) -> CommandResult:
        return self._systemctl("disable", self.config.unit_name)

    def status(self) -> CommandResult:
        return self._systemctl("is-active", self.config.unit_name)

    def enabled(self) -> CommandResult:
        return self._systemctl("is-enabled", self.config.unit_name)

    def _systemctl(self, *args: str) -> CommandResult:
        return self._runner(("systemctl", "--user", *args))


def _seconds(value: float) -> str:
    """Render a systemd duration without a float suffix surprise."""

    return f"{int(round(value))}s"
