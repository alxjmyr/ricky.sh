"""Real Chromium integration coverage for the browser boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import socket
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs
from urllib.request import urlopen

import pytest
from PIL import Image
from pydantic import SecretStr

from ricky.agent import AgentSession
from ricky.attachments import LoadedAttachment, browser_download_path
from ricky.browser.install import BrowserInstallationError, browser_status
from ricky.browser.playwright_backend import PlaywrightBrowserBackend
from ricky.browser.service import BrowserService, BrowserVisualCapture
from ricky.browser.tools import (
    BrowserCommitParams,
    BrowserCommitTool,
    BrowserCoordinateCommitParams,
    BrowserCoordinateCommitTool,
)
from ricky.browser.types import (
    BrowserActionRequest,
    BrowserActionTarget,
    BrowserCoordinateTarget,
    BrowserDialogPolicy,
    BrowserError,
    BrowserSnapshot,
    BrowserVisualCandidate,
)
from ricky.config import RickySettings
from ricky.profiles import ProfileResourceRef
from ricky.protected_values import (
    ProtectedDestinationPolicy,
    ProtectedFieldDescriptor,
    ProtectedMaterial,
    ProtectedUseRecord,
    ProtectedUseRequest,
    ProtectedValueDescriptor,
)
from ricky.tools import ToolContext

pytestmark = pytest.mark.browser_integration

# Capture the operator-selected installation root before the autouse test fixture
# replaces RICKY_USER_DATA_DIR and HOME with isolated temporary directories.
_CAPTURED_USER_DATA_DIR = Path(
    os.environ.get("RICKY_USER_DATA_DIR", "~/.ricky")
).expanduser().resolve()
_BINARY_DIR_OVERRIDE = os.environ.get("RICKY_BROWSER_TEST_BINARY_DIR")
_CAPTURED_BINARY_DIR = (
    Path(_BINARY_DIR_OVERRIDE).expanduser().resolve()
    if _BINARY_DIR_OVERRIDE is not None
    else _CAPTURED_USER_DATA_DIR / "browser" / "browsers"
)


@dataclass(frozen=True)
class _InstalledBrowser:
    binary_dir: Path
    executable: Path


class _FixtureHandler(BaseHTTPRequestHandler):
    server: Any

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = self.path.partition("?")[0]
        self.server.requests[path] += 1
        if path == "/start":
            self.send_response(302)
            self.send_header("Location", "/page?token=server-secret&source=fixture")
            self.end_headers()
            return
        if path == "/redirect-blocked":
            self.send_response(302)
            self.send_header("Location", self.server.redirect_target)
            self.end_headers()
            return
        if path == "/download":
            body = b"download content"
            self.send_response(200)
            self.send_header("Content-Disposition", 'attachment; filename="blocked.txt"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)
            return
        if path == "/phase4-download":
            body = b"phase four download body"
            self.send_response(200)
            self.send_header(
                "Content-Disposition", 'attachment; filename="phase-four-download.txt"'
            )
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)
            return
        if path == "/phase4-download-oversized":
            body = b"x" * 64
            self.send_response(200)
            self.send_header("Content-Disposition", 'attachment; filename="oversized-download.bin"')
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)
            return
        if path == "/phase4-pixels":
            body = str(self.server.requests["pixel-version"]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)
            return
        if path == "/phase6-destination":
            body = str(self.server.requests["phase6-destination-version"]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)
            return
        if path == "/slow":
            time.sleep(1.0)
        if path == "/phase4":
            body = b"""<!doctype html>
<html>
  <head><title>Phase four fixture</title></head>
  <body>
    <h1>Files and visual fallback</h1>
    <label>Reviewed document
      <input id="reviewed-file" type="file" aria-label="Reviewed document">
    </label>
    <output id="upload-digest">No upload</output>
    <a href="/phase4-download">Download phase four fixture</a>
    <a href="/phase4-download-oversized">Download oversized fixture</a>
    <label>Visible sentinel
      <input aria-label="Visible sentinel" value="ordinary-editable-sentinel">
    </label>
    <label>Protected sentinel
      <input type="password" aria-label="Protected sentinel" value="protected-sentinel">
    </label>
    <canvas id="visual-canvas" width="240" height="120" tabindex="0"
            aria-label="Visual canvas"></canvas>
    <output id="canvas-result">Canvas waiting</output>
    <script>
      const hex = bytes => Array.from(new Uint8Array(bytes))
        .map(value => value.toString(16).padStart(2, '0')).join('');
      document.querySelector('#reviewed-file').addEventListener('change', async event => {
        const files = Array.from(event.target.files);
        const digests = [];
        for (const file of files) {
          digests.push(hex(await crypto.subtle.digest('SHA-256', await file.arrayBuffer())));
        }
        document.querySelector('#upload-digest').textContent = digests.join(',');
      });
      const canvas = document.querySelector('#visual-canvas');
      const context = canvas.getContext('2d');
      let pixelVersion = -1;
      const draw = version => {
        context.fillStyle = Number(version) === 0 ? '#1473e6' : '#ef5b25';
        context.fillRect(0, 0, canvas.width, canvas.height);
        context.fillStyle = '#ffffff';
        context.font = '20px sans-serif';
        context.fillText('pixel-only control', 24, 65);
      };
      const refreshPixels = async () => {
        const version = Number(await (await fetch('/phase4-pixels')).text());
        if (version !== pixelVersion) {
          pixelVersion = version;
          draw(version);
        }
      };
      refreshPixels();
      setInterval(refreshPixels, 100);
      canvas.addEventListener('click', () => {
        document.querySelector('#canvas-result').textContent = 'Canvas clicked';
      });
    </script>
  </body>
</html>"""
        elif path == "/phase5":
            body = b"""<!doctype html>
<html>
  <head><title>Phase five protected fixture</title></head>
  <body>
    <h1>Protected form fixture</h1>
    <form method="post" action="/submitted">
      <label>Account username
        <input name="username" autocomplete="username">
      </label>
      <label>Account password
        <input name="password" type="password" autocomplete="current-password">
      </label>
      <label>One-time code
        <input name="otp" inputmode="numeric" autocomplete="one-time-code">
      </label>
      <label>Cardholder
        <input name="cardholder" autocomplete="cc-name">
      </label>
      <label>Card number
        <input name="card_number" inputmode="numeric" autocomplete="cc-number">
      </label>
      <label>Card expiry
        <input name="card_expiry" autocomplete="cc-exp">
      </label>
      <label>Card security code
        <input name="card_csc" inputmode="numeric" autocomplete="cc-csc">
      </label>
      <button>Submit protected fixture</button>
    </form>
  </body>
</html>"""
        elif path == "/phase6":
            body = b"""<!doctype html>
<html>
  <head><title>Phase six transaction fixture</title></head>
  <body>
    <h1>Transaction approval fixture</h1>
    <form method="post" action="/phase6-paid" aria-label="Synthetic checkout">
      <input type="hidden" name="payee" value="Example Events">
      <input type="hidden" name="total" value="19.50 USD">
      <label>Card number
        <input name="card_number" inputmode="numeric" autocomplete="cc-number">
      </label>
      <button>Pay 19.50 USD</button>
    </form>
    <form method="post" action="/phase6-form" aria-label="Volunteer application">
      <label>Volunteer name <input name="volunteer_name" value="Ricky Tester"></label>
      <button>Submit volunteer application</button>
    </form>
  </body>
</html>"""
        elif path == "/phase6-stale":
            body = b"""<!doctype html>
<html>
  <head><title>Phase six stale binding fixture</title></head>
  <body>
    <form id="stale-form" method="post" action="/phase6-stale-a">
      <button>Submit stale fixture</button>
    </form>
    <script>
      setInterval(async () => {
        const version = Number(await (await fetch('/phase6-destination')).text());
        document.querySelector('#stale-form').action = version === 0 ?
          '/phase6-stale-a' : '/phase6-stale-b';
      }, 40);
    </script>
  </body>
</html>"""
        elif path == "/phase6-backend-guards":
            overflow_controls = b"".join(
                f'<input type="hidden" name="dummy_{index}">'.encode() for index in range(101)
            )
            body = (
                b"""<!doctype html>
<html>
  <head><title>Phase six backend guards</title></head>
  <body>
    <form id="rewrite-form" method="post" action="/phase6-reviewed-a"
          onsubmit="this.action='/phase6-reviewed-b'">
      <button>Submit reviewed rewrite</button>
    </form>
    <button type="button" onclick="location.href='/phase6-js-unknown'">
      Continue with JavaScript
    </button>
    <button type="button">Place order</button>
    <form aria-label="Large non-financial form">"""
                + overflow_controls
                + b"""
      <button>Continue large form</button>
    </form>
  </body>
</html>"""
            )
        elif path == "/phase4-nested-frame":
            body = b"""<!doctype html>
<html>
  <body>
    <input type="file" aria-label="Nested frame file">
    <button aria-label="Nested frame safe"
      onclick="this.textContent='Nested clicked'">Click nested</button>
  </body>
</html>"""
        elif path == "/phase4-nested":
            body = b"""<!doctype html>
