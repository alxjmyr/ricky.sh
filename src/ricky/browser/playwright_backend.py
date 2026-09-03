"""Playwright implementation of Ricky's private browser backend."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit
from weakref import WeakSet

from playwright.async_api import (
    Browser,
    BrowserContext,
    Dialog,
    Download,
    FilePayload,
    Frame,
    Locator,
    Page,
    Playwright,
    Request,
    Route,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from ricky.browser.backend import (
    BackendActionOutcome,
    BackendActionPreflight,
    BackendActionRequest,
    BackendBoundingBox,
    BackendCoordinatePreflight,
    BackendCoordinateRequest,
    BackendDownload,
    BackendDownloadOutcome,
    BackendPageState,
    BackendProtectedFillRequest,
    BackendSnapshot,
    BackendTargetDescriptor,
    BackendUploadFile,
    BackendViewport,
    BackendVisualCandidate,
    BackendVisualSnapshot,
    BrowserCdpOptions,
    BrowserLaunchOptions,
    BrowserOpenOptions,
    BrowserPageHandle,
    BrowserSessionHandle,
    DestinationGuard,
)
from ricky.browser.policy import canonical_origin
from ricky.browser.types import (
    BrowserActionRequest,
    BrowserControlKind,
    BrowserDialogObservation,
    BrowserDialogPolicy,
    BrowserError,
    BrowserFailure,
)
from ricky.protected_values import ProtectedControlKind

_TARGET_LINE = re.compile(
    r"^(?P<indent>[ \t]*)-\s+(?P<role>[a-z][a-z0-9_-]*)"
    r'(?:\s+(?P<name>"(?:[^"\\]|\\.)*"))?'
    r".*?\[ref=(?P<ref>(?:f[0-9]+)?e[0-9]+)\]",
    re.MULTILINE,
)
_OPTION_LINE = re.compile(
    r'^(?P<indent>[ \t]*)-\s+option\s+(?P<name>"(?:[^"\\]|\\.)*")',
)
_CONSEQUENTIAL_WORDS = frozenset(
    {
        "buy",
        "confirm",
        "delete",
        "order",
        "pay",
        "place",
        "purchase",
        "reserve",
        "save",
        "send",
        "submit",
    }
)
_PROTECTED_TERMS = (
    "account number",
    "card",
    "credit",
    "credential",
    "cvc",
    "cvv",
    "debit",
    "expiration",
    "expiry",
    "one-time",
    "one time",
    "otp",
    "passcode",
    "password",
    "routing",
    "security code",
    "verification code",
)
_PROTECTED_AUTOCOMPLETE_TOKENS = frozenset(
    {
        "cc-additional-name",
        "cc-csc",
        "cc-exp",
        "cc-exp-month",
        "cc-exp-year",
        "cc-family-name",
        "cc-given-name",
        "cc-name",
        "cc-number",
        "cc-type",
        "current-password",
        "new-password",
        "one-time-code",
        "transaction-amount",
        "transaction-currency",
        "webauthn",
    }
)


@dataclass(frozen=True)
class _ParsedAriaTarget:
    ref: str
    role: str
    name: str
    option_labels: tuple[str, ...] = ()


class PlaywrightBrowserBackend:
    """Own one Playwright driver and the sessions opened through it."""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._sessions: list[_PlaywrightSession] = []

    async def open_session(
        self,
        options: BrowserOpenOptions,
        *,
        destination_guard: DestinationGuard,
    ) -> BrowserSessionHandle:
        if isinstance(options, BrowserLaunchOptions) and not options.executable_path.is_file():
            raise BrowserError(
                BrowserFailure(
                    code="not_installed",
                    message=(
                        "Playwright Chromium is not installed; run uv run ricky browser install"
                    ),
                )
            )
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        if isinstance(options, BrowserCdpOptions):
            return await self._open_cdp(options, destination_guard=destination_guard)
        return await self._open_owned(options, destination_guard=destination_guard)

    async def _open_owned(
        self,
        options: BrowserLaunchOptions,
        *,
        destination_guard: DestinationGuard,
    ) -> BrowserSessionHandle:
        assert self._playwright is not None
        try:
            context = await self._playwright.chromium.launch_persistent_context(
                str(options.user_data_dir),
                executable_path=str(options.executable_path),
                headless=options.headless,
                accept_downloads=True,
                downloads_path=str(options.download_temp_dir),
                service_workers="block",
            )
        except PlaywrightError as exc:
            raise _backend_error("Chromium could not be started", exc) from exc
        try:
            if options.start_blank:
                await _replace_owned_pages_with_blank(context)
            context.set_default_navigation_timeout(options.navigation_timeout_ms)
            context.set_default_timeout(options.operation_timeout_ms)
            session = _PlaywrightSession(
                (context,),
                process_owned=True,
                pages_owned=True,
                close_owner=context.close,
                is_connected=lambda: True,
                destination_guard=destination_guard,
                max_redirects=options.max_redirects,
                navigation_timeout_ms=options.navigation_timeout_ms,
                operation_timeout_ms=options.operation_timeout_ms,
                page_discovery_limit=options.page_discovery_limit,
                download_temp_dir=options.download_temp_dir,
                download_file_byte_limit=options.download_file_byte_limit,
            )
            await session.initialize()
        except BaseException:
            with suppress(PlaywrightError):
                await context.close()
            raise
        self._sessions.append(session)
        return session

    async def _open_cdp(
        self,
        options: BrowserCdpOptions,
        *,
        destination_guard: DestinationGuard,
    ) -> BrowserSessionHandle:
        assert self._playwright is not None
        browser: Browser | None = None
        try:
            async with asyncio.timeout(options.attachment_timeout_ms / 1_000):
                browser = await self._playwright.chromium.connect_over_cdp(
                    options.endpoint,
                    timeout=options.attachment_timeout_ms,
                )
                contexts = tuple(browser.contexts)
                if not contexts:
                    raise BrowserError(
                        BrowserFailure(
                            code="attachment_unavailable",
                            message="configured browser attachment has no controllable context",
                        )
                    )
                for context in contexts:
                    context.set_default_navigation_timeout(options.navigation_timeout_ms)
                    context.set_default_timeout(options.operation_timeout_ms)
                session = _PlaywrightSession(
                    contexts,
                    process_owned=False,
                    pages_owned=False,
                    close_owner=lambda: _disconnect_browser(browser),
                    is_connected=lambda: browser.is_connected(),
                    destination_guard=destination_guard,
                    max_redirects=options.max_redirects,
                    navigation_timeout_ms=options.navigation_timeout_ms,
                    operation_timeout_ms=options.operation_timeout_ms,
                    page_discovery_limit=options.page_discovery_limit,
                    download_temp_dir=None,
                    download_file_byte_limit=None,
                )
                await session.initialize()
        except PlaywrightTimeoutError as exc:
            if browser is not None:
                await _best_effort_disconnect(browser)
            raise BrowserError(
                BrowserFailure(
                    code="attachment_timeout",
                    message="configured browser attachment timed out",
                    retryable=True,
                )
            ) from exc
        except TimeoutError as exc:
            if browser is not None:
                await _best_effort_disconnect(browser)
            raise BrowserError(
                BrowserFailure(
                    code="attachment_timeout",
                    message="configured browser attachment timed out",
                    retryable=True,
                )
            ) from exc
        except PlaywrightError as exc:
            if browser is not None:
                await _best_effort_disconnect(browser)
            raise BrowserError(
                BrowserFailure(
                    code="attachment_unavailable",
                    message="configured browser attachment is unavailable",
                    retryable=True,
                )
            ) from exc
        except BaseException:
            if browser is not None:
                await _best_effort_disconnect(browser)
            raise
        assert browser is not None
        self._sessions.append(session)
        return session

    async def aclose(self) -> None:
        failure: BaseException | None = None
        remaining: list[_PlaywrightSession] = []
        for session in reversed(self._sessions):
            try:
                await session.close()
            except BaseException as exc:
                failure = failure or exc
                remaining.append(session)
        self._sessions = list(reversed(remaining))
        if failure is not None:
            raise failure
        if self._playwright is not None:
            playwright = self._playwright
            await playwright.stop()
            self._playwright = None


class _PlaywrightSession:
    def __init__(
        self,
        contexts: tuple[BrowserContext, ...],
        *,
        process_owned: bool,
        pages_owned: bool,
        close_owner: Callable[[], Awaitable[None]],
        is_connected: Callable[[], bool],
        destination_guard: DestinationGuard,
        max_redirects: int,
        navigation_timeout_ms: float,
        operation_timeout_ms: float,
        page_discovery_limit: int,
        download_temp_dir: Path | None = None,
        download_file_byte_limit: int | None = None,
    ) -> None:
        self._contexts = contexts
        self.process_owned = process_owned
        self.pages_owned = pages_owned
        self._close_owner = close_owner
        self._is_connected = is_connected
        self._destination_guard = destination_guard
        self._max_redirects = max_redirects
        self._navigation_timeout_ms = navigation_timeout_ms
        self._operation_timeout_seconds = operation_timeout_ms / 1_000
        self._page_discovery_limit = page_discovery_limit
        self._download_temp_dir = download_temp_dir
        self._download_file_byte_limit = download_file_byte_limit
        self._pages: dict[Page, _PlaywrightPage] = {}
        self._quarantined: WeakSet[Page] = WeakSet()
        self._page_overflow_count = 0
        self._discovery_offsets: dict[BrowserContext, int] = {}
        self._discovery_context_cursor = 0
        self._background_page_closes: set[asyncio.Task[None]] = set()
        self._background_download_cancels: set[asyncio.Task[None]] = set()
        self._explicit_download_pages: set[Page] = set()
        self._explicit_downloads: dict[Page, list[Download]] = {}
        self._blocked: dict[Page, BrowserError] = {}
        self._unattributed_blocked: BrowserError | None = None
        self._active_actions: set[Page] = set()
        self._action_destinations: dict[Page, tuple[str, ...]] = {}
        self._closed = False

    @property
    def connected(self) -> bool:
        return not self._closed and self._is_connected()

    async def initialize(self) -> None:
        remaining = self._page_discovery_limit
        for context in self._contexts:
            await context.route("**/*", self._route)
            context.on("page", self._register_page)
            pages = context.pages
            inspected = min(len(pages), remaining)
            for page in pages[:inspected]:
                self._register_page(page, report_overflow=False)
            self._discovery_offsets[context] = inspected
            self._page_overflow_count += len(pages) - inspected
            remaining -= inspected
        if not self._pages and self.pages_owned:
            self._register_page(await self._contexts[0].new_page())

    def _register_page(self, page: Page, *, report_overflow: bool = True) -> None:
        if page in self._pages or page in self._quarantined:
            return
        if len(self._pages) >= self._page_discovery_limit:
            if report_overflow:
                self._page_overflow_count += 1
            if self.pages_owned and not page.is_closed():
                task = asyncio.create_task(_close_page(page))
                self._background_page_closes.add(task)
                task.add_done_callback(self._background_page_closes.discard)
            return
        self._pages[page] = _PlaywrightPage(page, self)
        page.on("download", lambda download: self._observe_download(page, download))

    def _observe_download(self, page: Page, download: Download) -> None:
        if page in self._explicit_download_pages:
            self._explicit_downloads.setdefault(page, []).append(download)
            return
        task = asyncio.create_task(_cancel_download(download))
        self._background_download_cancels.add(task)
        task.add_done_callback(self._background_download_cancels.discard)

    def begin_download(self, page: Page) -> None:
        if self._download_temp_dir is None:
            raise BrowserError(
                BrowserFailure(
                    code="download_unavailable",
                    message="downloads are unavailable for attached browser sessions",
                )
            )
        self._explicit_download_pages.add(page)
        self._explicit_downloads[page] = []

    def end_download(self, page: Page) -> tuple[Download, ...]:
        self._explicit_download_pages.discard(page)
        return tuple(self._explicit_downloads.pop(page, []))

    async def _route(self, route: Route, request: Request) -> None:
        if not request.is_navigation_request():
            await route.continue_()
            return
        page: Page | None = None
        try:
            frame = request.frame
            page = frame.page
            if frame != page.main_frame:
                await route.continue_()
                return
        except PlaywrightError:
            # A popup's initial navigation can arrive before Chromium exposes its
            # Frame. It is still a top-level navigation and must pass the same
            # destination, redirect, and download checks.
            pass
        if page is not None and not self.controls(page):
            await route.continue_()
            return
        if page is None and not self._active_actions:
            await route.continue_()
            return
        try:
            redirected = request.redirected_from
            redirect_count = 0
            while redirected is not None:
                redirect_count += 1
                redirected = redirected.redirected_from
            if redirect_count > self._max_redirects:
                raise BrowserError(
                    BrowserFailure(
                        code="destination_blocked",
                        message="navigation exceeded the redirect limit",
                    )
                )
            if redirected is None:
                reviewed = await self._reviewed_action_destinations(page)
                if reviewed and not any(
                    _matches_reviewed_destination(request.url, destination)
                    for destination in reviewed
                ):
                    raise BrowserError(
                        BrowserFailure(
                            code="destination_blocked",
                            message=(
                                "browser action navigation differed from the reviewed destination"
                            ),
                            outcome_uncertain=True,
                        )
                    )
            await self._destination_guard(request.url)
            response = await route.fetch(
                max_redirects=0,
                max_retries=0,
                timeout=self._navigation_timeout_ms,
            )
            try:
                disposition = response.headers.get("content-disposition", "").lower()
                explicit_download = (
                    page in self._explicit_download_pages
                    if page is not None
                    else (bool(self._explicit_download_pages))
                )
                if disposition.startswith("attachment") and not explicit_download:
                    raise BrowserError(
                        BrowserFailure(code="download_blocked", message="downloads are disabled")
                    )
                content_length = response.headers.get("content-length")
                if (
                    explicit_download
                    and disposition.startswith("attachment")
                    and self._download_file_byte_limit is not None
                    and content_length is not None
                    and content_length.isascii()
                    and content_length.isdigit()
                    and int(content_length) > self._download_file_byte_limit
                ):
                    raise BrowserError(
                        BrowserFailure(
                            code="download_too_large",
                            message="browser download exceeds the configured byte limit",
                        )
                    )
                location = response.headers.get("location")
                if 300 <= response.status < 400 and location:
                    await self._destination_guard(urljoin(request.url, location))
                await route.fulfill(response=response)
            finally:
                with suppress(PlaywrightError):
                    await response.dispose()
        except BrowserError as exc:
            self._record_blocked(page, exc)
            await route.abort("blockedbyclient")
            return
        except PlaywrightTimeoutError:
            self._record_blocked(
                page,
                BrowserError(
                    BrowserFailure(
                        code="navigation_timeout",
                        message="browser navigation timed out; it was not retried",
                        retryable=True,
                        outcome_uncertain=True,
                    )
                ),
            )
            await route.abort("timedout")
            return
        except PlaywrightError as exc:
            self._record_blocked(
                page,
                _backend_error(
                    "browser navigation failed; it was not retried",
                    exc,
                ),
            )
            await route.abort("failed")

    def take_blocked_error(self, page: Page) -> BrowserError | None:
        blocked = self._blocked.pop(page, None)
        if blocked is not None:
            return blocked
        blocked, self._unattributed_blocked = self._unattributed_blocked, None
        return blocked

    def _record_blocked(self, page: Page | None, error: BrowserError) -> None:
        if page is None:
            if self._active_actions:
                for active_page in self._active_actions:
                    self._blocked[active_page] = error
            else:
                self._unattributed_blocked = error
        else:
            self._blocked[page] = error

    async def _reviewed_action_destinations(
        self,
        page: Page | None,
    ) -> tuple[str, ...]:
        if page is not None and page in self._action_destinations:
            return self._action_destinations[page]
        if page is not None:
            with suppress(PlaywrightError):
                opener = await page.opener()
                if opener in self._action_destinations:
                    assert opener is not None
                    return self._action_destinations[opener]
        if page is None and len(self._active_actions) == 1:
            active = next(iter(self._active_actions))
            return self._action_destinations.get(active, ())
        return ()

    def begin_action(
        self,
        page: Page,
        *,
        reviewed_destinations: tuple[str, ...] = (),
    ) -> None:
        self._active_actions.add(page)
        self._action_destinations[page] = reviewed_destinations

    def end_action(self, page: Page) -> None:
        self._active_actions.discard(page)
        self._action_destinations.pop(page, None)

    async def pages(self) -> tuple[BrowserPageHandle, ...]:
        self.ensure_connected()
        for page in tuple(self._pages):
            if page.is_closed() or page in self._quarantined:
                self._pages.pop(page, None)

        remaining = self._page_discovery_limit
        context_count = len(self._contexts)
        for offset in range(context_count):
            if remaining <= 0:
                break
            index = (self._discovery_context_cursor + offset) % context_count
            context = self._contexts[index]
            pages = context.pages
            if not pages:
                self._discovery_offsets[context] = 0
                continue
            start = self._discovery_offsets.get(context, 0) % len(pages)
            inspected = min(len(pages), remaining)
            for step in range(inspected):
                page = pages[(start + step) % len(pages)]
                self._register_page(page, report_overflow=False)
            self._discovery_offsets[context] = (start + inspected) % len(pages)
            remaining -= inspected
        if context_count:
            self._discovery_context_cursor = (self._discovery_context_cursor + 1) % context_count
        return tuple(self._pages.values())

    def take_page_overflow_count(self) -> int:
        count, self._page_overflow_count = self._page_overflow_count, 0
        return count

    def quarantine(self, page: Page) -> None:
        self._quarantined.add(page)
        self._pages.pop(page, None)

    def is_quarantined(self, page: Page) -> bool:
        return page in self._quarantined

    def controls(self, page: Page) -> bool:
        return page in self._pages and page not in self._quarantined

    def ensure_connected(self) -> None:
        if self._closed:
            raise BrowserError(
                BrowserFailure(code="session_closed", message="browser session is closed")
            )
        if not self.connected:
            raise BrowserError(
                BrowserFailure(
                    code="attachment_disconnected",
                    message="configured browser attachment disconnected",
                )
            )

    async def close(self) -> None:
        if self._closed:
            return
        if self._background_page_closes:
            await asyncio.gather(*tuple(self._background_page_closes), return_exceptions=True)
        if self._background_download_cancels:
            await asyncio.gather(*tuple(self._background_download_cancels), return_exceptions=True)
        await self._close_owner()
        self._closed = True


class _PlaywrightPage:
    def __init__(self, page: Page, session: _PlaywrightSession) -> None:
        self._page = page
        self._session = session
        self._key = f"playwright_page_{id(page)}"
        self._dom_targets: dict[str, tuple[Frame, Locator]] = {}
        self._semantic_targets: tuple[BackendTargetDescriptor, ...] = ()

    @property
    def key(self) -> str:
        return self._key

    async def state(self) -> BackendPageState:
        self._session.ensure_connected()
        if self._page.is_closed() or self._session.is_quarantined(self._page):
            return BackendPageState(key=self.key, url="[closed]", title="", closed=True)
        try:
            title = await self._page.title()
        except PlaywrightError:
            title = ""
        return BackendPageState(key=self.key, url=self._page.url, title=title[:1_000])

    async def bring_to_front(self) -> None:
        self._ensure_open()
        try:
            await self._page.bring_to_front()
        except PlaywrightError as exc:
            raise _backend_error("browser page could not be selected", exc) from exc

    async def navigate(self, url: str) -> BackendPageState:
        self._ensure_open()
        self._session.take_blocked_error(self._page)
        try:
            response = await self._page.goto(url, wait_until="domcontentloaded")
        except PlaywrightTimeoutError as exc:
            raise BrowserError(
                BrowserFailure(
                    code="navigation_timeout",
                    message="browser navigation timed out; it was not retried",
                    retryable=True,
                    outcome_uncertain=True,
                )
            ) from exc
        except PlaywrightError as exc:
            if blocked := self._session.take_blocked_error(self._page):
                raise blocked from exc
            if "Download is starting" in str(exc):
                raise BrowserError(
                    BrowserFailure(code="download_blocked", message="downloads are disabled")
                ) from exc
            raise _backend_error("browser navigation failed; it was not retried", exc) from exc
        if blocked := self._session.take_blocked_error(self._page):
            raise blocked
        if response is not None:
            disposition = (await response.header_value("content-disposition") or "").lower()
            if disposition.startswith("attachment"):
                raise BrowserError(
                    BrowserFailure(code="download_blocked", message="downloads are disabled")
                )
        await self._session._destination_guard(self._page.url)
        return await self.state()

    async def scroll(self, delta_y: int) -> BackendPageState:
        self._ensure_open()
        try:
            await self._page.mouse.wheel(0, delta_y)
        except PlaywrightTimeoutError as exc:
            raise BrowserError(
                BrowserFailure(
                    code="operation_timeout",
                    message="browser scroll timed out; it was not retried",
                    retryable=True,
                    outcome_uncertain=True,
                )
            ) from exc
        except PlaywrightError as exc:
            raise _backend_error("browser page could not be scrolled", exc) from exc
        return await self.state()

    async def snapshot(self, *, depth: int, character_limit: int) -> BackendSnapshot:
        self._ensure_open()
        if character_limit <= 0:
            raise ValueError("browser snapshot character limit must be positive")
        try:
            async with asyncio.timeout(self._session._operation_timeout_seconds):
                raw_content = await self._page.aria_snapshot(depth=depth, mode="ai")
                content, character_truncated = _bound_aria_content(
                    raw_content,
                    character_limit=character_limit,
                )
                del raw_content
                targets = await self._snapshot_targets(content)
                return BackendSnapshot(
                    content=content,
                    targets=targets,
                    character_truncated=character_truncated,
                )
        except (TimeoutError, PlaywrightTimeoutError) as exc:
            raise BrowserError(
                BrowserFailure(
                    code="operation_timeout",
                    message="semantic browser snapshot timed out",
                    retryable=True,
                )
            ) from exc
        except PlaywrightError as exc:
            raise _backend_error("semantic browser snapshot failed", exc) from exc

    async def visual_snapshot(self, *, candidate_limit: int) -> BackendVisualSnapshot:
        """Capture one masked viewport and bounded DOM-derived interactive candidates."""
        self._ensure_open()
        if candidate_limit < 1:
            raise ValueError("visual candidate limit must be positive")
        try:
            async with asyncio.timeout(self._session._operation_timeout_seconds):
                viewport, png = await self._masked_viewport_png()
                candidates: list[BackendVisualCandidate] = []
                dom_targets: dict[str, tuple[Frame, Locator]] = {}
                truncated = False
                selector = (
                    "a,button,input,select,textarea,[role],"
                    "[contenteditable]:not([contenteditable='false']),[tabindex],canvas"
                )
                for frame in self._page.frames:
                    locator = frame.locator(selector)
                    count = await locator.count()
                    for index in range(count):
                        candidate = locator.nth(index)
                        if not await candidate.is_visible():
                            continue
                        box = await candidate.bounding_box()
                        if not isinstance(box, dict):
                            continue
                        x = float(box.get("x", -1))
                        y = float(box.get("y", -1))
                        width = float(box.get("width", 0))
                        height = float(box.get("height", 0))
                        if (
                            x + width <= 0
                            or y + height <= 0
                            or x >= viewport.width
                            or y >= viewport.height
                            or width <= 0
                            or height <= 0
                        ):
                            continue
                        if len(candidates) >= candidate_limit:
                            truncated = True
                            break
                        facts = await candidate.evaluate(
                            """element => ({
                                tag: String(element.tagName || '').toLowerCase(),
                                role: String(element.getAttribute('role') || ''),
                                aria: String(element.getAttribute('aria-label') || ''),
                                alt: String(element.getAttribute('alt') || ''),
                                title: String(element.getAttribute('title') || ''),
                                placeholder: String(element.getAttribute('placeholder') || ''),
                                text: String(element.innerText || element.textContent || ''),
                            })"""
                        )
                        if not isinstance(facts, dict):
                            continue
                        ref = f"d{len(candidates) + 1}"
                        role = _dom_role(facts)
                        name = _dom_name(facts)
                        descriptor = await self._describe_target(
                            candidate,
                            frame=frame,
                            ref=ref,
                            role=role,
                            name=name,
                        )
                        dom_targets[ref] = (frame, candidate)
                        candidates.append(
                            BackendVisualCandidate(
                                descriptor=descriptor,
                                bounding_box=BackendBoundingBox(
                                    x=max(0.0, x),
                                    y=max(0.0, y),
                                    width=min(width, max(0.0, viewport.width - max(0.0, x))),
                                    height=min(
                                        height,
                                        max(0.0, viewport.height - max(0.0, y)),
                                    ),
                                ),
                            )
                        )
                    if truncated:
                        break
                verified_viewport, verified_png = await self._masked_viewport_png()
                if verified_viewport != viewport or hashlib.sha256(verified_png).digest() != (
                    hashlib.sha256(png).digest()
                ):
                    raise BrowserError(
                        BrowserFailure(
                            code="stale_target",
                            message=(
                                "browser viewport changed during visual capture; request another"
                            ),
                            retryable=True,
                        )
                    )
                self._dom_targets = dom_targets
                return BackendVisualSnapshot(
                    png=png,
                    masked_base_sha256=hashlib.sha256(png).hexdigest(),
                    viewport=viewport,
                    candidates=tuple(candidates),
                    candidate_truncated=truncated,
                )
        except (TimeoutError, PlaywrightTimeoutError) as exc:
            raise BrowserError(
                BrowserFailure(
                    code="operation_timeout",
                    message="visual browser snapshot timed out",
                    retryable=True,
                )
            ) from exc
        except PlaywrightError as exc:
            raise _backend_error("visual browser snapshot failed", exc) from exc

    async def _masked_viewport_png(self) -> tuple[BackendViewport, bytes]:
        metrics = await self._page.evaluate(
            """() => ({
                width: window.innerWidth,
                height: window.innerHeight,
                scrollX: window.scrollX,
                scrollY: window.scrollY,
                deviceScaleFactor: window.devicePixelRatio,
            })"""
        )
        if not isinstance(metrics, dict):
            raise BrowserError(
                BrowserFailure(
                    code="visual_capture_failed",
                    message="browser viewport metrics were unavailable",
                )
            )
        viewport = BackendViewport(
            width=int(metrics.get("width") or 0),
            height=int(metrics.get("height") or 0),
            scroll_x=float(metrics.get("scrollX") or 0),
            scroll_y=float(metrics.get("scrollY") or 0),
            device_scale_factor=float(metrics.get("deviceScaleFactor") or 1),
        )
        if viewport.width < 1 or viewport.height < 1:
            raise BrowserError(
                BrowserFailure(
                    code="visual_capture_failed",
                    message="browser viewport dimensions were invalid",
                )
            )
        masks = [
            frame.locator("input,textarea,select,[contenteditable]:not([contenteditable='false'])")
            for frame in self._page.frames
        ]
        raw = await self._page.screenshot(
            type="png",
            full_page=False,
            scale="css",
            caret="hide",
            mask=masks,
            mask_color="#4b0082",
        )
        return viewport, bytes(raw)

    async def preflight_action(
        self,
        request: BackendActionRequest,
    ) -> BackendActionPreflight:
        """Return exact current target and destination facts without dispatching."""

        _locator, preflight = await self._preflight_semantic_action(request)
        return preflight

    async def _preflight_semantic_action(
        self,
        request: BackendActionRequest,
    ) -> tuple[Locator, BackendActionPreflight]:
        self._ensure_open()
        resolved = await self._resolve_target(request.target.ref)
        if resolved is None:
            raise BrowserError(
                BrowserFailure(
                    code="invented_target",
                    message="browser target no longer resolves on this page",
                )
            )
        frame, locator = resolved
        live = await self._describe_target(
            locator,
            frame=frame,
            ref=request.target.ref,
            role=request.target.role,
            name=request.target.name,
        )
        if not _same_target(request.target, live):
            raise BrowserError(
                BrowserFailure(
                    code="stale_target",
                    message="browser target changed after its snapshot was captured",
                )
            )
        try:
            visible = await locator.is_visible()
        except PlaywrightError as exc:
            raise _backend_error("browser target could not be inspected", exc) from exc
        if not visible or live.disabled:
            raise BrowserError(
                BrowserFailure(
                    code="incompatible_target",
                    message="browser target is not currently actionable",
                )
            )
        _validate_action_compatibility(request, live)
        destinations, financial_signal = await self._validate_effective_destinations(
            locator,
            request,
        )
        return locator, BackendActionPreflight(
            target=live,
            effective_destinations=destinations,
            financial_signal=financial_signal,
        )

    async def preflight_protected_target(
        self,
        target: BackendTargetDescriptor,
    ) -> BackendTargetDescriptor:
        """Revalidate one recognized protected editable control without material."""
        _locator, live = await self._resolve_protected_target(target)
        return live

    async def perform_protected_fill(
        self,
        request: BackendProtectedFillRequest,
    ) -> BackendActionOutcome:
        """Fill one exact protected control through the in-process-only request."""
        state_before = await self.state()
        try:
            locator, live = await self._resolve_protected_target(request.target)
            safe_action = BrowserActionRequest(kind="fill", value="")
            safe_request = BackendActionRequest(
                action_id=request.action_id,
                action=safe_action,
                target=live,
            )
            await self._validate_effective_destinations(locator, safe_request)
        except BrowserError as exc:
            return _not_performed_outcome(state_before, exc.failure)

        async def fill() -> None:
            await locator.fill(request.value.get_secret_value())

        return await self._perform_raw_effect(state_before, safe_action, fill)

    async def perform_action(
        self,
        request: BackendActionRequest,
    ) -> BackendActionOutcome:
        self._ensure_open()
        state_before = await self.state()
        try:
            locator, preflight = await self._preflight_semantic_action(request)
        except BrowserError as exc:
            return _not_performed_outcome(state_before, exc.failure)
        except PlaywrightError:
            return _not_performed_outcome(
                state_before,
                BrowserFailure(
                    code="backend_error",
                    message="browser target could not be inspected before dispatch",
                ),
            )
        if request.expected_preflight is not None and (preflight != request.expected_preflight):
            return _not_performed_outcome(
                state_before,
                BrowserFailure(
                    code="stale_target",
                    message="browser target or destination changed before action dispatch",
                ),
            )
        navigation_occurred = False
        dialogs: list[BrowserDialogObservation] = []
        popup_observed = asyncio.Event()
        popup_pages: list[Page] = []

        def observe_navigation(frame: Frame) -> None:
            nonlocal navigation_occurred
            if frame == self._page.main_frame:
                navigation_occurred = True

        async def handle_dialog(dialog: Dialog) -> None:
            policy = request.action.dialog
            matched = policy.prompt_text is None or dialog.type == "prompt"
            response = "unhandled"
            try:
                if policy.response == "accept" and matched:
                    await dialog.accept(policy.prompt_text)
                    response = "accepted"
                else:
                    await dialog.dismiss()
                    response = "dismissed"
            except PlaywrightError:
                response = "unhandled"
                matched = False
            dialogs.append(
                BrowserDialogObservation(
                    kind=_dialog_kind(dialog.type),
                    message=dialog.message[:2_000],
                    response=response,
                    matched_policy=matched,
                )
            )

        def observe_popup(page: Page) -> None:
            self._session._register_page(page)
            popup_pages.append(page)
            popup_observed.set()

        self._page.on("framenavigated", observe_navigation)
        self._page.on("dialog", handle_dialog)
        self._page.on("popup", observe_popup)
        self._session.take_blocked_error(self._page)
        self._session.begin_action(
            self._page,
            reviewed_destinations=preflight.effective_destinations,
        )
        try:
            await _dispatch_action(locator, request)
            if request.target.control_kind == "link" and not popup_observed.is_set():
                with suppress(TimeoutError):
                    await asyncio.wait_for(popup_observed.wait(), timeout=0.25)
            for popup in popup_pages:
                with suppress(PlaywrightError):
                    await popup.wait_for_load_state("domcontentloaded", timeout=250)
            if navigation_occurred and not self._page.is_closed():
                await self._page.wait_for_load_state("domcontentloaded")
            if destination_failure := await self._postdispatch_destination_failure(
                popup_pages,
                allow_opener_blank=state_before.url == "about:blank",
            ):
                return await self._uncertain_outcome(
                    state_before,
                    navigation_occurred=navigation_occurred,
                    dialogs=dialogs,
                    failure=destination_failure,
                )
        except asyncio.CancelledError:
            raise
        except PlaywrightTimeoutError:
            return await self._uncertain_outcome(
                state_before,
                navigation_occurred=navigation_occurred,
                dialogs=dialogs,
                failure=BrowserFailure(
                    code="operation_timeout",
                    message="browser action timed out after dispatch; it was not retried",
                    retryable=True,
                    outcome_uncertain=True,
                ),
            )
        except PlaywrightError as exc:
            blocked = self._session.take_blocked_error(self._page)
            if blocked is not None:
                failure = blocked.failure.model_copy(update={"outcome_uncertain": True})
            elif self._page.is_closed():
                failure = BrowserFailure(
                    code="page_closed",
                    message="browser page closed during action dispatch",
                    outcome_uncertain=True,
                )
            else:
                failure = _backend_error(
                    "browser action failed after dispatch; it was not retried",
                    exc,
                ).failure.model_copy(update={"outcome_uncertain": True})
            return await self._uncertain_outcome(
                state_before,
                navigation_occurred=navigation_occurred,
                dialogs=dialogs,
                failure=failure,
            )
        finally:
            self._session.end_action(self._page)
            self._page.remove_listener("framenavigated", observe_navigation)
            self._page.remove_listener("dialog", handle_dialog)
            self._page.remove_listener("popup", observe_popup)

        if blocked := self._session.take_blocked_error(self._page):
            return await self._uncertain_outcome(
                state_before,
                navigation_occurred=navigation_occurred,
                dialogs=dialogs,
                failure=blocked.failure.model_copy(update={"outcome_uncertain": True}),
            )
        state_after = await self.state()
        return BackendActionOutcome(
            disposition="performed",
            dispatch_state="completed",
            state_before=state_before,
            state_after=state_after,
            navigation_occurred=navigation_occurred,
            dialogs=tuple(dialogs),
        )

    async def perform_upload(
        self,
        request: BackendActionRequest,
        files: tuple[BackendUploadFile, ...],
    ) -> BackendActionOutcome:
        """Set exact in-memory files on one current file control exactly once."""
        state_before = await self.state()
        try:
            locator, live = await self._preflight_custom_target(request)
        except BrowserError as exc:
            return _not_performed_outcome(state_before, exc.failure)
        if not live.file:
            return _not_performed_outcome(
                state_before,
                BrowserFailure(
                    code="incompatible_target",
                    message="browser upload target is not a file control",
                ),
            )
        if len(files) > 1 and not live.multiple:
            return _not_performed_outcome(
                state_before,
                BrowserFailure(
                    code="incompatible_target",
                    message="browser file control does not accept multiple files",
                ),
            )

        async def upload() -> None:
            payloads: list[FilePayload] = [
                {
                    "name": item.filename,
                    "mimeType": item.media_type,
                    "buffer": item.content,
                }
                for item in files
            ]
            await locator.set_input_files(payloads)

        return await self._perform_raw_effect(state_before, request.action, upload)

    async def perform_download(
        self,
        request: BackendActionRequest,
    ) -> BackendDownloadOutcome:
        """Capture exactly one explicit download into the owned attempt directory."""
        state_before = await self.state()
        try:
            locator, live = await self._preflight_custom_target(request)
            if live.file or live.protected:
                raise BrowserError(
                    BrowserFailure(
                        code="incompatible_target",
                        message="browser download target is not safe to activate",
                    )
                )
            await self._validate_effective_destinations(locator, request)
            self._session.begin_download(self._page)
        except BrowserError as exc:
            return BackendDownloadOutcome(action=_not_performed_outcome(state_before, exc.failure))

        captured: list[Download] = []

        async def download() -> None:
            async with self._page.expect_download() as pending:
                await locator.click()
            captured.append(await pending.value)

        try:
            action = await self._perform_raw_effect(state_before, request.action, download)
        finally:
            observed = self._session.end_download(self._page)
        unique = tuple(dict.fromkeys((*captured, *observed)))
        if action.disposition != "performed":
            for item in unique:
                await _cancel_download(item)
            return BackendDownloadOutcome(action=action)
        if len(unique) != 1 or self._session._download_temp_dir is None:
            for item in unique:
                await _cancel_download(item)
            return BackendDownloadOutcome(
                action=BackendActionOutcome(
                    disposition="in_doubt",
                    dispatch_state="dispatched",
                    state_before=action.state_before,
                    state_after=action.state_after,
                    navigation_occurred=action.navigation_occurred,
                    dialogs=action.dialogs,
                    failure=BrowserFailure(
                        code="download_unavailable",
                        message="browser action did not produce exactly one retained download",
                        outcome_uncertain=True,
                    ),
                )
            )
        item = unique[0]
        failure = await item.failure()
        if failure is not None:
            await _cancel_download(item)
            return BackendDownloadOutcome(
                action=BackendActionOutcome(
                    disposition="in_doubt",
                    dispatch_state="dispatched",
                    state_before=action.state_before,
                    state_after=action.state_after,
                    navigation_occurred=action.navigation_occurred,
                    dialogs=action.dialogs,
                    failure=BrowserFailure(
                        code="download_unavailable",
                        message="browser download failed after dispatch",
                        outcome_uncertain=True,
                    ),
                )
            )
        temporary = self._session._download_temp_dir / f"attempt-{uuid.uuid4().hex}.download"
        try:
            await item.save_as(temporary)
            await item.delete()
        except PlaywrightError:
            await _cancel_download(item)
            return BackendDownloadOutcome(
                action=BackendActionOutcome(
                    disposition="in_doubt",
                    dispatch_state="dispatched",
                    state_before=action.state_before,
                    state_after=action.state_after,
                    navigation_occurred=action.navigation_occurred,
                    dialogs=action.dialogs,
                    failure=BrowserFailure(
                        code="download_publish_failed",
                        message="browser download could not be copied from temporary storage",
                        outcome_uncertain=True,
                    ),
                )
            )
        return BackendDownloadOutcome(
            action=action,
            download=BackendDownload(
                temporary_path=temporary,
                suggested_filename=item.suggested_filename,
            ),
        )

    async def preflight_coordinate_commit(
        self,
        request: BackendCoordinateRequest,
    ) -> BackendCoordinatePreflight:
        """Recapture and inspect one coordinate without dispatching a click."""

        self._ensure_open()
        try:
            viewport, png = await self._masked_viewport_png()
        except PlaywrightError as exc:
            raise BrowserError(
                BrowserFailure(
                    code="backend_error",
                    message="browser coordinate pixels could not be safely inspected",
                )
            ) from exc
        if viewport != request.viewport or hashlib.sha256(png).hexdigest() != (
            request.masked_base_sha256
        ):
            raise BrowserError(
                BrowserFailure(
                    code="stale_target",
                    message="browser viewport pixels changed; request a new visual snapshot",
                )
            )
        if not (0 <= request.x < viewport.width and 0 <= request.y < viewport.height):
            raise BrowserError(
                BrowserFailure(
                    code="coordinate_out_of_bounds",
                    message="browser coordinate is outside the current viewport",
                )
            )
        try:
            facts = await self._coordinate_target_facts(request.x, request.y)
        except PlaywrightError as exc:
            raise BrowserError(
                BrowserFailure(
                    code="backend_error",
                    message="browser coordinate target could not be safely inspected",
                )
            ) from exc
        if not isinstance(facts, dict):
            raise BrowserError(
                BrowserFailure(
                    code="incompatible_target",
                    message="browser coordinate does not hit a current element",
                )
            )
        target = _coordinate_target_descriptor(facts)
        if target.file:
            raise BrowserError(
                BrowserFailure(
                    code="file_control",
                    message="coordinate clicks cannot activate file controls",
                )
            )
        if target.protected:
            raise BrowserError(
                BrowserFailure(
                    code="protected_field",
                    message="coordinate clicks cannot activate recognized protected controls",
                )
            )
        if target.restricted_interaction is not None:
            raise BrowserError(
                BrowserFailure(
                    code="handoff_required",
                    message=(
                        "recognized CAPTCHA, passkey, or SSO interactions require user handoff"
                    ),
                )
            )
        destinations = await self._validate_destination_facts(
            facts,
            activates_form=str(facts.get("type", "")).casefold() in {"submit", "image"},
        )
        return BackendCoordinatePreflight(
            target=target,
            effective_destinations=destinations,
            financial_signal=facts.get("financialSignal") is True,
            equivalent_semantic_ref=self._equivalent_semantic_ref(target),
        )

    def _equivalent_semantic_ref(self, target: BackendTargetDescriptor) -> str | None:
        """Return one unambiguous current ARIA target for the coordinate hit."""

        if target.role == "canvas" or target.disabled or target.file or target.protected:
            return None
        matches = [
            candidate.ref
            for candidate in self._semantic_targets
            if candidate.frame_key == target.frame_key
            and candidate.frame_origin == target.frame_origin
            and candidate.role == target.role
            and candidate.name == target.name
            and candidate.control_kind == target.control_kind
            and candidate.disabled == target.disabled
            and candidate.file == target.file
            and candidate.protected == target.protected
        ]
        return matches[0] if len(matches) == 1 else None

    async def perform_coordinate_commit(
        self,
        request: BackendCoordinateRequest,
        *,
        expected: BackendCoordinatePreflight | None = None,
    ) -> BackendActionOutcome:
        """Repeat coordinate preflight and click once only when exact facts remain current."""

        state_before = await self.state()
        try:
            preflight = await self.preflight_coordinate_commit(request)
        except BrowserError as exc:
            return _not_performed_outcome(state_before, exc.failure)
        if expected is not None and preflight != expected:
            return _not_performed_outcome(
                state_before,
                BrowserFailure(
                    code="stale_target",
                    message="browser coordinate target or destination changed before dispatch",
                ),
            )
        action = BrowserActionRequest(
            kind="coordinate_commit",
            dialog=BrowserDialogPolicy(
                response=request.dialog_response,
                prompt_text=request.dialog_prompt_text,
            ),
        )

        async def click() -> None:
            await self._page.mouse.click(request.x, request.y)

        return await self._perform_raw_effect(
            state_before,
            action,
            click,
            reviewed_destinations=preflight.effective_destinations,
        )

    async def _coordinate_target_facts(
        self,
        x: float,
        y: float,
    ) -> dict[str, object] | None:
        """Resolve the actual hit target through open shadows and child frames."""

        frame = self._page.main_frame
        local_x = x
        local_y = y
        for _depth in range(20):
            handle = await frame.evaluate_handle(
                """({x, y}) => {
                    let element = document.elementFromPoint(x, y);
                    const visited = new Set();
                    while (element && element.shadowRoot && !visited.has(element)) {
                        visited.add(element);
                        const nested = element.shadowRoot.elementFromPoint(x, y);
                        if (!nested || nested === element) break;
                        element = nested;
                    }
                    return element;
                }""",
                {"x": local_x, "y": local_y},
            )
            element = handle.as_element()
            if element is None:
                await handle.dispose()
                return None
            try:
                facts = await element.evaluate(
                    """element => {
                        const hitTag = String(element.tagName || '').toUpperCase();
                        let control = null;
                        let activation = null;
                        let cursor = element;
                        const visited = new Set();
                        while (cursor && !visited.has(cursor)) {
                            visited.add(cursor);
                            if (!activation && cursor.matches && cursor.matches(
                                'a[href],button,input,select,textarea,' +
                                '[role="button"],[role="link"],[role="menuitem"],' +
                                '[contenteditable]:not([contenteditable="false"])'
                            )) {
                                activation = cursor;
                            }
                            if (cursor.matches && cursor.matches(
                                'input,textarea,select,[contenteditable]:not([contenteditable="false"])'
                            )) {
                                control = cursor;
                                break;
                            }
                            if (String(cursor.tagName || '').toUpperCase() === 'LABEL' &&
                                cursor.control) {
                                control = cursor.control;
                                break;
                            }
                            const root = cursor.getRootNode ? cursor.getRootNode() : null;
                            cursor = cursor.parentElement || (root && root.host) || null;
                        }
                        const target = control || activation || element;
                        const form = target.form ||
                            (target.closest ? target.closest('form') : null);
                        const controls = [target];
                        let controlOverflow = false;
                        if (form && form.querySelectorAll) {
                            const formControls = Array.from(form.querySelectorAll(
                                'button,input,select,textarea'
                            ));
                            controlOverflow = formControls.length > 100;
                            controls.push(...formControls.slice(0, 100));
                        }
                        const metadata = [];
                        const addMetadata = candidate => {
                            if (!candidate) return;
                            for (const attribute of [
                                'aria-label', 'autocomplete', 'id', 'name',
                                'placeholder', 'title', 'type'
                            ]) {
                                metadata.push(String(
                                    candidate.getAttribute(attribute) || ''
                                ).slice(0, 1000));
                            }
                            if (candidate.labels) {
                                for (const label of Array.from(candidate.labels).slice(0, 10)) {
                                    metadata.push(
                                        String(label.textContent || '').slice(0, 1000)
                                    );
                                }
                            }
                        };
                        for (const candidate of controls) addMetadata(candidate);
                        if (form) {
                            for (const attribute of ['aria-label', 'id', 'name', 'title']) {
                                metadata.push(
                                    String(form.getAttribute(attribute) || '').slice(0, 1000)
                                );
                            }
                            metadata.push(String(form.action || '').slice(0, 8000));
                        }
                        metadata.push(String(target.textContent || '').slice(0, 2000));
                        const signalText = metadata.join(' ').toLowerCase();
                        const signalWords = new Set(signalText.match(/[a-z]+/g) || []);
                        const directFinancial = [
                            'pay', 'payment', 'purchase', 'buy', 'checkout', 'order',
                            'charge', 'charged', 'charging',
                            'donate', 'donation', 'transfer', 'subscription',
                            'subscribe', 'bid', 'billing', 'card'
                        ].some(word => signalWords.has(word)) || signalText.includes('cc-');
                        const paidBooking = [
                            'book', 'booking', 'reserve', 'reservation'
                        ].some(word => signalWords.has(word)) && [
                            'price', 'cost', 'fee', 'pay', 'card', 'total'
                        ].some(word => signalWords.has(word));
                        let restrictedInteraction = null;
                        if (['captcha', 'recaptcha', 'hcaptcha', 'turnstile'].some(
                            word => signalText.includes(word)
                        )) restrictedInteraction = 'captcha';
                        else if (['passkey', 'webauthn'].some(
                            word => signalText.includes(word)
                        )) restrictedInteraction = 'passkey';
                        else if (signalWords.has('sso') ||
                            signalText.includes('single sign-on')) restrictedInteraction = 'sso';
                        return {
                            hitTag,
                            tag: String(target.tagName || '').toUpperCase(),
                            type: String(target.type || '').slice(0, 100),
                            autocomplete: String(
                                target.getAttribute('autocomplete') || ''
                            ).slice(0, 1000),
                            id: String(target.id || '').slice(0, 1000),
                            name: String(target.getAttribute('name') || '').slice(0, 1000),
                            aria: String(
                                target.getAttribute('aria-label') || ''
                            ).slice(0, 1000),
                            role: String(target.getAttribute('role') || '').slice(0, 100),
                            alt: String(target.getAttribute('alt') || '').slice(0, 1000),
                            title: String(target.getAttribute('title') || '').slice(0, 1000),
                            placeholder: String(
                                target.getAttribute('placeholder') || ''
                            ).slice(0, 1000),
                            text: String(
                                target.innerText || target.textContent || ''
                            ).slice(0, 2000),
                            contenteditable: String(
                                target.getAttribute('contenteditable') || ''
                            ).slice(0, 100),
                            inputmode: String(
                                target.getAttribute('inputmode') || ''
                            ).slice(0, 100),
                            accept: String(
                                target.getAttribute('accept') || ''
                            ).slice(0, 10000),
                            multiple: Boolean(target.multiple),
                            disabled: Boolean(target.disabled),
                            editable: Boolean(
                                !target.disabled && !target.readOnly &&
                                (target.matches('textarea,select') ||
                                    (target.matches('input') && ![
                                        'button', 'checkbox', 'file', 'hidden', 'image',
                                        'radio', 'reset', 'submit'
                                    ].includes(String(target.type || '').toLowerCase())) ||
                                    target.isContentEditable)
                            ),
                            checked: typeof target.checked === 'boolean' ?
                                target.checked : null,
                            optionLabels: target.matches('select') ?
                                Array.from(target.options, option =>
                                    String(option.textContent || '').trim().slice(0, 1000)
                                ).slice(0, 200) : [],
                            href: typeof target.href === 'string' ? target.href : null,
                            formAction: target.form &&
                                typeof target.form.action === 'string' ?
                                (target.hasAttribute('formaction') &&
                                    typeof target.formAction === 'string' &&
                                    target.formAction ? target.formAction :
                                    target.form.action) : null,
                            financialSignal: directFinancial || paidBooking || controlOverflow,
                            restrictedInteraction,
                        };
                    }"""
                )
                if not isinstance(facts, dict):
                    return None
                if str(facts.get("hitTag", "")).casefold() not in {"iframe", "frame"}:
                    frame_key, frame_origin = self._frame_identity(frame)
                    facts["frameKey"] = frame_key
                    facts["frameOrigin"] = frame_origin
                    frame_url = frame.url.casefold()
                    if any(
                        marker in frame_url
                        for marker in (
                            "recaptcha",
                            "hcaptcha",
                            "challenges.cloudflare.com",
                            "arkoselabs",
                        )
                    ):
                        facts["restrictedInteraction"] = "captcha"
                    return facts
                child = await element.content_frame()
                geometry = await element.evaluate(
                    """element => {
                        const rect = element.getBoundingClientRect();
                        return {
                            left: rect.left,
                            top: rect.top,
                            width: rect.width,
                            height: rect.height,
                            offsetWidth: element.offsetWidth,
                            offsetHeight: element.offsetHeight,
                            clientLeft: element.clientLeft,
                            clientTop: element.clientTop,
                            clientWidth: element.clientWidth,
                            clientHeight: element.clientHeight,
                        };
                    }"""
                )
            finally:
                await handle.dispose()
            if child is None or not isinstance(geometry, dict):
                raise BrowserError(
                    BrowserFailure(
                        code="incompatible_target",
                        message="browser coordinate nested target could not be resolved",
                    )
                )
            values = {
                name: float(geometry.get(name) or 0)
                for name in (
                    "left",
                    "top",
                    "width",
                    "height",
                    "offsetWidth",
                    "offsetHeight",
                    "clientLeft",
                    "clientTop",
                    "clientWidth",
                    "clientHeight",
                )
            }
            if (
                not all(math.isfinite(value) for value in values.values())
                or values["offsetWidth"] <= 0
                or values["offsetHeight"] <= 0
                or values["clientWidth"] <= 0
                or values["clientHeight"] <= 0
            ):
                raise BrowserError(
                    BrowserFailure(
                        code="incompatible_target",
                        message="browser coordinate nested target geometry was invalid",
                    )
                )
            scale_x = values["width"] / values["offsetWidth"]
            scale_y = values["height"] / values["offsetHeight"]
            if scale_x <= 0 or scale_y <= 0:
                raise BrowserError(
                    BrowserFailure(
                        code="incompatible_target",
                        message="browser coordinate nested target geometry was invalid",
                    )
                )
            local_x = (local_x - values["left"]) / scale_x - values["clientLeft"]
            local_y = (local_y - values["top"]) / scale_y - values["clientTop"]
            if not (0 <= local_x < values["clientWidth"] and 0 <= local_y < values["clientHeight"]):
                raise BrowserError(
                    BrowserFailure(
                        code="incompatible_target",
                        message="browser coordinate does not hit nested frame content",
                    )
                )
            if child.url not in {"about:blank", "about:srcdoc"}:
                await self._session._destination_guard(child.url)
            frame = child
        raise BrowserError(
            BrowserFailure(
                code="incompatible_target",
                message="browser coordinate nesting exceeds the supported limit",
            )
        )

    async def _preflight_custom_target(
        self,
        request: BackendActionRequest,
    ) -> tuple[Locator, BackendTargetDescriptor]:
        self._ensure_open()
        resolved = await self._resolve_target(request.target.ref)
        if resolved is None:
            raise BrowserError(
                BrowserFailure(
                    code="stale_target",
                    message="browser target changed before effect dispatch",
                )
            )
        frame, locator = resolved
        live = await self._describe_target(
            locator,
            frame=frame,
            ref=request.target.ref,
            role=request.target.role,
            name=request.target.name,
            fallback_option_labels=request.target.option_labels,
        )
        if (
            not _same_target(request.target, live)
            or not await locator.is_visible()
            or live.disabled
        ):
            raise BrowserError(
                BrowserFailure(
                    code="stale_target",
                    message="browser target changed before effect dispatch",
                )
            )
        return locator, live

    async def _resolve_protected_target(
        self,
        target: BackendTargetDescriptor,
    ) -> tuple[Locator, BackendTargetDescriptor]:
        self._ensure_open()
        resolved = await self._resolve_target(target.ref)
        if resolved is None:
            raise BrowserError(
                BrowserFailure(
                    code="stale_target",
                    message="protected browser target changed before dispatch",
                )
            )
        frame, locator = resolved
        live = await self._describe_target(
            locator,
            frame=frame,
            ref=target.ref,
            role=target.role,
            name=target.name,
            fallback_option_labels=target.option_labels,
        )
        try:
            visible = await locator.is_visible()
        except PlaywrightError as exc:
            raise _backend_error("protected browser target could not be inspected", exc) from exc
        if (
            not _same_target(target, live)
            or not visible
            or live.disabled
            or not live.editable
            or not live.protected
            or live.protected_kind is None
            or live.file
        ):
            raise BrowserError(
                BrowserFailure(
                    code="stale_target",
                    message="protected browser target changed before dispatch",
                )
            )
        return locator, live

    async def _perform_raw_effect(
        self,
        state_before: BackendPageState,
        action: BrowserActionRequest,
        operation: Callable[[], Awaitable[None]],
        *,
        reviewed_destinations: tuple[str, ...] = (),
    ) -> BackendActionOutcome:
        navigation_occurred = False
        dialogs: list[BrowserDialogObservation] = []
        popups: list[Page] = []

        def observe_navigation(frame: Frame) -> None:
            nonlocal navigation_occurred
            if frame == self._page.main_frame:
                navigation_occurred = True

        async def handle_dialog(dialog: Dialog) -> None:
            matched = action.dialog.prompt_text is None or dialog.type == "prompt"
            response: Literal["dismissed", "accepted", "unhandled"] = "unhandled"
            try:
                if action.dialog.response == "accept" and matched:
                    await dialog.accept(action.dialog.prompt_text)
                    response = "accepted"
                else:
                    await dialog.dismiss()
                    response = "dismissed"
            except PlaywrightError:
                matched = False
            dialogs.append(
                BrowserDialogObservation(
                    kind=_dialog_kind(dialog.type),
                    message=dialog.message[:2_000],
                    response=response,
                    matched_policy=matched,
                )
            )

        def observe_popup(page: Page) -> None:
            self._session._register_page(page)
            popups.append(page)

        self._page.on("framenavigated", observe_navigation)
        self._page.on("dialog", handle_dialog)
        self._page.on("popup", observe_popup)
        self._session.take_blocked_error(self._page)
        self._session.begin_action(
            self._page,
            reviewed_destinations=reviewed_destinations,
        )
        try:
            await operation()
            for popup in popups:
                with suppress(PlaywrightError):
                    await popup.wait_for_load_state("domcontentloaded", timeout=250)
            if navigation_occurred and not self._page.is_closed():
                await self._page.wait_for_load_state("domcontentloaded")
            if failure := await self._postdispatch_destination_failure(
                popups,
                allow_opener_blank=state_before.url == "about:blank",
            ):
                return await self._uncertain_outcome(
                    state_before,
                    navigation_occurred=navigation_occurred,
                    dialogs=dialogs,
                    failure=failure,
                )
        except asyncio.CancelledError:
            raise
        except (BrowserError, PlaywrightError, PlaywrightTimeoutError, TimeoutError) as exc:
            blocked = self._session.take_blocked_error(self._page)
            failure = (
                blocked.failure.model_copy(update={"outcome_uncertain": True})
                if blocked is not None
                else (
                    exc.failure.model_copy(update={"outcome_uncertain": True})
                    if isinstance(exc, BrowserError)
                    else BrowserFailure(
                        code="action_in_doubt",
                        message="browser effect failed after dispatch may have begun",
                        outcome_uncertain=True,
                    )
                )
            )
            return await self._uncertain_outcome(
                state_before,
                navigation_occurred=navigation_occurred,
                dialogs=dialogs,
                failure=failure,
            )
        finally:
            self._session.end_action(self._page)
            self._page.remove_listener("framenavigated", observe_navigation)
            self._page.remove_listener("dialog", handle_dialog)
            self._page.remove_listener("popup", observe_popup)
        if blocked := self._session.take_blocked_error(self._page):
            return await self._uncertain_outcome(
                state_before,
                navigation_occurred=navigation_occurred,
                dialogs=dialogs,
                failure=blocked.failure.model_copy(update={"outcome_uncertain": True}),
            )
        return BackendActionOutcome(
            disposition="performed",
            dispatch_state="completed",
            state_before=state_before,
            state_after=await self.state(),
            navigation_occurred=navigation_occurred,
            dialogs=tuple(dialogs),
        )

    async def _validate_effective_destinations(
        self,
        locator: Locator,
        request: BackendActionRequest,
    ) -> tuple[tuple[str, ...], bool]:
        try:
            facts = await locator.evaluate(
                """element => {
                    const formAction = element.form &&
                        typeof element.form.action === 'string' ?
                        (element.hasAttribute('formaction') &&
                            typeof element.formAction === 'string' && element.formAction ?
                            element.formAction : element.form.action) : null;
                    const form = element.form ||
                        (element.closest ? element.closest('form') : null);
                    const controls = [element];
                    let controlOverflow = false;
                    if (form && form.querySelectorAll) {
                        const formControls = Array.from(form.querySelectorAll(
                            'button,input,select,textarea'
                        ));
                        controlOverflow = formControls.length > 100;
                        controls.push(...formControls.slice(0, 100));
                    }
                    const metadata = [];
                    const addMetadata = candidate => {
                        if (!candidate) return;
                        for (const attribute of [
                            'aria-label', 'autocomplete', 'id', 'name',
                            'placeholder', 'title', 'type'
                        ]) {
                            metadata.push(
                                String(candidate.getAttribute(attribute) || '').slice(0, 1000)
                            );
                        }
                        if (candidate.labels) {
                            for (const label of Array.from(candidate.labels).slice(0, 10)) {
                                metadata.push(
                                    String(label.textContent || '').slice(0, 1000)
                                );
                            }
                        }
                    };
                    addMetadata(element);
                    for (const control of controls) addMetadata(control);
                    if (form) {
                        for (const attribute of ['aria-label', 'id', 'name', 'title']) {
                            metadata.push(
                                String(form.getAttribute(attribute) || '').slice(0, 1000)
                            );
                        }
                        metadata.push(String(form.action || '').slice(0, 8000));
                    }
                    metadata.push(String(element.textContent || '').slice(0, 2000));
                    const signalText = metadata.join(' ').toLowerCase();
                    const signalWords = new Set(signalText.match(/[a-z]+/g) || []);
                    const directFinancial = [
                        'pay', 'payment', 'purchase', 'buy', 'checkout', 'order',
                        'charge', 'charged', 'charging',
                        'donate', 'donation', 'transfer', 'subscription',
                        'subscribe', 'bid', 'billing', 'card'
                    ].some(word => signalWords.has(word)) || signalText.includes('cc-');
                    const paidBooking = [
                        'book', 'booking', 'reserve', 'reservation'
                    ].some(word => signalWords.has(word)) && [
                        'price', 'cost', 'fee', 'pay', 'card', 'total'
                    ].some(word => signalWords.has(word));
                    return {
                        href: typeof element.href === 'string' ? element.href : null,
                        formAction,
                        type: typeof element.type === 'string' ? element.type : null,
                        tag: typeof element.tagName === 'string' ? element.tagName : null,
                        financialSignal: directFinancial || paidBooking || controlOverflow,
                    };
                }"""
            )
        except PlaywrightError as exc:
            raise _backend_error(
                "browser target destination could not be inspected",
                exc,
            ) from exc
        if not isinstance(facts, dict):
            raise BrowserError(
                BrowserFailure(
                    code="backend_error",
                    message="browser target destination inspection was invalid",
                )
            )
        effective_type = facts.get("type")
        tag = facts.get("tag")
        activates_form = effective_type in {"submit", "image"} or (
            request.action.kind == "commit"
            and request.action.activation == "enter"
            and tag in {"INPUT", "TEXTAREA"}
        )
        destinations = await self._validate_destination_facts(
            facts,
            activates_form=activates_form,
        )
        return destinations, facts.get("financialSignal") is True

    async def _validate_destination_facts(
        self,
        facts: dict[str, object],
        *,
        activates_form: bool,
    ) -> tuple[str, ...]:
        destinations: list[str] = []
        href = facts.get("href")
        if isinstance(href, str) and href:
            destinations.append(href)
        form_action = facts.get("formAction")
        if activates_form and isinstance(form_action, str) and form_action:
            destinations.append(form_action)
        destinations = list(dict.fromkeys(destinations))
        for destination in destinations:
            if len(destination) > 8_000:
                raise BrowserError(
                    BrowserFailure(
                        code="destination_blocked",
                        message="browser action destination exceeds the supported URL limit",
                    )
                )
            scheme = urlsplit(destination).scheme.casefold()
            if scheme in {"blob", "data", "javascript"}:
                raise BrowserError(
                    BrowserFailure(
                        code="destination_blocked",
                        message="browser action destination uses a blocked URL scheme",
                    )
                )
            await self._session._destination_guard(destination)
        return tuple(destinations)

    async def _postdispatch_destination_failure(
        self,
        popups: list[Page],
        *,
        allow_opener_blank: bool,
    ) -> BrowserFailure | None:
        blocked = False
        for page in (self._page, *popups):
            if page.is_closed():
                continue
            if page.url == "about:blank":
                if page is self._page and allow_opener_blank:
                    continue
                blocked = True
                await self._discard_blocked_page(page)
                continue
            try:
                await self._session._destination_guard(page.url)
            except BrowserError:
                blocked = True
                await self._discard_blocked_page(page)
        if not blocked:
            return None
        return BrowserFailure(
            code="destination_blocked",
            message=(
                "browser action reached a blocked destination; Ricky stopped controlling it"
                if not self._session.pages_owned
                else "browser action reached a blocked destination; the page was closed"
            ),
            outcome_uncertain=True,
        )

    async def _discard_blocked_page(self, page: Page) -> None:
        if self._session.pages_owned:
            with suppress(PlaywrightError):
                await page.close(run_before_unload=False)
            return
        self._session.quarantine(page)

    async def _uncertain_outcome(
        self,
        state_before: BackendPageState,
        *,
        navigation_occurred: bool,
        dialogs: list[BrowserDialogObservation],
        failure: BrowserFailure,
    ) -> BackendActionOutcome:
        state_after = await self.state()
        return BackendActionOutcome(
            disposition="in_doubt",
            dispatch_state="dispatched",
            state_before=state_before,
            state_after=state_after,
            navigation_occurred=navigation_occurred,
            dialogs=tuple(dialogs),
            failure=failure,
        )

    async def _snapshot_targets(self, content: str) -> tuple[BackendTargetDescriptor, ...]:
        targets: list[BackendTargetDescriptor] = []
        for parsed in _parse_aria_targets(content):
            ref = parsed.ref
            resolved = await self._resolve_target(ref, allow_ambiguous=True)
            if resolved is None:
                targets.append(
                    BackendTargetDescriptor(
                        ref=ref,
                        role=parsed.role,
                        name=parsed.name,
                        option_labels=parsed.option_labels,
                    )
                )
                continue
            frame, locator = resolved
            targets.append(
                await self._describe_target(
                    locator,
                    frame=frame,
                    ref=ref,
                    role=parsed.role,
                    name=parsed.name,
                    fallback_option_labels=parsed.option_labels,
                )
            )
        result = tuple(targets)
        self._semantic_targets = result
        return result

    async def _resolve_target(
        self,
        ref: str,
        *,
        allow_ambiguous: bool = False,
    ) -> tuple[Frame, Locator] | None:
        if ref.startswith("d"):
            return self._dom_targets.get(ref)
        locator = self._page.locator(f"aria-ref={ref}")
        try:
            count = await locator.count()
        except PlaywrightError as exc:
            if "Invalid frame in aria-ref selector" in str(exc):
                return None
            raise _backend_error("browser target could not be resolved", exc) from exc
        if count == 0:
            return None
        if count > 1 and not allow_ambiguous:
            raise BrowserError(
                BrowserFailure(
                    code="ambiguous_target",
                    message="browser target resolved to more than one live element",
                )
            )
        if count > 1:
            return None
        try:
            handle = await locator.element_handle()
            if handle is None:
                return None
            try:
                frame = await handle.owner_frame()
            finally:
                await handle.dispose()
        except PlaywrightError as exc:
            raise _backend_error("browser target frame could not be resolved", exc) from exc
        if frame is None:
            return None
        return frame, locator

    async def _describe_target(
        self,
        locator: Locator,
        *,
        frame: Frame,
        ref: str,
        role: str,
        name: str,
        fallback_option_labels: tuple[str, ...] = (),
    ) -> BackendTargetDescriptor:
        attributes = {
            attribute: await _attribute(locator, attribute)
            for attribute in (
                "aria-label",
                "accept",
                "autocomplete",
                "contenteditable",
                "id",
                "inputmode",
                "name",
                "multiple",
                "role",
                "type",
            )
        }
        input_type = (attributes["type"] or "").casefold()
        with suppress(PlaywrightError):
            effective_type = await locator.evaluate(
                "element => typeof element.type === 'string' ? element.type : null"
            )
            if isinstance(effective_type, str):
                input_type = effective_type.casefold()[:100]
        explicit_role = attributes["role"] or role
        control_kind = _control_kind(explicit_role, input_type, attributes["contenteditable"])
        try:
            disabled = not await locator.is_enabled()
            editable = await locator.is_editable()
        except PlaywrightError:
            disabled = False
            editable = False
        checked: bool | None = None
        if control_kind in {"checkbox", "radio"}:
            with suppress(PlaywrightError):
                checked = await locator.is_checked()
        option_labels: tuple[str, ...] = ()
        if control_kind == "select":
            with suppress(PlaywrightError):
                raw_labels = await locator.evaluate(
                    "element => Array.from(element.options, option => option.textContent)"
                )
                if isinstance(raw_labels, list):
                    option_labels = tuple(
                        label.strip()[:1_000]
                        for label in raw_labels[:200]
                        if isinstance(label, str)
                    )
            if not option_labels:
                option_labels = fallback_option_labels
        identifying_text = " ".join(
            value
            for value in (
                name,
                attributes["aria-label"],
                attributes["autocomplete"],
                attributes["id"],
                attributes["name"],
            )
            if value
        ).casefold()
        autocomplete_tokens = set((attributes["autocomplete"] or "").casefold().split())
        protected_autocomplete = bool(
            autocomplete_tokens
            & {
                "current-password",
                "new-password",
                "one-time-code",
                "username",
                "webauthn",
            }
        ) or any(token.startswith("cc-") for token in autocomplete_tokens)
        protected = (
            protected_autocomplete
            or input_type == "password"
            or any(term in identifying_text for term in _PROTECTED_TERMS)
        )
        protected_kind = _protected_control_kind(
            autocomplete_tokens,
            input_type=input_type,
            identifying_text=identifying_text,
        )
        file_control = input_type == "file"
        accept = tuple(
            item.strip()[:100]
            for item in (attributes["accept"] or "").split(",")[:100]
            if item.strip()
        )
        frame_key, frame_origin = self._frame_identity(frame)
        return BackendTargetDescriptor(
            ref=ref,
            role=role,
            name=name[:500],
            control_kind=control_kind,
            frame_origin=frame_origin,
            frame_key=frame_key,
            checked=checked,
            disabled=disabled,
            editable=editable,
            option_labels=option_labels,
            consequential=_is_consequential(name, input_type),
            protected=protected,
            protected_kind=protected_kind,
            file=file_control,
            multiple=attributes["multiple"] is not None,
            accept=accept,
        )

    def _frame_identity(self, selected: Frame) -> tuple[str, str | None]:
        frames = self._page.frames
        index = frames.index(selected)
        key = "main" if selected == self._page.main_frame else f"frame-{index}"
        try:
            origin = canonical_origin(selected.url)
        except ValueError:
            return key, None
        return key, origin

    async def close(self) -> None:
        if self._page.is_closed():
            return
        if not self._session.pages_owned:
            self._session.quarantine(self._page)
            return
        with suppress(PlaywrightError):
            await self._page.close(run_before_unload=False)

    def _ensure_open(self) -> None:
        self._session.ensure_connected()
        if self._page.is_closed() or self._session.is_quarantined(self._page):
            raise BrowserError(BrowserFailure(code="page_closed", message="browser page is closed"))


async def _disconnect_browser(browser: Browser) -> None:
    """Dispose a connected Playwright client without owning the external process."""

    await browser.close()


async def _replace_owned_pages_with_blank(context: BrowserContext) -> None:
    """Replace restored tabs without ever closing Chromium's last live page."""

    restored_pages = tuple(context.pages)
    blank_page = await context.new_page()
    for page in restored_pages:
        if page is blank_page:
            continue
        with suppress(PlaywrightError):
            await page.close(run_before_unload=False)


