"""Discovery of host-managed Google Chrome Stable, without browser downloads."""

from __future__ import annotations

import asyncio
import os
import re
import signal
from contextlib import suppress
from importlib.metadata import version
from pathlib import Path

from ricky.browser.types import BrowserStatus
from ricky.config import RickySettings

_CHROME_LOCATIONS = (
    Path("/usr/bin/google-chrome-stable"),
    Path("/usr/bin/google-chrome"),
    Path("/opt/google/chrome/google-chrome"),
)
_PROBE_TIMEOUT_SECONDS = 5.0
_OUTPUT_LIMIT = 256
_VERSION = re.compile(rb"Google Chrome ([0-9]+(?:\.[0-9]+){3})")
_MISSING = (
    "Google Chrome Stable is unavailable; install it through your host package manager "
    "or set browser.executable_path to its absolute executable path"
)


class ChromeDiscoveryError(RuntimeError):
    """A bounded Chrome readiness error that never includes child diagnostics."""


def chrome_environment() -> dict[str, str]:
    """Copy the host environment, removing browser-control debug overrides."""

    environment = os.environ.copy()
    for key in ("DEBUG", "DEBUGP", "PWDEBUG", "PLAYWRIGHT_BROWSERS_PATH"):
        environment.pop(key, None)
    return environment


async def browser_status(settings: RickySettings) -> BrowserStatus:
    """Check executable identity only; launching and display readiness are separate."""

    candidates = (
        (settings.browser.executable_path,)
        if settings.browser.executable_path is not None
        else _CHROME_LOCATIONS
    )
    executable = next(
        (path for path in candidates if path.is_file() and os.access(path, os.X_OK)), None
    )
    if executable is None:
        return BrowserStatus(
            enabled=settings.browser.enabled,
            playwright_version=version("playwright"),
            ready=False,
            diagnostic=_MISSING,
        )
    try:
        chrome_version = await _probe_version(executable)
    except ChromeDiscoveryError as exc:
        return BrowserStatus(
            enabled=settings.browser.enabled,
            playwright_version=version("playwright"),
            ready=False,
            executable=str(executable),
            diagnostic=str(exc),
        )
    return BrowserStatus(
        enabled=settings.browser.enabled,
        playwright_version=version("playwright"),
        ready=True,
        executable=str(executable),
        version=chrome_version,
    )


async def require_chrome(settings: RickySettings) -> Path:
    status = await browser_status(settings)
    if not status.ready or status.executable is None:
        raise ChromeDiscoveryError(status.diagnostic or _MISSING)
    return Path(status.executable)


async def _probe_version(executable: Path) -> str:
    startup = asyncio.create_task(
        asyncio.create_subprocess_exec(
            str(executable),
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=chrome_environment(),
            start_new_session=True,
        )
    )
    try:
        process = await asyncio.shield(startup)
    except asyncio.CancelledError:
        while not startup.done():
            try:
                await asyncio.shield(startup)
            except asyncio.CancelledError:
                pass
            except OSError:
                break
        try:
            process = startup.result()
        except OSError:
            pass
        else:
            await _stop_probe(process)
        raise
    except OSError as exc:
        raise ChromeDiscoveryError("Google Chrome could not be executed") from exc
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
            assert process.stdout is not None
            try:
                output = await process.stdout.readexactly(_OUTPUT_LIMIT + 1)
            except asyncio.IncompleteReadError as exc:
                output = exc.partial
            if len(output) > _OUTPUT_LIMIT:
                raise ChromeDiscoveryError("Google Chrome returned an invalid version response")
            await process.wait()
        match = _VERSION.fullmatch(output.strip())
        if process.returncode != 0 or match is None:
            raise ChromeDiscoveryError(
                "Executable is not supported Google Chrome Stable or its version check failed"
            )
        return match.group(1).decode("ascii")
    except TimeoutError as exc:
        raise ChromeDiscoveryError("Google Chrome version check timed out") from exc
    finally:
        await _stop_probe(process)


async def _stop_probe(process: asyncio.subprocess.Process) -> None:
    cleanup = asyncio.create_task(_reap_probe(process))
    interrupted = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            interrupted = True
    cleanup.result()
    if interrupted:
        raise asyncio.CancelledError


async def _reap_probe(process: asyncio.subprocess.Process) -> None:
    # Kill the owned process group as wrappers can leave children holding stdout.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    if process.stdout is not None:
        while await process.stdout.read(65_536):
            pass
    await process.wait()
