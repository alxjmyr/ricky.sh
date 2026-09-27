"""Real pipe ownership checks for shell cancellation and desktop handoff."""

from __future__ import annotations

import asyncio
import shlex
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import pytest

from ricky.agent.session import AgentSession
from ricky.config import RickySettings
from ricky.tools.base import ToolContext
from ricky.tools.builtin.shell import RunShellParams, RunShellTool
from ricky.tools.registry import ToolRegistry
from ricky.workflows.inspection import inspect_workflow
from ricky.workflows.visualization import export_visualization
from workflow_visualization_support import write_design


@asynccontextmanager
async def controlled_child() -> AsyncIterator[
    tuple[str, asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]
]:
    """A child signals readiness then stays alive until explicitly released."""
    connected = asyncio.get_running_loop().create_future()

    def accept(reader, writer):
        connected.set_result((reader, writer))

    with tempfile.TemporaryDirectory(prefix="ricky-pipes-") as directory:
        socket_path = str(Path(directory) / "control.sock")
        server = await asyncio.start_unix_server(accept, path=socket_path)
        child = (
            "import socket\n"
            "connection = socket.socket(socket.AF_UNIX)\n"
            f"connection.connect({socket_path!r})\n"
            "connection.sendall(b'ready')\n"
            "connection.recv(1)\n"
            "connection.close()\n"
        )
        try:
            yield child, connected
        finally:
            if connected.done() and not connected.cancelled():
                reader, writer = connected.result()
                with suppress(ConnectionError):
                    writer.write(b"x")
                    await writer.drain()
                    async with asyncio.timeout(5):
                        await reader.read()
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()
            server.close()
            await server.wait_closed()


def context(tmp_path: Path) -> ToolContext:
    settings = RickySettings()
    return ToolContext(
        cwd=tmp_path,
        settings=settings,
        session=AgentSession.create(settings, profile_scope=settings.resolve_profile_scope()),
    )


async def test_open_view_returns_through_shell_while_desktop_browser_stays_alive(
    bundled_root: Path, tmp_path: Path
) -> None:
    write_design(bundled_root)
    ctx = context(tmp_path)
    view = inspect_workflow(
        "email-triage",
        settings=ctx.settings,
        scope=ctx.session.profile_scope,
        tools=ToolRegistry([]),
    )
    path = export_visualization(view, settings=ctx.settings, scope=ctx.session.profile_scope)
    async with controlled_child() as (child, connected):
        chrome = tmp_path / "google-chrome"
        chrome.write_text(
            f"#!{sys.executable}\nimport sys\n"
            "if '--version' in sys.argv:\n"
            "    print('Google Chrome 140.0.0.0')\n"
            "    sys.exit(0)\n" + child
        )
        chrome.chmod(0o700)
        # Exercise the real CLI entrypoint and launcher inside run_shell's captured pipes.
        script = (
            "from pathlib import Path\n"
            "from ricky.config import RickySettings\n"
            "from ricky.interfaces.cli import workflows\n"
            "workflows.load_settings = lambda: RickySettings(browser="
            f"{{'executable_path': {str(chrome)!r}}})\n"
            f"workflows.workflow_open_view(Path({str(path)!r}), profile=None, access_profiles=[])\n"
        )
        task = asyncio.create_task(
            RunShellTool().run(
                RunShellParams(
                    command=shlex.join([sys.executable, "-c", script]), timeout_seconds=15
                ),
                ctx,
            )
        )
        try:
            reader, _ = await asyncio.wait_for(asyncio.shield(connected), 10)
            assert await asyncio.wait_for(reader.readexactly(5), 5) == b"ready"
            result = await asyncio.wait_for(asyncio.shield(task), 5)
            assert not result.is_error
            assert "Sent the blueprint to Chrome on this host." in result.content
            assert not reader.at_eof()  # The desktop process is still waiting for release.
        finally:
            # Release Chrome before draining the shell task, also on regression failure.
            if connected.done():
                connected.result()[1].write(b"x")
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)


@pytest.mark.parametrize("interrupt", ["cancel", "timeout"])
async def test_shell_reaps_pipe_holding_child_after_parent_exits(tmp_path, interrupt, monkeypatch):
    ctx = context(tmp_path)
    started = asyncio.get_running_loop().create_future()
    original = asyncio.create_subprocess_shell

    async def capture(*args, **kwargs):
        process = await original(*args, **kwargs)
        started.set_result(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_shell", capture)
    async with controlled_child() as (child, connected):
        parent = f"import subprocess, sys\nsubprocess.Popen([sys.executable, '-c', {child!r}])\n"
        task = asyncio.create_task(
            RunShellTool().run(
                RunShellParams(
                    command=shlex.join([sys.executable, "-c", parent]),
                    timeout_seconds=0.5 if interrupt == "timeout" else 15,
                ),
                ctx,
            )
        )
        try:
            reader, _ = await asyncio.wait_for(asyncio.shield(connected), 5)
            assert await asyncio.wait_for(reader.readexactly(5), 5) == b"ready"
            process = await started
            async with asyncio.timeout(5):
                while process.returncode is None:
                    await asyncio.sleep(0.001)
            if interrupt == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(task), 5)
            else:
                result = await asyncio.wait_for(asyncio.shield(task), 5)
                assert result.is_error and "timed out" in result.content
            assert await asyncio.wait_for(reader.read(), 5) == b""
        finally:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)


async def test_shell_cancellation_waits_for_late_process_creation(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    original = asyncio.create_subprocess_shell
    started = asyncio.Event()
    release = asyncio.Event()
    processes = []

    async def delayed(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        started.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_shell", delayed)
    async with controlled_child() as (child, connected):
        task = asyncio.create_task(
            RunShellTool().run(
                RunShellParams(
                    command=shlex.join([sys.executable, "-c", child]), timeout_seconds=15
                ),
                ctx,
            )
        )
        try:
            reader, _ = await asyncio.wait_for(asyncio.shield(connected), 5)
            assert await asyncio.wait_for(reader.readexactly(5), 5) == b"ready"
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            await asyncio.sleep(0)  # Deliver cancellation while the launch is still pending.
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 5)
            assert processes[0].returncode is not None
            assert await asyncio.wait_for(reader.read(), 5) == b""
        finally:
            release.set()
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