async def _best_effort_disconnect(browser: Browser) -> None:
    """Bound cleanup after an attachment that never became observable."""

    with suppress(PlaywrightError, TimeoutError):
        async with asyncio.timeout(5):
            await browser.close()


async def _close_page(page: Page) -> None:
    """Close one unregistered Ricky-owned overflow page without surfacing its data."""

    with suppress(PlaywrightError):
        await page.close(run_before_unload=False)


async def _cancel_download(download: Download) -> None:
    """Cancel and delete one attempt-owned or unexpected Playwright download."""
    with suppress(PlaywrightError):
        await download.cancel()
    with suppress(PlaywrightError):
        await download.delete()


def _backend_error(message: str, exc: Exception) -> BrowserError:
    del exc
    return BrowserError(BrowserFailure(code="backend_error", message=message))


def _matches_reviewed_destination(actual: str, reviewed: str) -> bool:
    """Match one initial request to its reviewed DOM-resolved destination."""

    actual_parts = urlsplit(actual)
    reviewed_parts = urlsplit(reviewed)
    actual_network = urlunsplit(
        (
            actual_parts.scheme,
            actual_parts.netloc,
            actual_parts.path,
            actual_parts.query,
            "",
        )
    )
    reviewed_network = urlunsplit(
        (
            reviewed_parts.scheme,
            reviewed_parts.netloc,
            reviewed_parts.path,
            reviewed_parts.query,
            "",
        )
    )
    if actual_network == reviewed_network:
        return True
    if (
        actual_parts.scheme != reviewed_parts.scheme
        or actual_parts.netloc != reviewed_parts.netloc
        or actual_parts.path != reviewed_parts.path
    ):
        return False
    # A GET form adds successful-control query entries to its resolved action.
    # Preserve every statically reviewed query pair while permitting only those
    # same-endpoint additions.
    remaining = list(parse_qsl(actual_parts.query, keep_blank_values=True))
    for pair in parse_qsl(reviewed_parts.query, keep_blank_values=True):
        if pair not in remaining:
            return False
        remaining.remove(pair)
    return True