<html>
  <head><title>Nested coordinate fixture</title></head>
  <body>
    <h1>Nested coordinate controls</h1>
    <iframe title="Nested frame" width="360" height="100"
      src="/phase4-nested-frame"></iframe>
    <div id="shadow-host"></div>
    <output id="nested-result">Nested waiting</output>
    <script>
      const shadow = document.querySelector('#shadow-host').attachShadow({mode: 'open'});
      shadow.innerHTML = `<input type="password"
        aria-label="Nested shadow password" value="secret">`;
    </script>
  </body>
</html>"""
        elif path == "/actions":
            body = b"""<!doctype html>
<html>
  <head><title>Action fixture</title></head>
  <body>
    <h1>Ordinary form</h1>
    <form method="post" action="/submitted">
      <label>Display name <input name="display_name"></label>
      <label>Plan
        <select name="plan">
          <option>Basic</option>
          <option>Plus</option>
        </select>
      </label>
      <label>Ambiguous plan
        <select name="ambiguous_plan">
          <option>Same</option>
          <option>Same</option>
        </select>
      </label>
      <label><input type="checkbox" name="updates"> Product updates</label>
      <div contenteditable="true" aria-label="Account password">
        <span>nested-contenteditable-secret</span>
      </div>
      <button>Continue</button>
    </form>
    <output>Waiting</output>
    <button type="button" onclick="alert('Fixture notice')">Show notice</button>
    <button type="button" onclick="confirm('Confirm fixture action?')">Confirm locally</button>
    <a href="/popup" target="_blank">Open details</a>
    <a href="/redirect-blocked" target="_blank">Blocked popup</a>
    <button type="button" onclick="location.reload()">Reload this page</button>
    <a href="data:text/html,blocked">Data destination</a>
    <a href="javascript:fetch('/scheme-executed')">Script destination</a>
    <a id="blob-destination">Blob destination</a>
    <form action="data:text/html,blocked">
      <button>Unsafe form</button>
    </form>
    <button type="button" onclick="window.open('data:text/html,blocked')">
      Open scripted data
    </button>
    <button type="button"
            onclick="window.open(document.querySelector('#blob-destination').href)">
      Open scripted blob
    </button>
    <button type="button"
            onclick="const popup = window.open(); popup.document.write('blocked')">
      Open scripted blank
    </button>
    <button type="button" onclick="location.href = 'about:blank'">Navigate blank</button>
    <input type="password" aria-label="Account password" value="fixture-password-secret">
    <input autocomplete="username webauthn" aria-label="Profile alias" value="fixture-user">
    <input inputmode="numeric" autocomplete="one-time-code"
           aria-label="Temporary digits" value="123456">
    <input type="number" autocomplete="cc-number"
           aria-label="Billing digits" value="4111111111111111">
    <input type="file" aria-label="Supporting document">
    <iframe title="Action evidence frame" src="/frame"></iframe>
    <script>
      document.querySelector('#blob-destination').href =
        URL.createObjectURL(new Blob(['blocked'], {type: 'text/html'}));
    </script>
  </body>
</html>"""
        elif path == "/persistent":
            body = b"""<!doctype html>
<html>
  <head><title>Persistent fixture</title></head>
  <body>
    <h1>Persistent browser state</h1>
    <output id="state">unknown</output>
    <button type="button" onclick="localStorage.setItem('ricky-marker', 'remembered')">
      Save local marker
    </button>
    <script>
      document.querySelector('#state').textContent =
        localStorage.getItem('ricky-marker') || 'missing';
    </script>
  </body>
</html>"""
        elif path == "/popup":
            body = (
                b"<html><head><title>Popup fixture</title></head>"
                b"<body><h1>Popup details</h1></body></html>"
            )
        elif path == "/frame":
            body = b"""<html><body>
<h2>Frame evidence</h2>
<button type="button" onclick="document.querySelector('output').textContent = 'Frame ready'">
  Frame preview
</button>
<output>Frame waiting</output>
</body></html>"""
        else:
            body = b"""<!doctype html>
<html>
  <head><title>Browser fixture</title></head>
  <body>
    <h1>Research evidence</h1>
    <a href="/next">Continue research</a>
    <input type="password" aria-label="Password" value="fixture-password-secret">
    <input aria-label="Payment card" value="4111111111111111">
    <table><tr><th>Item</th><th>Value</th></tr><tr><td>Alpha</td><td>42</td></tr></table>
    <iframe title="Evidence frame" src="/frame"></iframe>
    <div style="height: 1800px">Scrollable space</div>
    <h2>Lower evidence</h2>
  </body>
