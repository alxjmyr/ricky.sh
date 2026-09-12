"""Ordinary local Chrome setup without an automation connection."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from ricky.browser.chrome import chrome_environment, require_chrome
from ricky.browser.lease import BrowserResourceLease
from ricky.browser.resources import prepare_persistent_browser, require_browser_resource
from ricky.browser.types import BrowserError, BrowserFailure
from ricky.config import PersistentBrowserResourceSettings, RickySettings
from ricky.profiles import ProfileResourceRef, ProfileScope


@asynccontextmanager
async def manual_browser_setup(
    settings: RickySettings, *, scope: ProfileScope, resource: ProfileResourceRef
) -> AsyncIterator[asyncio.subprocess.Process]:
    """Lease one dedicated profile until its ordinary Chrome process is closed."""

    resolved = require_browser_resource(settings, scope=scope, ref=resource)
    if resolved is None:
        raise BrowserError(
            BrowserFailure(code="unknown_resource", message="unknown browser resource")
        )
    if not isinstance(resolved.settings, PersistentBrowserResourceSettings):
        raise BrowserError(
            BrowserFailure(
                code="resource_kind_mismatch",
                message="browser setup is only available for persistent browser resources",
            )
        )
    executable = await require_chrome(settings)
    environment = chrome_environment()
    if not (environment.get("DISPLAY") or environment.get("WAYLAND_DISPLAY")):
        raise BrowserError(
            BrowserFailure(
                code="backend_error", message="Chrome setup requires a local graphical display"
            )
        )
    lease = BrowserResourceLease(settings, resource)
    lease.acquire()
    process: asyncio.subprocess.Process | None = None
    try:
        state = prepare_persistent_browser(settings, resource)
        # No debugging port/pipe, automation flags, model, or network interception.
        launch = asyncio.create_task(
            asyncio.create_subprocess_exec(
                str(executable),
                f"--user-data-dir={state}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-mode",
                "--new-window",
                "about:blank",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
                start_new_session=True,
            )
        )
        interrupted = False
        while not launch.done():
            try:
                await asyncio.shield(launch)
            except asyncio.CancelledError:
                # Repeated cancellation cannot relinquish a late-created process.
                interrupted = True
        process = launch.result()
        if interrupted:
            raise asyncio.CancelledError
        assert process is not None
        yield process
        # A successful setup finishes through Chrome's own window-close path.
        # Terminating it here can discard recently issued login cookies.
        if await process.wait() != 0:
            raise BrowserError(
                BrowserFailure(
                    code="backend_error",
                    message="Chrome setup exited unsuccessfully; sign-in state may not be saved",
                )
            )
    finally:
        if process is None:
            lease.release()
        else:
            cleanup = asyncio.create_task(_close_setup(process, lease))
            interrupted = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    interrupted = True
            cleanup.result()
            if interrupted:
                raise asyncio.CancelledError


async def _close_setup(process: asyncio.subprocess.Process, lease: BrowserResourceLease) -> None:
    # Let Chrome coordinate shutdown and flush its profile before terminating
    # helpers. Signalling the whole group first can kill the cookie-store owner.
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
    try:
        async with asyncio.timeout(5):
            await process.wait()
    except TimeoutError:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    try:
        async with asyncio.timeout(5):
            while _group_running(process.pid):
                await asyncio.sleep(0.02)
    except TimeoutError as exc:
        raise BrowserError(
            BrowserFailure(
                code="backend_error",
                message=(
                    "Chrome setup cleanup could not be confirmed; browser resource remains busy"
                ),
            )
        ) from exc
    lease.release()


def _group_running(group: int) -> bool:
    """Check Linux process ownership without reading arguments or environment."""

    proc = Path("/proc")
    if not proc.is_dir():
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return False
        return True
    for path in proc.iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        # The stat suffix begins with state, parent pid, and process-group id.
        if int(fields[2]) == group and fields[0] not in {"Z", "X"}:
            return True
    return False