def _not_performed_outcome(
    state: BackendPageState,
    failure: BrowserFailure,
) -> BackendActionOutcome:
    return BackendActionOutcome(
        disposition="not_performed",
        dispatch_state="not_dispatched",
        state_before=state,
        state_after=state,
        failure=failure,
    )


def _decode_aria_name(encoded: str | None) -> str:
    if encoded is None:
        return ""
    try:
        decoded = json.loads(encoded)
    except (json.JSONDecodeError, TypeError):
        return ""
    return decoded if isinstance(decoded, str) else ""


def _dom_role(facts: dict[str, object]) -> str:
    explicit = facts.get("role")
    if isinstance(explicit, str) and explicit:
        return explicit[:100]
    tag = str(facts.get("tag") or "").casefold()
    return {
        "a": "link",
        "button": "button",
        "input": "textbox",
        "select": "combobox",
        "textarea": "textbox",
        "canvas": "canvas",
    }.get(tag, tag or "other")[:100]


def _dom_name(facts: dict[str, object]) -> str:
    for key in ("aria", "alt", "title", "placeholder", "text"):
        value = facts.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:500]
    return ""


def _coordinate_target_descriptor(facts: dict[str, object]) -> BackendTargetDescriptor:
    """Convert one locally inspected coordinate hit into exact private target facts."""

    tag = str(facts.get("tag") or "").casefold()
    input_type = str(facts.get("type") or "").casefold()[:100]
    explicit_role = str(facts.get("role") or "")[:100]
    role = explicit_role or {
        "a": "link",
        "button": "button",
        "select": "combobox",
        "textarea": "textbox",
    }.get(tag, "")
    if not role and tag == "input":
        role = {
            "button": "button",
            "checkbox": "checkbox",
            "image": "button",
            "radio": "radio",
            "reset": "button",
            "submit": "button",
        }.get(input_type, "textbox")
    if not role:
        role = tag or "other"
    name = _dom_name(facts)
    contenteditable = str(facts.get("contenteditable") or "") or None
    control_kind = _control_kind(role, input_type, contenteditable)
    autocomplete_tokens = set(str(facts.get("autocomplete") or "").casefold().split())
    identifying_text = " ".join(
        str(facts.get(key) or "")
        for key in ("name", "aria", "autocomplete", "id", "placeholder", "title")
    ).casefold()
    protected_autocomplete = (
        bool(autocomplete_tokens & _PROTECTED_AUTOCOMPLETE_TOKENS)
        or "username" in autocomplete_tokens
    )
    protected = (
        protected_autocomplete
        or input_type == "password"
        or any(term in identifying_text for term in _PROTECTED_TERMS)
    )
    raw_options = facts.get("optionLabels")
    option_labels = (
        tuple(str(label)[:1_000] for label in raw_options[:200])
        if isinstance(raw_options, list)
        else ()
    )
    checked_value = facts.get("checked")
    checked = (
        checked_value
        if isinstance(checked_value, bool) and control_kind in {"checkbox", "radio"}
        else None
    )
    accept = tuple(
        item.strip()[:100]
        for item in str(facts.get("accept") or "").split(",")[:100]
        if item.strip()
    )
    frame_origin = facts.get("frameOrigin")
    raw_restricted = facts.get("restrictedInteraction")
    restricted_interaction: Literal["captcha", "passkey", "sso"] | None = None
    if raw_restricted == "captcha":
        restricted_interaction = "captcha"
    elif raw_restricted == "passkey":
        restricted_interaction = "passkey"
    elif raw_restricted == "sso":
        restricted_interaction = "sso"
    return BackendTargetDescriptor(
        ref="d0",
        role=role[:100],
        name=name,
        control_kind=control_kind,
        frame_origin=frame_origin if isinstance(frame_origin, str) else None,
        frame_key=str(facts.get("frameKey") or "main")[:100],
        checked=checked,
        disabled=facts.get("disabled") is True,
        editable=facts.get("editable") is True,
        option_labels=option_labels,
        consequential=_is_consequential(name, input_type),
        protected=protected,
        protected_kind=_protected_control_kind(
            autocomplete_tokens,
            input_type=input_type,
            identifying_text=identifying_text,
        ),
        file=input_type == "file",
        multiple=facts.get("multiple") is True,
        accept=accept,
        restricted_interaction=restricted_interaction,
    )