</html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        with suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        path = self.path.partition("?")[0]
        self.server.requests[path] += 1
        content_length = int(self.headers.get("Content-Length", "0"))
        submitted = parse_qs(self.rfile.read(content_length).decode("utf-8"))
        self.server.submissions.append(submitted)
        for field, values in submitted.items():
            for value in values:
                self.server.requests[f"submission:{field}={value}"] += 1
        if self.server.post_hook is not None:
            self.server.post_hook(path)
        body = (
            b"<html><head><title>Submitted</title></head>"
            b"<body><h1>Reservation received</h1></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        with suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args
        return None


@contextmanager
def _fixture_server(
    *,
    redirect_target: str = "/",
    post_hook: Callable[[str], None] | None = None,
) -> Iterator[tuple[str, Counter[str]]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    server.requests = Counter()  # type: ignore[attr-defined]
    server.submissions = []  # type: ignore[attr-defined]
    server.redirect_target = redirect_target  # type: ignore[attr-defined]
    server.post_hook = post_hook  # type: ignore[attr-defined]
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    try:
        host = str(server.server_address[0])
        port = server.server_port
        yield f"http://{host}:{port}", server.requests  # type: ignore[attr-defined]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _owned_chromium_pid(state_dir: Path) -> int:
    """Resolve only the main Chromium process for one exact isolated state directory."""

    proc = Path("/proc")
    if not proc.is_dir():
        pytest.skip("Chromium process failure drill requires procfs")
    expected = str(state_dir).encode()
    matches: list[int] = []
    for candidate in proc.iterdir():
        if not candidate.name.isdigit():
            continue
        try:
            arguments = (candidate / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if any(expected in item for item in arguments):
            matches.append(int(candidate.name))
    matched = set(matches)
    roots: list[int] = []
    for pid in matches:
        try:
            status = (proc / str(pid) / "status").read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        parent_line = next(line for line in status.splitlines() if line.startswith("PPid:"))
        parent = int(parent_line.partition(":")[2].strip())
        if parent not in matched:
            roots.append(pid)
    assert len(roots) == 1, f"expected one Chromium owner for {state_dir}, found {roots}"
    return roots[0]


async def _wait_for_process_exit(pid: int) -> None:
    for _ in range(100):
        if not Path(f"/proc/{pid}").exists():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"process {pid} did not exit after SIGKILL")


@pytest.fixture
async def installed_browser() -> _InstalledBrowser:
    lookup_root = _CAPTURED_BINARY_DIR.parent
    binary_dir = _CAPTURED_BINARY_DIR.name
    lookup_settings = RickySettings.model_validate(
        {"user_data_dir": str(lookup_root), "browser": {"binary_dir": binary_dir}}
    )
    try:
        status = await browser_status(lookup_settings)
    except BrowserInstallationError:
        pytest.skip(
            "Playwright Chromium readiness could not be inspected; "
            "run uv run ricky browser install (or set RICKY_BROWSER_TEST_BINARY_DIR)"
        )
    if not status.ready:
        pytest.skip(
            "Playwright Chromium is not installed; run uv run ricky browser install "
            "(or set RICKY_BROWSER_TEST_BINARY_DIR)"
        )
    assert status.executable is not None
    return _InstalledBrowser(
        binary_dir=Path(status.install_dir),
        executable=Path(status.executable),
    )


async def test_real_chromium_navigates_snapshots_scrolls_and_blocks_private_origin(
    installed_browser: _InstalledBrowser,
    tmp_path: Path,
) -> None:
    with (
        _fixture_server() as (blocked_origin, blocked_requests),
        _fixture_server(redirect_target=f"{blocked_origin}/must-not-run") as (
            allowed_origin,
            allowed_requests,
        ),
    ):
        settings = RickySettings.model_validate(
            {
                "user_data_dir": str(tmp_path / "user"),
                "project_data_dir": str(tmp_path / "project"),
                "browser": {
                    "enabled": True,
                    "headless": True,
                    "navigation_timeout_seconds": 0.5,
                    "allowed_private_origins": [allowed_origin],
                },
            }
        )
        service = BrowserService(
            settings,
            scope=settings.resolve_profile_scope(),
            backend=PlaywrightBrowserBackend(),
            executable_path=installed_browser.executable,
        )
        try:
            session = await service.open_session(headless=True)
            navigation = await service.navigate(
                session.session_id,
                page_id=None,
                url=f"{allowed_origin}/start",
            )
            snapshot = await service.snapshot(session.session_id, page_id=None)
            scrolled = await service.scroll(
                session.session_id,
                page_id=None,
                direction="down",
                amount=900,
            )

            assert navigation.page.title == "Browser fixture"
            assert navigation.page.origin == allowed_origin
            assert navigation.page.url == (f"{allowed_origin}/page?token=redacted&source=present")
            assert "server-secret" not in navigation.model_dump_json()
            assert "Research evidence" in snapshot.content
            assert "Continue research" in snapshot.content
            assert "Alpha" in snapshot.content
            assert "fixture-password-secret" not in snapshot.content
            assert "4111111111111111" not in snapshot.content
            assert snapshot.targets
            assert scrolled.amount == 900
            assert allowed_requests["/start"] == 1
            assert allowed_requests["/page"] == 1
            assert allowed_requests["/frame"] == 1

            with pytest.raises(BrowserError) as blocked:
                await service.navigate(
                    session.session_id,
                    page_id=None,
                    url=f"{blocked_origin}/must-not-run",
                )
            assert blocked.value.failure.code == "destination_blocked"
            assert blocked_requests["/must-not-run"] == 0

            with pytest.raises(BrowserError) as rejected_redirect:
                await service.navigate(
                    session.session_id,
                    page_id=None,
                    url=f"{allowed_origin}/redirect-blocked",
                )
            assert rejected_redirect.value.failure.code == "destination_blocked"
            assert allowed_requests["/redirect-blocked"] == 1
            assert blocked_requests["/must-not-run"] == 0

            with pytest.raises(BrowserError) as download:
                await service.navigate(
                    session.session_id,
                    page_id=None,
                    url=f"{allowed_origin}/download",
                )
            assert download.value.failure.code == "download_blocked"
            assert allowed_requests["/download"] == 1

            with pytest.raises(BrowserError) as timed_out:
                await service.navigate(
                    session.session_id,
                    page_id=None,
                    url=f"{allowed_origin}/slow",
                )
            assert timed_out.value.failure.code == "navigation_timeout"
            assert timed_out.value.failure.outcome_uncertain
            assert allowed_requests["/slow"] == 1
        finally:
            await service.aclose()

        profile_ephemeral = (
            Path(settings.user_data_dir) / "profiles" / "personal" / settings.browser.ephemeral_dir
        )
        assert not profile_ephemeral.exists()
        assert not Path(settings.project_data_dir).exists()


async def test_real_chromium_phase4_files_visual_masks_and_coordinate_freshness(
    installed_browser: _InstalledBrowser,
    tmp_path: Path,
) -> None:
    with _fixture_server() as (origin, requests):
        settings = RickySettings.model_validate(
            {
                "user_data_dir": str(tmp_path / "user"),
                "project_data_dir": str(tmp_path / "project"),
                "browser": {
                    "enabled": True,
                    "headless": True,
                    "allowed_private_origins": [origin],
                },
                "profile_configs": {
                    "personal": {"browser": {"screenshot_allowed_providers": ["openrouter"]}}
                },
            }
        )
        service = BrowserService(
            settings,
            scope=settings.resolve_profile_scope(),
            backend=PlaywrightBrowserBackend(),
            executable_path=installed_browser.executable,
        )
        download_path: Path | None = None
        try:
            session = await service.open_session(headless=True)
            await service.navigate(session.session_id, page_id=None, url=f"{origin}/phase4")
            snapshot = await service.snapshot(session.session_id, page_id=None)

            reviewed = b"exact bytes reviewed before permission"
            reviewed_digest = hashlib.sha256(reviewed).hexdigest()
            source = tmp_path / "reviewed.txt"
            source.write_bytes(reviewed)
            prepared = LoadedAttachment(
                filename="reviewed.txt",
                media_type="text/plain",
                content=reviewed,
                sha256=reviewed_digest,
                source_path=source,
            )
            source.write_bytes(b"changed after review")
            upload = await service.upload(
                _target(snapshot, "Reviewed document"),
                (prepared,),
            )
            assert upload.disposition == "performed"
            snapshot = await _wait_for_snapshot_text(
                service,
                session.session_id,
                reviewed_digest,
            )
            assert hashlib.sha256(source.read_bytes()).hexdigest() != reviewed_digest

            downloaded = await service.download(_target(snapshot, "Download phase four fixture"))
            assert downloaded.disposition == "performed", downloaded
            assert downloaded.download is not None
            assert downloaded.download.profile == "personal"
            assert (
                downloaded.download.sha256
                == hashlib.sha256(b"phase four download body").hexdigest()
            )
            download_path = browser_download_path(settings, downloaded.download)
            assert download_path.read_bytes() == b"phase four download body"
            assert download_path.is_relative_to(
                Path(settings.user_data_dir) / "profiles" / "personal"
            )
            assert not Path(settings.project_data_dir).exists()

            await service.snapshot(session.session_id, page_id=None)
            visual = await service.visual_snapshot(
                session.session_id,
                page_id=None,
                provider="openrouter",
            )
            candidates = {item.descriptor.name: item for item in visual.candidates}
            assert {"Visual canvas", "Visible sentinel", "Protected sentinel"} <= candidates.keys()
            assert all(item.target.ref.startswith("d") for item in visual.candidates)
            with Image.open(BytesIO(visual.png)) as screenshot:
                for name in ("Visible sentinel", "Protected sentinel", "Reviewed document"):
                    x, y = _visual_candidate_point(visual, candidates[name])
                    assert screenshot.convert("RGB").getpixel((x, y)) == (75, 0, 130)

            protected_x, protected_y = _visual_candidate_point(
                visual,
                candidates["Protected sentinel"],
            )
            with pytest.raises(BrowserError) as protected:
                await service.prepare_coordinate_commit(
                    BrowserCoordinateTarget(
                        session_id=session.session_id,
                        page_id=visual.page.page_id,
                        screenshot_id=visual.snapshot_id,
                        x=protected_x,
                        y=protected_y,
                    ),
                    dialog=BrowserDialogPolicy(),
                )
            assert protected.value.failure.code == "protected_field"

            visual = await service.visual_snapshot(session.session_id, page_id=None)
            candidates = {item.descriptor.name: item for item in visual.candidates}
            file_x, file_y = _visual_candidate_point(visual, candidates["Reviewed document"])
            with pytest.raises(BrowserError) as file_rejected:
                await service.prepare_coordinate_commit(
                    BrowserCoordinateTarget(
                        session_id=session.session_id,
                        page_id=visual.page.page_id,
                        screenshot_id=visual.snapshot_id,
                        x=file_x,
                        y=file_y,
                    ),
                    dialog=BrowserDialogPolicy(),
                )
            assert file_rejected.value.failure.code == "file_control"

            visual = await service.visual_snapshot(
                session.session_id,
                page_id=None,
                provider="openrouter",
            )
            candidates = {item.descriptor.name: item for item in visual.candidates}
            canvas_x, canvas_y = _visual_candidate_point(visual, candidates["Visual canvas"])
            ordinary = await service.prepare_coordinate_click(
                BrowserCoordinateTarget(
                    session_id=session.session_id,
                    page_id=visual.page.page_id,
                    screenshot_id=visual.snapshot_id,
                    x=canvas_x,
                    y=canvas_y,
                )
            )
            await asyncio.sleep(0.05)
            ordinary_clicked = await service.coordinate_click_prepared(ordinary)
            if (
                ordinary_clicked.failure is not None
                and ordinary_clicked.failure.code == "stale_target"
            ):
                await service.snapshot(session.session_id, page_id=None)
                visual = await service.visual_snapshot(
                    session.session_id,
                    page_id=None,
                    provider="openrouter",
                )
                canvas = next(
                    item for item in visual.candidates if item.descriptor.name == "Visual canvas"
                )
                canvas_x, canvas_y = _visual_candidate_point(visual, canvas)
                ordinary = await service.prepare_coordinate_click(
                    BrowserCoordinateTarget(
                        session_id=session.session_id,
                        page_id=visual.page.page_id,
                        screenshot_id=visual.snapshot_id,
                        x=canvas_x,
                        y=canvas_y,
                    )
                )
                await asyncio.sleep(0.05)
                ordinary_clicked = await service.coordinate_click_prepared(ordinary)
            assert ordinary_clicked.disposition == "performed", ordinary_clicked
            await _wait_for_snapshot_text(service, session.session_id, "Canvas clicked")

            await service.snapshot(session.session_id, page_id=None)
            visual = await service.visual_snapshot(
                session.session_id,
                page_id=None,
                provider="openrouter",
            )
            candidates = {item.descriptor.name: item for item in visual.candidates}
            canvas_x, canvas_y = _visual_candidate_point(visual, candidates["Visual canvas"])
            clicked = await _coordinate_transaction(
                service,
                BrowserCoordinateTarget(
                    session_id=session.session_id,
                    page_id=visual.page.page_id,
                    screenshot_id=visual.snapshot_id,
                    x=canvas_x,
                    y=canvas_y,
                ),
            )
            assert clicked.disposition == "performed", clicked
            await _wait_for_snapshot_text(service, session.session_id, "Canvas clicked")

            stale_visual = await service.visual_snapshot(session.session_id, page_id=None)
            stale_canvas = next(
                item for item in stale_visual.candidates if item.descriptor.name == "Visual canvas"
            )
            stale_x, stale_y = _visual_candidate_point(stale_visual, stale_canvas)
            requests["pixel-version"] = 1
            await asyncio.sleep(0.3)
            with pytest.raises(BrowserError) as stale:
                await service.prepare_coordinate_commit(
                    BrowserCoordinateTarget(
                        session_id=session.session_id,
                        page_id=stale_visual.page.page_id,
                        screenshot_id=stale_visual.snapshot_id,
                        x=stale_x,
                        y=stale_y,
                    ),
                    dialog=BrowserDialogPolicy(),
                )
            assert stale.value.failure.code == "stale_target"

            await service.navigate(
                session.session_id,
                page_id=None,
                url=f"{origin}/phase4-nested",
            )
            nested_visual = await service.visual_snapshot(session.session_id, page_id=None)
            nested_candidates = {item.descriptor.name: item for item in nested_visual.candidates}
            assert {
                "Nested frame file",
                "Nested frame safe",
                "Nested shadow password",
            } <= nested_candidates.keys()
            nested_file_x, nested_file_y = _visual_candidate_point(
                nested_visual,
                nested_candidates["Nested frame file"],
            )
            with pytest.raises(BrowserError) as nested_file:
                await service.prepare_coordinate_commit(
                    BrowserCoordinateTarget(
                        session_id=session.session_id,
                        page_id=nested_visual.page.page_id,
                        screenshot_id=nested_visual.snapshot_id,
                        x=nested_file_x,
                        y=nested_file_y,
                    ),
                    dialog=BrowserDialogPolicy(),
                )
            assert nested_file.value.failure.code == "file_control"

            nested_visual = await service.visual_snapshot(session.session_id, page_id=None)
            nested_candidates = {item.descriptor.name: item for item in nested_visual.candidates}
            shadow_x, shadow_y = _visual_candidate_point(
                nested_visual,
                nested_candidates["Nested shadow password"],
            )
            with pytest.raises(BrowserError) as nested_protected:
                await service.prepare_coordinate_commit(
                    BrowserCoordinateTarget(
                        session_id=session.session_id,
                        page_id=nested_visual.page.page_id,
                        screenshot_id=nested_visual.snapshot_id,
                        x=shadow_x,
                        y=shadow_y,
                    ),
                    dialog=BrowserDialogPolicy(),
                )
            assert nested_protected.value.failure.code == "protected_field"

            nested_visual = await service.visual_snapshot(session.session_id, page_id=None)
            nested_candidates = {item.descriptor.name: item for item in nested_visual.candidates}
            safe_x, safe_y = _visual_candidate_point(
                nested_visual,
                nested_candidates["Nested frame safe"],
            )
            nested_safe = await _coordinate_transaction(
                service,
                BrowserCoordinateTarget(
                    session_id=session.session_id,
                    page_id=nested_visual.page.page_id,
                    screenshot_id=nested_visual.snapshot_id,
                    x=safe_x,
                    y=safe_y,
                ),
            )
            assert nested_safe.disposition == "performed", nested_safe
            await _wait_for_snapshot_text(service, session.session_id, "Nested clicked")
        finally:
            await service.aclose()

        assert download_path is not None and download_path.exists()
        ephemeral = (
            Path(settings.user_data_dir) / "profiles" / "personal" / settings.browser.ephemeral_dir
        )
        assert not ephemeral.exists()


async def test_real_chromium_classifies_and_fills_protected_field_without_commit(
    installed_browser: _InstalledBrowser,
    tmp_path: Path,
) -> None:
    sentinel = "real-browser-protected-sentinel-8041"
    with _fixture_server() as (origin, requests):
        settings = RickySettings.model_validate(
            {
                "user_data_dir": str(tmp_path / "user"),
                "project_data_dir": str(tmp_path / "project"),
                "browser": {
                    "enabled": True,
                    "headless": True,
                    "allowed_private_origins": [origin],
                },
            }
        )
        service = BrowserService(
            settings,
            scope=settings.resolve_profile_scope(),
            backend=PlaywrightBrowserBackend(),
            executable_path=installed_browser.executable,
        )
        try:
            session = await service.open_session(headless=True)
            await service.navigate(session.session_id, page_id=None, url=f"{origin}/phase5")
            snapshot = await service.snapshot(session.session_id, page_id=None)
            kinds = {item.name: item.protected_kind for item in snapshot.descriptors}
            assert kinds["Account username"] == "username"
            assert kinds["Account password"] == "password"
            assert kinds["One-time code"] == "one_time_code"
            assert kinds["Cardholder"] == "cardholder_name"
            assert kinds["Card number"] == "card_number"
            assert kinds["Card expiry"] == "card_expiry"
            assert kinds["Card security code"] == "card_security_code"

            password_target = _target(snapshot, "Account password")
            ordinary = await service.action(
                password_target,
                BrowserActionRequest(kind="fill", value="ordinary-path-must-fail"),
            )
            assert ordinary.disposition == "not_performed"
            assert ordinary.failure is not None
            assert ordinary.failure.code == "protected_field"

            snapshot = await service.snapshot(session.session_id, page_id=None)
            password_target = _target(snapshot, "Account password")
            field = ProtectedFieldDescriptor(
                name="password",
                label="Fixture password",
                mode="stored",
                compatible_controls=("password",),
            )
            now = datetime.now(UTC)
            ref = ProfileResourceRef(profile="personal", name="phase5-fixture")
            use_request = ProtectedUseRequest(
                ref=ref,
                field="password",
                consumer_id="browser.fill",
                control_kind="password",
                top_level_origin=origin,
                frame_origin=origin,
                occurrence=service.protected_occurrence(password_target),
            )
            material = ProtectedMaterial(
                use=ProtectedUseRecord(
                    id="protected_use_" + "a" * 32,
                    request=use_request,
                    resource_revision=1,
                    disposition="materialized",
                    created_at=now,
                    finalized_at=now,
                ),
                descriptor=ProtectedValueDescriptor(
                    ref=ref,
                    kind="credential",
                    label="Phase 5 fixture",
                    fields=(field,),
                    policy=ProtectedDestinationPolicy(mode="strict", authored_origins=(origin,)),
                    revision=1,
                    created_at=now,
                    updated_at=now,
                ),
                field=field,
                value=SecretStr(sentinel),
                authorization="authored",
            )

            class ReviewedBroker:
                async def revalidate(self, reviewed: ProtectedMaterial) -> None:
                    assert reviewed is material

            filled = await service.protected_fill(  # type: ignore[arg-type]
                password_target,
                material,
                cast(Any, ReviewedBroker()),
            )
            assert filled.disposition == "performed"
            assert sentinel not in filled.model_dump_json()
            assert requests["/submitted"] == 0
            assert requests[f"submission:password={sentinel}"] == 0

            after_fill = await service.snapshot(session.session_id, page_id=None)
            assert sentinel not in after_fill.model_dump_json()
            committed = await _semantic_transaction(
                service,
                _target(after_fill, "Submit protected fixture"),
                BrowserActionRequest(kind="commit", activation="click"),
            )
            assert committed.disposition == "performed"
            assert requests["/submitted"] == 1
            assert requests[f"submission:password={sentinel}"] == 1
        finally:
            await service.aclose()


async def test_real_chromium_prepared_financial_and_generic_transaction_envelopes(
    installed_browser: _InstalledBrowser,
    tmp_path: Path,
) -> None:
    card_sentinel = "4242424242424242"
    with _fixture_server() as (origin, requests):
        settings = RickySettings.model_validate(
            {
                "user_data_dir": str(tmp_path / "user"),
                "project_data_dir": str(tmp_path / "project"),
                "browser": {
                    "enabled": True,
                    "headless": True,
                    "allowed_private_origins": [origin],
                },
            }
        )
        service = BrowserService(
            settings,
            scope=settings.resolve_profile_scope(),
            backend=PlaywrightBrowserBackend(),
            executable_path=installed_browser.executable,
        )
        agent_session = AgentSession.create(
            settings,
            profile_scope=settings.resolve_profile_scope(),
        )
        ctx = ToolContext(cwd=tmp_path, settings=settings, session=agent_session)
        try:
            session = await service.open_session(headless=True)
            await service.navigate(session.session_id, page_id=None, url=f"{origin}/phase6")
            snapshot = await service.snapshot(session.session_id, page_id=None)
            card_target = _target(snapshot, "Card number")
            field = ProtectedFieldDescriptor(
                name="card_number",
                label="Synthetic card number",
                mode="stored",
                compatible_controls=("card_number",),
            )
            now = datetime.now(UTC)
            card_ref = ProfileResourceRef(profile="personal", name="phase6-card")
            use_request = ProtectedUseRequest(
                ref=card_ref,
                field="card_number",
                consumer_id="browser.fill",
                control_kind="card_number",
                top_level_origin=origin,
                frame_origin=origin,
                occurrence=service.protected_occurrence(card_target),
            )
            material = ProtectedMaterial(
                use=ProtectedUseRecord(
                    id="protected_use_" + "6" * 32,
                    request=use_request,
                    resource_revision=1,
                    disposition="materialized",
                    created_at=now,
                    finalized_at=now,
                ),
                descriptor=ProtectedValueDescriptor(
                    ref=card_ref,
                    kind="payment_card",
                    label="Phase 6 card",
                    fields=(field,),
                    policy=ProtectedDestinationPolicy(
                        mode="strict",
                        authored_origins=(origin,),
                    ),
                    revision=1,
                    created_at=now,
                    updated_at=now,
                ),
                field=field,
                value=SecretStr(card_sentinel),
                authorization="authored",
            )

            class ReviewedBroker:
                async def revalidate(self, reviewed: ProtectedMaterial) -> None:
                    assert reviewed is material

            filled = await service.protected_fill(  # type: ignore[arg-type]
                card_target,
                material,
                cast(Any, ReviewedBroker()),
            )
            assert filled.disposition == "performed"
            checkout = await service.snapshot(session.session_id, page_id=None)
            financial_args: dict[str, object] = {
                "target": _target(checkout, "Pay 19.50 USD").model_dump(mode="python"),
                "envelope": {
                    "kind": "financial",
                    "intent": "Purchase one synthetic acceptance-test ticket",
                    "payee": "Example Events",
                    "total": {"amount": "19.50", "currency": "USD"},
                    "components": [
                        {
                            "label": "Ticket",
                            "amount": {"amount": "18", "currency": "USD"},
                        }
                    ],
                    "fees": [
                        {
                            "label": "Booking fee",
                            "amount": {"amount": "1.50", "currency": "USD"},
                        }
                    ],
                    "timing": "one_time",
                    "source": {
                        "kind": "protected_value",
                        "protected_value": card_ref.model_dump(mode="python"),
                    },
                    "consequences": ["Creates one synthetic non-refundable order"],
                    "expected_result": "The fixture displays a submission receipt",
                },
            }
            financial_tool = BrowserCommitTool(service)
            financial = await financial_tool.prepare_effect(financial_args, ctx)
            assert financial.prepared.financial_signal
            assert "Proposed total: 19.50 USD" in financial.permission_summary
            assert f'Funding source (locally matched alias): "{card_ref.qualified}"' in (
                financial.permission_summary
            )
            assert f'Effective destination(s):\n- "{origin}/phase6-paid"' in (
                financial.permission_summary
            )
            paid = await financial_tool.run_prepared(
                BrowserCommitParams.model_validate(financial_args),
                financial,
                ctx,
            )
            assert not paid.is_error, paid.content
            assert paid.effect_receipt is not None
            assert paid.effect_receipt.disposition == "performed"
            assert requests["/phase6-paid"] == 1
            assert requests["submission:payee=Example Events"] == 1
            assert requests["submission:total=19.50 USD"] == 1
            assert requests[f"submission:card_number={card_sentinel}"] == 1

            await service.navigate(session.session_id, page_id=None, url=f"{origin}/phase6")
            visual = await service.visual_snapshot(session.session_id, page_id=None)
            application = next(
                item
                for item in visual.candidates
                if item.descriptor.name == "Submit volunteer application"
            )
            x, y = _visual_candidate_point(visual, application)
            coordinate_args: dict[str, object] = {
                "target": BrowserCoordinateTarget(
                    session_id=session.session_id,
                    page_id=visual.page.page_id,
                    screenshot_id=visual.snapshot_id,
                    x=x,
                    y=y,
                ).model_dump(mode="python"),
                "envelope": {
                    "kind": "browser",
                    "intent": "Submit the synthetic volunteer application",
                    "destination": "Example volunteer program",
                    "consequences": ["Creates one synthetic application for review"],
                    "disclosures": ["Volunteer name"],
                    "expected_result": "The fixture displays a submission receipt",
                },
            }
            coordinate_tool = BrowserCoordinateCommitTool(service)
            generic = await coordinate_tool.prepare_effect(coordinate_args, ctx)
            assert not generic.prepared.financial_signal
            assert "NON-FINANCIAL BROWSER TRANSACTION" in generic.permission_summary
            assert f'Effective destination(s):\n- "{origin}/phase6-form"' in (
                generic.permission_summary
            )
            submitted = await coordinate_tool.run_prepared(
                BrowserCoordinateCommitParams.model_validate(coordinate_args),
                generic,
                ctx,
            )
            assert not submitted.is_error, submitted.content
            assert submitted.effect_receipt is not None
            assert submitted.effect_receipt.disposition == "performed"
            deadline = asyncio.get_running_loop().time() + 2
            while requests["/phase6-form"] == 0:
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.02)
            assert requests["/phase6-form"] == 1
            assert requests["submission:volunteer_name=Ricky Tester"] == 1
            await _wait_for_snapshot_text(
                service,
                session.session_id,
                "Reservation received",
            )

            await service.navigate(
                session.session_id,
                page_id=None,
                url=f"{origin}/phase6-stale",
            )
            stale_snapshot = await service.snapshot(session.session_id, page_id=None)
            stale_args: dict[str, object] = {
                "target": _target(stale_snapshot, "Submit stale fixture").model_dump(mode="python"),
                "envelope": {
                    "kind": "browser",
                    "intent": "Submit the synthetic stale fixture",
                    "destination": "Example stale endpoint",
                    "consequences": ["Creates one synthetic stale record"],
                    "disclosures": [],
                    "expected_result": "The fixture displays a submission receipt",
                },
            }
            stale_tool = BrowserCommitTool(service)
            stale = await stale_tool.prepare_effect(stale_args, ctx)
            assert stale.prepared.preflight.effective_destinations == (f"{origin}/phase6-stale-a",)
            requests["phase6-destination-version"] = 1
            await asyncio.sleep(0.2)
            rejected = await stale_tool.run_prepared(
                BrowserCommitParams.model_validate(stale_args),
                stale,
                ctx,
            )
            assert rejected.is_error
            assert rejected.effect_receipt is not None
            assert rejected.effect_receipt.disposition == "not_performed"
            assert requests["/phase6-stale-a"] == 0
            assert requests["/phase6-stale-b"] == 0
        finally:
            await service.aclose()


async def test_real_chromium_binds_dispatch_destination_and_escalates_financial_signals(
    installed_browser: _InstalledBrowser,
    tmp_path: Path,
) -> None:
    with _fixture_server() as (origin, requests):
        settings = RickySettings.model_validate(
            {
                "user_data_dir": str(tmp_path / "user"),
                "project_data_dir": str(tmp_path / "project"),
                "browser": {
                    "enabled": True,
                    "headless": True,
                    "allowed_private_origins": [origin],
                },
            }
        )
        service = BrowserService(
            settings,
            scope=settings.resolve_profile_scope(),
            backend=PlaywrightBrowserBackend(),
            executable_path=installed_browser.executable,
        )
        ctx = ToolContext(
            cwd=tmp_path,
            settings=settings,
            session=AgentSession.create(
                settings,
                profile_scope=settings.resolve_profile_scope(),
            ),
        )
        envelope = {
            "kind": "browser",
            "intent": "Submit one synthetic browser fixture",
            "destination": "Synthetic fixture",
            "consequences": ["Creates one synthetic fixture request"],
            "disclosures": [],
            "expected_result": "The fixture displays a receipt",
        }
        tool = BrowserCommitTool(service)
        coordinate_tool = BrowserCoordinateCommitTool(service)
        try:
            session = await service.open_session(headless=True)
            await service.navigate(
                session.session_id,
                page_id=None,
                url=f"{origin}/phase6-backend-guards",
            )
            snapshot = await service.snapshot(session.session_id, page_id=None)
            rewrite_args: dict[str, object] = {
                "target": _target(snapshot, "Submit reviewed rewrite").model_dump(mode="python"),
                "envelope": envelope,
            }
            rewrite = await tool.prepare_effect(rewrite_args, ctx)
            assert rewrite.prepared.preflight.effective_destinations == (
                f"{origin}/phase6-reviewed-a",
            )

            blocked = await tool.run_prepared(
                BrowserCommitParams.model_validate(rewrite_args),
                rewrite,
                ctx,
            )

            assert blocked.is_error
            assert blocked.effect_receipt is not None
            assert blocked.effect_receipt.disposition == "in_doubt"
            assert requests["/phase6-reviewed-a"] == 0
            assert requests["/phase6-reviewed-b"] == 0

            await service.close_session(session.session_id)
            session = await service.open_session(headless=True)
            await service.navigate(
                session.session_id,
                page_id=None,
                url=f"{origin}/phase6-backend-guards",
            )
            snapshot = await service.snapshot(session.session_id, page_id=None)
            unknown_args: dict[str, object] = {
                "target": _target(snapshot, "Continue with JavaScript").model_dump(mode="python"),
                "envelope": envelope,
            }
            unknown = await tool.prepare_effect(unknown_args, ctx)
            assert unknown.prepared.preflight.effective_destinations == ()
            allowed = await tool.run_prepared(
                BrowserCommitParams.model_validate(unknown_args),
                unknown,
                ctx,
            )
            assert not allowed.is_error, allowed.content
            assert requests["/phase6-js-unknown"] == 1

            await service.navigate(
                session.session_id,
                page_id=None,
                url=f"{origin}/phase6-backend-guards",
            )
            snapshot = await service.snapshot(session.session_id, page_id=None)
            order_args: dict[str, object] = {
                "target": _target(snapshot, "Place order").model_dump(mode="python"),
                "envelope": envelope,
            }
            with pytest.raises(BrowserError, match="requires a financial envelope"):
                await tool.prepare_effect(order_args, ctx)

            visual = await service.visual_snapshot(session.session_id, page_id=None)
            order_candidate = next(
                item for item in visual.candidates if item.descriptor.name == "Place order"
            )
            x, y = _visual_candidate_point(visual, order_candidate)
            with pytest.raises(BrowserError, match="requires a financial envelope"):
                await coordinate_tool.prepare_effect(
                    {
                        "target": BrowserCoordinateTarget(
                            session_id=session.session_id,
                            page_id=visual.page.page_id,
                            screenshot_id=visual.snapshot_id,
                            x=x,
                            y=y,
                        ).model_dump(mode="python"),
                        "envelope": envelope,
                    },
                    ctx,
                )

            await service.navigate(
                session.session_id,
                page_id=None,
                url=f"{origin}/phase6-backend-guards",
            )
            snapshot = await service.snapshot(session.session_id, page_id=None)
            overflow_args: dict[str, object] = {
                "target": _target(snapshot, "Continue large form").model_dump(mode="python"),
                "envelope": envelope,
            }
            with pytest.raises(BrowserError, match="requires a financial envelope"):
                await tool.prepare_effect(overflow_args, ctx)
        finally:
            await service.aclose()


@pytest.mark.skipif(os.name != "posix", reason="process failure drill requires POSIX signals")
async def test_real_chromium_kill_before_commit_dispatch_is_not_performed(
    installed_browser: _InstalledBrowser,
    tmp_path: Path,
) -> None:
    with _fixture_server() as (origin, requests):
        settings = RickySettings.model_validate(
            {
                "user_data_dir": str(tmp_path / "user"),
                "project_data_dir": str(tmp_path / "project"),
                "browser": {
                    "enabled": True,
                    "headless": True,
                    "allowed_private_origins": [origin],
                },
            }
        )
        service = BrowserService(
            settings,
            scope=settings.resolve_profile_scope(),
            backend=PlaywrightBrowserBackend(),
            executable_path=installed_browser.executable,
        )
        ctx = ToolContext(
            cwd=tmp_path,
            settings=settings,
            session=AgentSession.create(
                settings,
                profile_scope=settings.resolve_profile_scope(),
            ),
        )
        tool = BrowserCommitTool(service)
        try:
            session = await service.open_session(headless=True)
            await service.navigate(session.session_id, page_id=None, url=f"{origin}/actions")
            snapshot = await service.snapshot(session.session_id, page_id=None)
            arguments: dict[str, object] = {
                "target": _target(snapshot, "Continue").model_dump(mode="python"),
                "envelope": {
                    "kind": "browser",
                    "intent": "Submit the synthetic fixture form",
                    "destination": "Synthetic fixture server",
                    "consequences": ["Creates one synthetic fixture submission"],
                    "disclosures": [],
                    "expected_result": "The fixture records one submission",
                },
            }
            params = BrowserCommitParams.model_validate(arguments)
            prepared = await tool.prepare_effect(arguments, ctx)
            entry = service._sessions[session.session_id]  # noqa: SLF001 - exact process drill
            assert entry.state_dir is not None
            browser_pid = _owned_chromium_pid(entry.state_dir)
            os.kill(browser_pid, signal.SIGKILL)
            await _wait_for_process_exit(browser_pid)

            result = await tool.run_prepared(params, prepared, ctx)

            assert result.is_error
            assert result.effect_receipt is not None
            assert result.effect_receipt.disposition == "not_performed"
            assert requests["/submitted"] == 0
        finally:
            await service.aclose()


@pytest.mark.skipif(os.name != "posix", reason="process failure drill requires POSIX signals")
async def test_real_chromium_kill_after_commit_dispatch_is_terminal_and_not_replayed(
    installed_browser: _InstalledBrowser,
    tmp_path: Path,
) -> None:
    browser_pid: int | None = None

    def kill_after_post(path: str) -> None:
        nonlocal browser_pid
        if path != "/submitted" or browser_pid is None:
            return
        selected = browser_pid
        browser_pid = None
        os.kill(selected, signal.SIGKILL)

    with _fixture_server(post_hook=kill_after_post) as (origin, requests):
        settings = RickySettings.model_validate(
            {
                "user_data_dir": str(tmp_path / "user"),
                "project_data_dir": str(tmp_path / "project"),
                "browser": {
                    "enabled": True,
                    "headless": True,
                    "allowed_private_origins": [origin],
                },
            }
        )
        service = BrowserService(
            settings,
            scope=settings.resolve_profile_scope(),
            backend=PlaywrightBrowserBackend(),
            executable_path=installed_browser.executable,
        )
        ctx = ToolContext(
            cwd=tmp_path,
            settings=settings,
            session=AgentSession.create(
                settings,
                profile_scope=settings.resolve_profile_scope(),
            ),
        )
        tool = BrowserCommitTool(service)
        try:
            session = await service.open_session(headless=True)
            await service.navigate(session.session_id, page_id=None, url=f"{origin}/actions")
            snapshot = await service.snapshot(session.session_id, page_id=None)
            arguments: dict[str, object] = {
                "target": _target(snapshot, "Continue").model_dump(mode="python"),
                "envelope": {
                    "kind": "browser",
                    "intent": "Submit the synthetic fixture form",
                    "destination": "Synthetic fixture server",
                    "consequences": ["Creates one synthetic fixture submission"],
                    "disclosures": [],
                    "expected_result": "The fixture records one submission",
                },
            }
            params = BrowserCommitParams.model_validate(arguments)
            prepared = await tool.prepare_effect(arguments, ctx)
            entry = service._sessions[session.session_id]  # noqa: SLF001 - exact process drill
            assert entry.state_dir is not None
            browser_pid = _owned_chromium_pid(entry.state_dir)

            result = await tool.run_prepared(params, prepared, ctx)

            assert result.effect_receipt is not None
            assert result.effect_receipt.disposition in {"performed", "in_doubt"}
            assert requests["/submitted"] == 1

            replay = await tool.run_prepared(params, prepared, ctx)
            assert replay.is_error
            assert requests["/submitted"] == 1
        finally:
            await service.aclose()


async def test_real_chromium_rejects_oversized_download_before_publication(
    installed_browser: _InstalledBrowser,
    tmp_path: Path,
) -> None:
    with _fixture_server() as (origin, requests):
        settings = RickySettings.model_validate(
            {
                "user_data_dir": str(tmp_path / "user"),
                "project_data_dir": str(tmp_path / "project"),
                "browser": {
                    "enabled": True,
                    "headless": True,
                    "allowed_private_origins": [origin],
                    "download_file_byte_limit": 8,
                },
            }
        )
        service = BrowserService(
            settings,
            scope=settings.resolve_profile_scope(),
            backend=PlaywrightBrowserBackend(),
            executable_path=installed_browser.executable,
        )
        try:
            session = await service.open_session(headless=True)
            await service.navigate(session.session_id, page_id=None, url=f"{origin}/phase4")
            snapshot = await service.snapshot(session.session_id, page_id=None)

            unexpected = await service.action(
                _target(snapshot, "Download phase four fixture"),
                BrowserActionRequest(kind="click"),
            )
            assert unexpected.disposition == "in_doubt"
            assert unexpected.failure is not None
            # Chromium may surface the aborted attachment as the route failure
            # or as the resulting unsupported error page. Both are fail-closed.
            assert unexpected.failure.code in {"download_blocked", "destination_blocked"}
            await service.close_session(session.session_id)
            session = await service.open_session(headless=True)
            await service.navigate(session.session_id, page_id=None, url=f"{origin}/phase4")
            snapshot = await service.snapshot(session.session_id, page_id=None)
            result = await service.download(_target(snapshot, "Download oversized fixture"))

            assert result.disposition == "in_doubt"
            assert result.download is None
            assert result.failure is not None
            assert result.failure.code == "download_too_large"
            assert result.failure.outcome_uncertain
            assert requests["/phase4-download"] == 1
            assert requests["/phase4-download-oversized"] == 1
            durable = (
                Path(settings.user_data_dir)
                / "profiles"
                / "personal"
                / settings.browser.download_dir
            )
            assert not durable.exists()
        finally:
            await service.aclose()


async def test_real_chromium_interacts_commits_handles_dialog_and_popup(
    installed_browser: _InstalledBrowser,
    tmp_path: Path,
) -> None:
    with _fixture_server() as (origin, requests):
        settings = RickySettings.model_validate(
            {
                "user_data_dir": str(tmp_path / "user"),
                "project_data_dir": str(tmp_path / "project"),
                "browser": {
                    "enabled": True,
                    "headless": True,
                    "allowed_private_origins": [origin],
                },
            }
        )
        service = BrowserService(
            settings,
            scope=settings.resolve_profile_scope(),
            backend=PlaywrightBrowserBackend(),
            executable_path=installed_browser.executable,
        )
        try:
            session = await service.open_session(headless=True)
            await service.navigate(session.session_id, page_id=None, url=f"{origin}/actions")
            snapshot = await service.snapshot(session.session_id, page_id=None)

            reloaded = await service.action(
                _target(snapshot, "Reload this page"),
                BrowserActionRequest(kind="click"),
            )
            assert reloaded.disposition == "performed"
            assert reloaded.postcondition.navigation_occurred
            assert reloaded.snapshot is not None
            assert requests["/actions"] == 2
            snapshot = reloaded.snapshot

            protected_names = {
                descriptor.name
                for descriptor in snapshot.descriptors
                if descriptor.protected or descriptor.file
            }
            assert {
                "Account password",
                "Billing digits",
                "Profile alias",
                "Temporary digits",
            } <= protected_names, (snapshot.descriptors, snapshot.content)
            assert any(
                descriptor.name == "Account password"
                and descriptor.control_kind == "contenteditable"
                and descriptor.protected
                for descriptor in snapshot.descriptors
            ), snapshot.descriptors
            assert "nested-contenteditable-secret" not in snapshot.model_dump_json()
            assert "fixture-password-secret" not in snapshot.model_dump_json()
            assert "fixture-user" not in snapshot.model_dump_json()
            assert "4111111111111111" not in snapshot.model_dump_json()
            assert "123456" not in snapshot.model_dump_json()

            snapshot = await _act(
                service,
                snapshot,
                "Frame preview",
                BrowserActionRequest(kind="click"),
            )
            assert "Frame ready" in snapshot.content

            ambiguous_option = await service.action(
                _target(snapshot, "Ambiguous plan"),
                BrowserActionRequest(kind="select", option_label="Same"),
            )
            assert ambiguous_option.disposition == "not_performed"
            assert ambiguous_option.failure is not None
            assert ambiguous_option.failure.code == "ambiguous_target"

            snapshot = await _act(
                service,
                snapshot,
                "Display name",
                BrowserActionRequest(kind="fill", value="Ricky Tester"),
            )
            snapshot = await _act(
                service,
                snapshot,
                "Display name",
                BrowserActionRequest(kind="press_key", key="End"),
            )
            plan = next(item for item in snapshot.descriptors if item.name == "Plan")
            assert plan.option_labels == ("Basic", "Plus"), (plan, snapshot.content)
            snapshot = await _act(
                service,
                snapshot,
                "Plan",
                BrowserActionRequest(kind="select", option_label="Plus"),
            )
            snapshot = await _act(
                service,
                snapshot,
                "Product updates",
                BrowserActionRequest(kind="set_checked", checked=True),
            )

            notice = await service.action(
                _target(snapshot, "Show notice"),
                BrowserActionRequest(kind="click"),
            )
            assert notice.disposition == "performed"
            assert [(item.kind, item.response) for item in notice.dialogs] == [
                ("alert", "dismissed")
            ]
            assert notice.snapshot is not None
            snapshot = notice.snapshot

            confirmation = await _semantic_transaction(
                service,
                _target(snapshot, "Confirm locally"),
                BrowserActionRequest(
                    kind="commit",
                    activation="click",
                    dialog=BrowserDialogPolicy(response="accept"),
                ),
            )
            assert confirmation.disposition == "performed"
            assert [(item.kind, item.response) for item in confirmation.dialogs] == [
                ("confirm", "accepted")
            ]
            assert confirmation.snapshot is not None
            snapshot = confirmation.snapshot

            original_page_id = snapshot.page.page_id
            popup_target = _target(snapshot, "Open details")
            assert popup_target.ref.startswith("f"), snapshot.content
            popup = await service.action(
                popup_target,
                BrowserActionRequest(kind="click"),
            )
            assert popup.disposition == "performed"
            assert popup.postcondition.page_changes.selected_popup_page_id == popup.page.page_id
            assert popup.snapshot is not None
            assert "Popup details" in popup.snapshot.content

            await service.select_page(session.session_id, original_page_id)
            snapshot = await service.snapshot(
                session.session_id,
                page_id=original_page_id,
            )
            continue_descriptor = next(
                item for item in snapshot.descriptors if item.name == "Continue"
            )
            assert continue_descriptor.consequential
            rejected_submit = await service.action(
                _target(snapshot, "Continue"),
                BrowserActionRequest(kind="click"),
            )
            assert rejected_submit.disposition == "not_performed"
            assert rejected_submit.failure is not None
            assert rejected_submit.failure.code == "consequential_target"
            assert requests["/submitted"] == 0

            submitted = await _semantic_transaction(
                service,
                _target(snapshot, "Continue"),
                BrowserActionRequest(kind="commit", activation="click"),
            )
            assert submitted.disposition == "performed"
            assert submitted.postcondition.navigation_occurred
            assert submitted.snapshot is not None
            assert "Reservation received" in submitted.snapshot.content
            assert requests["/submitted"] == 1
            assert requests["submission:display_name=Ricky Tester"] == 1
            assert requests["submission:plan=Plus"] == 1
            assert requests["submission:updates=on"] == 1
        finally:
            await service.aclose()


async def test_action_triggered_popup_redirect_cannot_bypass_destination_policy(
    installed_browser: _InstalledBrowser,
    tmp_path: Path,
) -> None:
    with (
        _fixture_server() as (blocked_origin, blocked_requests),
        _fixture_server(redirect_target=f"{blocked_origin}/must-not-run") as (
            allowed_origin,
            allowed_requests,
        ),
    ):
        settings = RickySettings.model_validate(
            {
                "user_data_dir": str(tmp_path / "user"),
                "project_data_dir": str(tmp_path / "project"),
                "browser": {
                    "enabled": True,
                    "headless": True,
                    "allowed_private_origins": [allowed_origin],
                },
            }
        )
        service = BrowserService(
            settings,
            scope=settings.resolve_profile_scope(),
            backend=PlaywrightBrowserBackend(),
            executable_path=installed_browser.executable,
        )
        try:
            session = await service.open_session(headless=True)
            await service.navigate(
                session.session_id,
                page_id=None,
                url=f"{allowed_origin}/actions",
            )
            snapshot = await service.snapshot(session.session_id, page_id=None)
            result = await service.action(
                _target(snapshot, "Blocked popup"),
                BrowserActionRequest(kind="click"),
            )

            assert result.disposition == "in_doubt"
            assert result.failure is not None
            assert result.failure.code == "destination_blocked"
            assert result.failure.outcome_uncertain
            assert allowed_requests["/redirect-blocked"] == 1
            assert blocked_requests["/must-not-run"] == 0
        finally:
            await service.aclose()


async def test_non_network_action_destinations_are_blocked(
    installed_browser: _InstalledBrowser,
    tmp_path: Path,
) -> None:
    with _fixture_server() as (origin, requests):
        settings = RickySettings.model_validate(
            {
                "user_data_dir": str(tmp_path / "user"),
                "project_data_dir": str(tmp_path / "project"),
                "browser": {
                    "enabled": True,
                    "headless": True,
                    "allowed_private_origins": [origin],
                },
            }
        )
        service = BrowserService(
            settings,
            scope=settings.resolve_profile_scope(),
            backend=PlaywrightBrowserBackend(),
            executable_path=installed_browser.executable,
        )
        try:
            session = await service.open_session(headless=True)
            await service.navigate(
                session.session_id,
                page_id=None,
                url=f"{origin}/actions",
            )
            snapshot = await service.snapshot(session.session_id, page_id=None)

            for name in ("Data destination", "Script destination", "Blob destination"):
                rejected = await service.action(
                    _target(snapshot, name),
                    BrowserActionRequest(kind="click"),
                )
                assert rejected.disposition == "not_performed"
                assert rejected.failure is not None
                assert rejected.failure.code == "destination_blocked"

            with pytest.raises(BrowserError) as rejected_form:
                await service.prepare_commit(
                    _target(snapshot, "Unsafe form"),
                    BrowserActionRequest(kind="commit", activation="click"),
                )
            assert rejected_form.value.failure.code == "destination_blocked"
            assert requests["/scheme-executed"] == 0
            assert requests["/submitted"] == 0

            scripted_popup = await service.action(
                _target(snapshot, "Open scripted data"),
                BrowserActionRequest(kind="click"),
            )
            if scripted_popup.disposition == "in_doubt":
                assert scripted_popup.failure is not None
                assert scripted_popup.failure.code == "destination_blocked"
            else:
                # Chromium currently refuses direct data: popups before creating
                # a page. The initiating click still completed, but no unsafe page
                # exists to select or inspect.
                assert scripted_popup.disposition == "performed"
            pages = await service.pages(session.session_id)
            assert len(pages.pages) == 1
            assert pages.pages[0].url == f"{origin}/actions"

            snapshot = await service.snapshot(session.session_id, page_id=None)
            scripted_blank = await service.action(
                _target(snapshot, "Open scripted blank"),
                BrowserActionRequest(kind="click"),
            )
            assert scripted_blank.disposition == "in_doubt"
            assert scripted_blank.failure is not None
            assert scripted_blank.failure.code == "destination_blocked"
            pages = await service.pages(session.session_id)
            assert len(pages.pages) == 1
            assert pages.pages[0].url == f"{origin}/actions"

            snapshot = await service.snapshot(session.session_id, page_id=None)
            scripted_blob = await service.action(
                _target(snapshot, "Open scripted blob"),
                BrowserActionRequest(kind="click"),
            )
            assert scripted_blob.disposition == "in_doubt"
            assert scripted_blob.failure is not None
            assert scripted_blob.failure.code == "destination_blocked"
            pages = await service.pages(session.session_id)
            assert len(pages.pages) == 1
            assert pages.pages[0].url == f"{origin}/actions"

            snapshot = await service.snapshot(session.session_id, page_id=None)
            navigated_blank = await service.action(
                _target(snapshot, "Navigate blank"),
                BrowserActionRequest(kind="click"),
            )
            assert navigated_blank.disposition == "in_doubt"
            assert navigated_blank.failure is not None
            assert navigated_blank.failure.code == "destination_blocked"
        finally:
            await service.aclose()


async def test_real_persistent_resource_reuses_profile_state_across_sessions(
    installed_browser: _InstalledBrowser,
    tmp_path: Path,
) -> None:
    with _fixture_server() as (origin, _requests):
        settings = RickySettings.model_validate(
            {
                "user_data_dir": str(tmp_path / "user"),
                "project_data_dir": str(tmp_path / "project"),
                "browser": {
                    "enabled": True,
                    "headless": True,
                    "allowed_private_origins": [origin],
                },
                "profile_configs": {
                    "personal": {
                        "browser": {
                            "resources": {
                                "main": {
                                    "kind": "persistent",
                                    "description": "Persistent integration browser",
                                    "headless": True,
                                }
                            }
                        }
                    },
                    "work": {
                        "browser": {
                            "resources": {
                                "main": {
                                    "kind": "persistent",
                                    "description": "Isolated work browser",
                                    "headless": True,
                                }
                            }
                        }
                    },
                },
            }
        )
        service = BrowserService(
            settings,
            scope=settings.resolve_profile_scope("personal", access_profiles=["work"]),
            backend=PlaywrightBrowserBackend(),
            executable_path=installed_browser.executable,
        )
        try:
            first = await service.open_resource("personal/main")
            await service.navigate(first.session_id, page_id=None, url=f"{origin}/persistent")
            snapshot = await service.snapshot(first.session_id, page_id=None)
            saved = await _semantic_transaction(
                service,
                _target(snapshot, "Save local marker"),
                BrowserActionRequest(kind="commit", activation="click"),
            )
            assert saved.disposition == "performed"
            await service.close_session(first.session_id)

            work = await service.open_resource("work/main")
            await service.navigate(work.session_id, page_id=None, url=f"{origin}/persistent")
            isolated = await service.snapshot(work.session_id, page_id=None)
            assert "missing" in isolated.content
            assert "remembered" not in isolated.content
            await service.close_session(work.session_id)

            second = await service.open_resource("personal/main")
            await service.navigate(second.session_id, page_id=None, url=f"{origin}/persistent")
            restored = await service.snapshot(second.session_id, page_id=None)
            assert "remembered" in restored.content
            await service.close_session(second.session_id)
        finally:
            await service.aclose()

        assert not Path(settings.project_data_dir).exists()


async def test_real_cdp_disconnect_leaves_external_browser_and_tab_alive(
    installed_browser: _InstalledBrowser,
    tmp_path: Path,
) -> None:
    with _fixture_server() as (origin, requests):
        port = _unused_loopback_port()
        endpoint = f"http://127.0.0.1:{port}"
        external_state = tmp_path / "external-chromium"
        process = await asyncio.create_subprocess_exec(
            str(installed_browser.executable),
            "--headless=new",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={external_state}",
            "about:blank",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await _wait_for_cdp(endpoint)
            settings = RickySettings.model_validate(
                {
                    "user_data_dir": str(tmp_path / "user"),
                    "project_data_dir": str(tmp_path / "project"),
                    "browser": {
                        "enabled": True,
                        "headless": True,
                        "allowed_private_origins": [origin],
                    },
                    "profile_configs": {
                        "personal": {
                            "browser": {
                                "resources": {
                                    "debug": {
                                        "kind": "cdp",
                                        "description": "External integration browser",
                                        "endpoint": endpoint,
                                    }
                                }
                            }
                        }
                    },
                }
            )
            service = BrowserService(
                settings,
                scope=settings.resolve_profile_scope(),
                backend=PlaywrightBrowserBackend(),
                executable_path=installed_browser.executable,
            )
            try:
                attached = await service.open_resource("personal/debug")
                assert attached.process_owned is False
                await service.navigate(
                    attached.session_id,
                    page_id=None,
                    url=f"{origin}/phase4",
                )
                snapshot = await service.snapshot(attached.session_id, page_id=None)
                download = await service.download(_target(snapshot, "Download phase four fixture"))
                assert download.disposition == "not_performed"
                assert download.failure is not None
                assert download.failure.code == "download_unavailable"
                assert requests["/phase4-download"] == 0
                await service.close_session(attached.session_id)
            finally:
                await service.aclose()

            assert process.returncode is None
            targets = await asyncio.to_thread(_read_cdp_targets, endpoint)
            assert any(item.get("url") == f"{origin}/phase4" for item in targets)
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()


async def test_real_cdp_unreachable_endpoint_fails_boundedly_and_releases_lease(
    installed_browser: _InstalledBrowser,
    tmp_path: Path,
) -> None:
    port = _unused_loopback_port()
    endpoint = f"http://127.0.0.1:{port}"
    settings = RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / "project"),
            "browser": {"enabled": True, "attachment_timeout_seconds": 1},
            "profile_configs": {
                "personal": {
                    "browser": {
                        "resources": {
                            "debug": {
                                "kind": "cdp",
                                "description": "Unavailable external browser",
                                "endpoint": endpoint,
                            }
                        }
                    }
                }
            },
        }
    )
    service = BrowserService(
        settings,
        scope=settings.resolve_profile_scope(),
        backend=PlaywrightBrowserBackend(),
        executable_path=installed_browser.executable,
    )
    try:
        for _attempt in range(2):
            with pytest.raises(BrowserError) as unavailable:
                await service.open_resource("personal/debug")
            assert unavailable.value.failure.code == "attachment_unavailable"
            assert endpoint not in unavailable.value.failure.message
            assert str(port) not in unavailable.value.failure.message
    finally:
        await service.aclose()


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def _wait_for_cdp(endpoint: str) -> None:
    deadline = asyncio.get_running_loop().time() + 10
    while True:
        try:
            await asyncio.to_thread(_read_cdp_targets, endpoint)
            return
        except OSError:
            if asyncio.get_running_loop().time() >= deadline:
                raise
            await asyncio.sleep(0.05)


