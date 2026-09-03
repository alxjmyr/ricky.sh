"""Explicit Playwright Chromium installation and readiness inspection."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from ricky.browser.types import BrowserInstallStatus
from ricky.config import RickySettings, ensure_private_user_data_root, user_data_subpath

_BROWSERS_PATH_ENV = "PLAYWRIGHT_BROWSERS_PATH"
_COMMAND_OUTPUT_LIMIT = 8_000


class BrowserInstallationError(RuntimeError):
    """A bounded, secret-safe browser installation or inspection failure."""


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def browser_binary_dir(settings: RickySettings) -> Path:
    """Resolve the installation-owned browser binary directory."""

    return user_data_subpath(settings, settings.browser.binary_dir)


async def browser_status(settings: RickySettings) -> BrowserInstallStatus:
    """Inspect the locked Chromium executable without launching a browser."""

    install_dir = browser_binary_dir(settings)
    executable = await _probe_executable(install_dir)
    return BrowserInstallStatus(
        enabled=settings.browser.enabled,
        ready=executable.is_file() and _is_executable(executable),
        install_dir=str(install_dir),
        executable=str(executable),
    )


async def install_chromium(settings: RickySettings) -> BrowserInstallStatus:
    """Run the version-matched Playwright installer only on explicit request."""

    ensure_private_user_data_root(settings)
    install_dir = browser_binary_dir(settings)
    install_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(install_dir, 0o700)
    result = await _run_command(
        (sys.executable, "-m", "playwright", "install", "chromium"),
        env=_child_environment(install_dir),
    )
    if result.returncode != 0:
        raise BrowserInstallationError(
            f"Playwright Chromium installation failed with exit code {result.returncode}; "
            "run `uv run ricky browser install` again after checking network access"
        )
    status = await browser_status(settings)
    if not status.ready:
        raise BrowserInstallationError(
            "Playwright reported a successful Chromium installation, but the locked "
            "executable is not ready"
        )
    return status


async def _probe_executable(install_dir: Path) -> Path:
    result = await _run_command(
        (sys.executable, "-m", "ricky.browser._probe"),
        env=_child_environment(install_dir),
    )
    if result.returncode != 0:
        raise BrowserInstallationError(
            "Chromium readiness could not be inspected for the installed Playwright version"
        )
    output = result.stdout.decode("utf-8", errors="replace").strip()
    if not output or len(output) > 4_000 or len(output.splitlines()) != 1:
        raise BrowserInstallationError("Chromium readiness returned an invalid executable path")
    executable = Path(output).expanduser().resolve()
    root = install_dir.resolve()
    if not executable.is_relative_to(root):
        raise BrowserInstallationError(
            "Playwright resolved Chromium outside Ricky's configured browser binary directory"
        )
    return executable


def _child_environment(install_dir: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("DEBUGP", None)
    environment[_BROWSERS_PATH_ENV] = str(install_dir)
    return environment


async def _run_command(args: Sequence[str], *, env: Mapping[str, str]) -> _CommandResult:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(env),
        )
    except OSError as exc:
        raise BrowserInstallationError("Playwright could not be started") from exc
    try:
        stdout, stderr = await process.communicate()
    except BaseException:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
        await process.communicate()
        raise
    return _CommandResult(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=stdout[-_COMMAND_OUTPUT_LIMIT:],
        stderr=stderr[-_COMMAND_OUTPUT_LIMIT:],
    )


def _is_executable(path: Path) -> bool:
    return os.name != "posix" or os.access(path, os.X_OK)