def _parse_aria_targets(content: str) -> tuple[_ParsedAriaTarget, ...]:
    lines = content.splitlines()
    parsed: list[_ParsedAriaTarget] = []
    for index, line in enumerate(lines):
        match = _TARGET_LINE.match(line)
        if match is None:
            continue
        role = match.group("role")
        option_labels: tuple[str, ...] = ()
        if role in {"combobox", "listbox"}:
            parent_indent = len(match.group("indent"))
            nested: list[str] = []
            for candidate in lines[index + 1 :]:
                if not candidate.strip():
                    continue
                indentation = len(candidate) - len(candidate.lstrip(" \t"))
                if indentation <= parent_indent:
                    break
                option = _OPTION_LINE.match(candidate)
                if option is not None:
                    nested.append(_decode_aria_name(option.group("name"))[:1_000])
            option_labels = tuple(nested[:200])
        parsed.append(
            _ParsedAriaTarget(
                ref=match.group("ref"),
                role=role,
                name=_decode_aria_name(match.group("name")),
                option_labels=option_labels,
            )
        )
    return tuple(parsed)


def _bound_aria_content(content: str, *, character_limit: int) -> tuple[str, bool]:
    """Keep only complete ARIA lines within the configured provider-facing limit."""

    if len(content) <= character_limit:
        return content, False
    bounded = content[:character_limit]
    final_line_end = bounded.rfind("\n")
    if final_line_end < 0:
        return "", True
    return bounded[: final_line_end + 1], True