def _read_cdp_targets(endpoint: str) -> list[dict[str, object]]:
    with urlopen(f"{endpoint}/json/list", timeout=1) as response:  # noqa: S310
        payload = json.load(response)
    assert isinstance(payload, list)
    return [item for item in payload if isinstance(item, dict)]


def _target(snapshot: BrowserSnapshot, name: str) -> BrowserActionTarget:
    descriptor = next(item for item in snapshot.descriptors if item.name == name)
    return BrowserActionTarget(
        session_id=snapshot.page.session_id,
        page_id=snapshot.page.page_id,
        snapshot_id=snapshot.snapshot_id,
        ref=descriptor.ref,
    )


async def _semantic_transaction(
    service: BrowserService,
    target: BrowserActionTarget,
    request: BrowserActionRequest,
):
    prepared = await service.prepare_commit(target, request)
    transaction = service.transaction_evidence(
        prepared,
        envelope_kind="browser",
        envelope_sha256="e" * 64,
    )
    return await service.commit_prepared(prepared, transaction)


async def _coordinate_transaction(
    service: BrowserService,
    target: BrowserCoordinateTarget,
    *,
    dialog: BrowserDialogPolicy | None = None,
):
    policy = dialog or BrowserDialogPolicy()
    prepared = await service.prepare_coordinate_commit(target, dialog=policy)
    transaction = service.transaction_evidence(
        prepared,
        envelope_kind="browser",
        envelope_sha256="f" * 64,
    )
    # A real fresh-review prompt naturally separates preparation and dispatch.
    # Let Chromium finish restoring its temporary screenshot masking before the
    # second pixel recapture that immediately precedes the synthetic click.
    await asyncio.sleep(0.05)
    return await service.coordinate_commit_prepared(prepared, transaction)