async def _attribute(locator: Locator, name: str) -> str | None:
    try:
        value = await locator.get_attribute(name)
    except PlaywrightError:
        return None
    return value[:1_000] if value is not None else None


def _control_kind(
    role: str,
    input_type: str,
    contenteditable: str | None,
) -> BrowserControlKind:
    if input_type == "file":
        return "file"
    if contenteditable is not None and contenteditable.casefold() != "false":
        return "contenteditable"
    if role == "link":
        return "link"
    if role == "button":
        return "button"
    if role == "searchbox" or input_type == "search":
        return "search"
    if input_type == "email":
        return "email"
    if input_type == "tel":
        return "telephone"
    if input_type == "url":
        return "url"
    if input_type in {"date", "datetime-local", "month", "time", "week"}:
        return "date"
    if input_type == "number":
        return "number"
    if role == "textbox":
        return "text"
    if role in {"combobox", "listbox"}:
        return "select"
    if role == "checkbox":
        return "checkbox"
    if role == "radio":
        return "radio"
    return "other"


def _is_consequential(name: str, input_type: str) -> bool:
    if input_type in {"submit", "image"}:
        return True
    words = set(re.findall(r"[a-z]+", name.casefold()))
    return bool(words & _CONSEQUENTIAL_WORDS)


def _protected_control_kind(
    autocomplete_tokens: set[str],
    *,
    input_type: str,
    identifying_text: str,
) -> ProtectedControlKind | None:
    """Classify only categories that the dedicated protected-fill path supports."""
    if "one-time-code" in autocomplete_tokens or any(
        term in identifying_text for term in ("one-time", "one time", "otp", "verification code")
    ):
        return "one_time_code"
    if "cc-csc" in autocomplete_tokens or any(
        term in identifying_text for term in ("cvc", "cvv", "security code")
    ):
        return "card_security_code"
    if "cc-number" in autocomplete_tokens or any(
        term in identifying_text for term in ("card number", "credit card", "debit card")
    ):
        return "card_number"
    if "cc-exp-month" in autocomplete_tokens:
        return "card_expiry_month"
    if "cc-exp-year" in autocomplete_tokens:
        return "card_expiry_year"
    if "cc-exp" in autocomplete_tokens or any(
        term in identifying_text for term in ("expiration", "expiry")
    ):
        return "card_expiry"
    if autocomplete_tokens & {
        "cc-name",
        "cc-given-name",
        "cc-additional-name",
        "cc-family-name",
    }:
        return "cardholder_name"
    if input_type == "password" or any(
        term in identifying_text for term in ("password", "passcode")
    ):
        return "password"
    if "username" in autocomplete_tokens:
        return "username"
    if any(term in identifying_text for term in _PROTECTED_TERMS):
        return "generic_secret"
    return None