async def _act(
    service: BrowserService,
    snapshot: BrowserSnapshot,
    name: str,
    action: BrowserActionRequest,
) -> BrowserSnapshot:
    result = await service.action(_target(snapshot, name), action)
    assert result.disposition == "performed", result
    assert result.snapshot is not None
    return result.snapshot


async def _wait_for_snapshot_text(
    service: BrowserService,
    session_id: str,
    expected: str,
) -> BrowserSnapshot:
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        try:
            snapshot = await service.snapshot(session_id, page_id=None)
        except BrowserError as exc:
            # Navigation can finish during this read-only observation. Retry
            # only that race; never replay the submission that preceded it.
            if not (
                exc.failure.retryable
                and exc.failure.code == "backend_error"
                and exc.failure.message
                == "browser page navigated during snapshot; request a new snapshot"
            ):
                raise
            if asyncio.get_running_loop().time() >= deadline:
                raise
        else:
            if expected in snapshot.content:
                return snapshot
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"browser snapshot did not contain {expected!r}")
        await asyncio.sleep(0.05)


def _visual_candidate_point(
    visual: BrowserVisualCapture,
    candidate: BrowserVisualCandidate,
) -> tuple[int, int]:
    scale = visual.viewport.image_scale
    box = candidate.bounding_box
    x = int((box.x + box.width / 2) * scale)
    y = int((box.y + box.height / 2) * scale)
    return min(visual.width - 1, max(0, x)), min(visual.height - 1, max(0, y))