def _same_target(
    expected: BackendTargetDescriptor,
    live: BackendTargetDescriptor,
) -> bool:
    return (
        expected.ref == live.ref
        and expected.role == live.role
        and expected.name == live.name
        and expected.control_kind == live.control_kind
        and expected.frame_origin == live.frame_origin
        and expected.frame_key == live.frame_key
        and expected.checked == live.checked
        and expected.disabled == live.disabled
        and expected.editable == live.editable
        and expected.option_labels == live.option_labels
        and expected.consequential == live.consequential
        and expected.protected == live.protected
        and expected.protected_kind == live.protected_kind
        and expected.file == live.file
        and expected.multiple == live.multiple
        and expected.accept == live.accept
    )


def _validate_action_compatibility(
    request: BackendActionRequest,
    target: BackendTargetDescriptor,
) -> None:
    kind = request.action.kind
    if target.file:
        raise BrowserError(
            BrowserFailure(code="file_control", message="file controls require user handoff")
        )
    if kind == "fill" and target.protected:
        raise BrowserError(
            BrowserFailure(
                code="protected_field",
                message="protected fields require user handoff",
            )
        )
    if kind == "click" and target.consequential:
        raise BrowserError(
            BrowserFailure(
                code="consequential_target",
                message="consequential controls require browser_commit",
            )
        )
    if kind == "fill" and (
        not target.editable
        or target.control_kind
        not in {
            "text",
            "search",
            "email",
            "telephone",
            "url",
            "date",
            "number",
            "textarea",
            "contenteditable",
        }
    ):
        _raise_incompatible("fill")
    if kind == "select":
        if target.control_kind != "select":
            _raise_incompatible("select")
        assert request.action.option_label is not None
        option_count = target.option_labels.count(request.action.option_label)
        if option_count == 0:
            raise BrowserError(
                BrowserFailure(
                    code="incompatible_target",
                    message="selected option is not available on this control",
                )
            )
        if option_count > 1:
            raise BrowserError(
                BrowserFailure(
                    code="ambiguous_target",
                    message="selected option label is not unique on this control",
                )
            )
    if kind == "set_checked" and target.control_kind not in {"checkbox", "radio"}:
        _raise_incompatible("set checked")
    if kind == "click" and target.control_kind not in {
        "button",
        "checkbox",
        "link",
        "radio",
        "other",
    }:
        _raise_incompatible("click")


def _raise_incompatible(action: str) -> None:
    raise BrowserError(
        BrowserFailure(
            code="incompatible_target",
            message=f"browser target is not compatible with {action}",
        )
    )


async def _dispatch_action(locator: Locator, request: BackendActionRequest) -> None:
    action = request.action
    if action.kind == "click":
        await locator.click()
        return
    if action.kind == "fill":
        assert action.value is not None
        await locator.fill(action.value)
        return
    if action.kind == "select":
        assert action.option_label is not None
        await locator.select_option(label=action.option_label)
        return
    if action.kind == "set_checked":
        assert action.checked is not None
        await locator.set_checked(action.checked)
        return
    if action.kind == "press_key":
        assert action.key is not None
        await locator.press(action.key)
        return
    assert action.activation is not None
    if action.activation == "click":
        await locator.click()
    elif action.activation == "enter":
        await locator.press("Enter")
    else:
        await locator.press("Space")


def _dialog_kind(
    value: str,
) -> Literal["alert", "confirm", "prompt", "beforeunload"]:
    if value in {"alert", "confirm", "prompt", "beforeunload"}:
        return cast(Literal["alert", "confirm", "prompt", "beforeunload"], value)
    return "alert"
