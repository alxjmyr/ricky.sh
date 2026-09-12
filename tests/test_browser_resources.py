"""Configured browser resource, attachment, and lifecycle contracts."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any, cast

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from browser_support import (
    FakeBrowserBackend,
    FakeBrowserPage,
    FakeBrowserSession,
    fake_executable,
)
from ricky.browser.backend import (
    BackendActionOutcome,
    BackendActionRequest,
    BackendPageState,
    BackendTargetDescriptor,
    BrowserCdpOptions,
    BrowserLaunchOptions,
    BrowserOpenOptions,
    DestinationGuard,
)
from ricky.browser.lease import BrowserResourceLease
from ricky.browser.playwright_backend import (
    PlaywrightBrowserBackend,
    _PlaywrightSession,
)
from ricky.browser.resources import persistent_browser_path
from ricky.browser.service import BrowserService
from ricky.browser.types import (
    BrowserActionRequest,
    BrowserActionTarget,
    BrowserError,
    BrowserFailure,
)
from ricky.config import RickySettings
from ricky.profiles import ProfileResourceRef


class _FakeNavigationCDP:
    def __init__(self) -> None:
        self.commands: list[tuple[str, dict[str, object] | None]] = []

    async def send(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        self.commands.append((method, params))
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "frame"}}}
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"type": "page"}}
        return {}

    def on(self, event: str, handler: object) -> None:
        pass

    async def detach(self) -> None:
        pass

    def remove_listener(self, event: str, handler: object) -> None:
        pass

    async def new_browser_cdp_session(self) -> _FakeNavigationCDP:
        return self

    async def new_cdp_session(self, page: object) -> _FakeNavigationCDP:
        return self


async def _allow_destination(_url: str) -> None:
    return None


async def _noop_close() -> None:
    return None


def _settings(
    tmp_path: Path,
    *,
    max_pages: int = 8,
    include_work: bool = False,
    extra_persistent: bool = False,
    attachment_timeout_seconds: float = 10.0,
) -> RickySettings:
    profile_configs: dict[str, object] = {
        "personal": {
            "browser": {
                "resources": {
                    "main": {
                        "kind": "persistent",
                        "description": "Personal signed-in browser",
                        "headless": True,
                    },
                    "debug": {
                        "kind": "cdp",
                        "description": "Dedicated local debug browser",
                        "endpoint": "http://127.0.0.1:9222",
                    },
                }
            }
        }
    }
    if extra_persistent:
        personal_browser = profile_configs["personal"]
        assert isinstance(personal_browser, dict)
        browser = personal_browser["browser"]
        assert isinstance(browser, dict)
        resources = browser["resources"]
        assert isinstance(resources, dict)
        resources["secondary"] = {
            "kind": "persistent",
            "description": "Secondary personal browser",
            "headless": True,
        }
    if include_work:
        profile_configs["work"] = {
            "browser": {
                "resources": {
                    "main": {
                        "kind": "persistent",
                        "description": "Work signed-in browser",
                    }
                }
            }
        }
    return RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": str(tmp_path / "project"),
            "browser": {
                "enabled": True,
                "headless": True,
                "max_sessions": 2,
                "max_pages": max_pages,
                "attachment_timeout_seconds": attachment_timeout_seconds,
                "allowed_private_origins": ["http://127.0.0.1:8765"],
            },
            "profile_configs": profile_configs,
        }
    )


def _service(
    settings: RickySettings,
    tmp_path: Path,
    backend: FakeBrowserBackend | None = None,
    *,
    access_work: bool = False,
) -> tuple[BrowserService, FakeBrowserBackend]:
    selected = backend or FakeBrowserBackend()
    scope = settings.resolve_profile_scope(
        "personal",
        access_profiles=["work"] if access_work else [],
    )
    return (
        BrowserService(
            settings,
            scope=scope,
            backend=selected,
            executable_path=fake_executable(tmp_path),
        ),
        selected,
    )


async def test_cdp_connection_timeout_is_bounded_and_hides_endpoint() -> None:
    endpoint = "http://127.0.0.1:9222"

    class TimingOutChromium:
        async def connect_over_cdp(self, selected: str, *, timeout: float):
            assert selected == endpoint
            assert timeout == 250
            raise PlaywrightTimeoutError("raw endpoint-specific timeout")

    class FakePlaywright:
        chromium = TimingOutChromium()

    async def allow(_url: str) -> None:
        return None

    backend = PlaywrightBrowserBackend()
    backend._playwright = cast(Any, FakePlaywright())
    options = BrowserCdpOptions(
        mode="attached_cdp",
        endpoint=endpoint,
        attachment_timeout_ms=250,
        navigation_timeout_ms=1_000,
        operation_timeout_ms=1_000,
        max_redirects=3,
        page_discovery_limit=8,
    )

    with pytest.raises(BrowserError) as timed_out:
        await backend.open_session(options, destination_guard=allow)

    assert timed_out.value.failure.code == "attachment_timeout"
    assert timed_out.value.failure.retryable
    assert endpoint not in timed_out.value.failure.message
    assert "raw endpoint" not in timed_out.value.failure.message


async def test_cdp_contexts_receive_configured_operation_timeouts() -> None:
    class FakeContext:
        pages: list[object] = []
        navigation_timeout: float | None = None
        operation_timeout: float | None = None

        def set_default_navigation_timeout(self, timeout: float) -> None:
            self.navigation_timeout = timeout

        def set_default_timeout(self, timeout: float) -> None:
            self.operation_timeout = timeout

        browser = _FakeNavigationCDP()

        async def new_cdp_session(self, _page: object) -> _FakeNavigationCDP:
            return _FakeNavigationCDP()

        def on(self, _event: str, _handler: object) -> None:
            return None

    class FakeBrowser:
        def __init__(self, context: FakeContext) -> None:
            self.contexts = [context]
            self.connected = True

        def is_connected(self) -> bool:
            return self.connected

        async def close(self) -> None:
            self.connected = False

    class FakeChromium:
        def __init__(self, browser: FakeBrowser) -> None:
            self.browser = browser

        async def connect_over_cdp(self, _endpoint: str, *, timeout: float) -> FakeBrowser:
            assert timeout == 250
            return self.browser

    class FakePlaywright:
        def __init__(self, browser: FakeBrowser) -> None:
            self.chromium = FakeChromium(browser)

        async def stop(self) -> None:
            return None

    context = FakeContext()
    browser = FakeBrowser(context)
    backend = PlaywrightBrowserBackend()
    backend._playwright = cast(Any, FakePlaywright(browser))
    options = BrowserCdpOptions(
        mode="attached_cdp",
        endpoint="http://127.0.0.1:9222",
        attachment_timeout_ms=250,
        navigation_timeout_ms=1_500,
        operation_timeout_ms=750,
        max_redirects=3,
        page_discovery_limit=8,
    )

    session = await backend.open_session(options, destination_guard=_allow_destination)

    assert context.navigation_timeout == 1_500
    assert context.operation_timeout == 750
    await session.close()
    await backend.aclose()


async def test_omitted_external_page_navigation_bypasses_ricky_routing() -> None:
    guarded: list[str] = []

    async def guard(url: str) -> None:
        guarded.append(url)

    session = _PlaywrightSession(
        (),
        process_owned=False,
        pages_owned=False,
        close_owner=_noop_close,
        is_connected=lambda: True,
        destination_guard=guard,
        max_redirects=3,
        navigation_timeout_ms=1_000,
        operation_timeout_ms=1_000,
        page_discovery_limit=1,
    )
    cdp = _FakeNavigationCDP()
    session._navigation_cdp = cast(Any, cdp)
    await session._native_navigation(
        {
            "requestId": "request",
            "frameId": "omitted",
            "request": {"url": "http://10.0.0.1/manual"},
        }
    )
    assert cdp.commands[-1] == ("Fetch.continueRequest", {"requestId": "request"})
    assert guarded == []


async def test_every_post_saturation_external_popup_reports_overflow() -> None:
    class FakePage:
        context = _FakeNavigationCDP()

        def is_closed(self) -> bool:
            return False

        def on(self, _event: str, _handler: object) -> None:
            return None

    class FakeContext:
        def __init__(self) -> None:
            self.pages = [FakePage()]
            self.page_handler: Any = None

        browser = _FakeNavigationCDP()

        async def new_cdp_session(self, _page: object) -> _FakeNavigationCDP:
            return _FakeNavigationCDP()

        def on(self, event: str, handler: object) -> None:
            assert event == "page"
            self.page_handler = handler

    context = FakeContext()
    session = _PlaywrightSession(
        (cast(Any, context),),
        process_owned=False,
        pages_owned=False,
        close_owner=_noop_close,
        is_connected=lambda: True,
        destination_guard=_allow_destination,
        max_redirects=3,
        navigation_timeout_ms=1_000,
        operation_timeout_ms=1_000,
        page_discovery_limit=1,
    )
    await session.initialize()

    context.page_handler(FakePage())
    assert session.take_page_overflow_count() == 1
    context.page_handler(FakePage())
    assert session.take_page_overflow_count() == 1


async def test_playwright_session_close_failure_remains_retryable() -> None:
    attempts = 0

    async def close_owner() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("close failed")

    session = _PlaywrightSession(
        (),
        process_owned=True,
        pages_owned=True,
        close_owner=close_owner,
        is_connected=lambda: True,
        destination_guard=_allow_destination,
        max_redirects=3,
        navigation_timeout_ms=1_000,
        operation_timeout_ms=1_000,
        page_discovery_limit=1,
    )

    with pytest.raises(RuntimeError, match="close failed"):
        await session.close()
    assert session.connected
    with pytest.raises(BrowserError) as closing:
        await session.pages()
    assert closing.value.failure.code == "session_closed"

    await session.close()
    assert not session.connected
    assert attempts == 2


async def test_resource_catalog_is_scope_qualified_and_provider_safe(tmp_path: Path) -> None:
    settings = _settings(tmp_path, include_work=True)
    service, _backend = _service(settings, tmp_path)

    resources = await service.resources()

    assert [item.resource.qualified for item in resources.resources] == [
        "personal/debug",
        "personal/main",
    ]
    serialized = resources.model_dump_json()
    assert "9222" not in serialized
    assert "persistent" in serialized
    assert str(tmp_path) not in serialized
    with pytest.raises(BrowserError) as inaccessible:
        service.resource("work/main")
    assert inaccessible.value.failure.code == "unknown_resource"
    await service.aclose()
    assert not Path(settings.user_data_dir).exists()


async def test_persistent_resource_state_survives_close_and_can_be_reset(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, extra_persistent=True)
    service, backend = _service(settings, tmp_path)

    opened = await service.open_resource(
        "personal/main",
        headless=False,
    )
    [options] = backend.options
    assert isinstance(options, BrowserLaunchOptions)
    assert options.mode == "owned_persistent"
    assert options.headless is False
    assert options.page_discovery_limit == settings.browser.max_pages + 50
    expected = persistent_browser_path(
        settings,
        ProfileResourceRef(profile="personal", name="main"),
    )
    assert options.user_data_dir == expected
    marker = expected / "state-marker"
    marker.touch()
    secondary = persistent_browser_path(
        settings,
        ProfileResourceRef(profile="personal", name="secondary"),
    )
    secondary.mkdir(parents=True)
    secondary_marker = secondary / "must-remain"
    secondary_marker.touch()

    await service.close_session(opened.session_id)

    assert expected.is_dir()
    assert marker.is_file()
    assert not Path(settings.project_data_dir).exists()
    reset = await service.reset_resource("personal/main")
    assert reset.resource.qualified == "personal/main"
    assert not expected.exists()
    assert secondary_marker.is_file()
    await service.aclose()


async def test_configured_resource_lease_blocks_concurrent_owners_and_releases(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    first, _first_backend = _service(settings, tmp_path)
    second, _second_backend = _service(settings, tmp_path)
    opened = await first.open_resource("personal/main")

    with pytest.raises(BrowserError) as busy:
        await second.open_resource("personal/main")
    assert busy.value.failure.code == "resource_busy"
    with pytest.raises(BrowserError) as reset_busy:
        await second.reset_resource("personal/main")
    assert reset_busy.value.failure.code == "resource_busy"

    await first.close_session(opened.session_id)
    reopened = await second.open_resource("personal/main")
    assert reopened.resource.qualified == "personal/main"
    await second.close_session(reopened.session_id)
    await first.aclose()
    await second.aclose()


async def test_distinct_configured_resources_can_open_concurrently(tmp_path: Path) -> None:
    settings = _settings(tmp_path, extra_persistent=True)
    service, backend = _service(settings, tmp_path)

    main = await service.open_resource("personal/main")
    secondary = await service.open_resource("personal/secondary")

    assert main.resource.qualified == "personal/main"
    assert secondary.resource.qualified == "personal/secondary"
    assert len(backend.sessions) == 2
    await service.close_session(secondary.session_id)
    await service.close_session(main.session_id)
    await service.aclose()


async def test_failed_resource_launch_releases_lease(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    failing_backend = FakeBrowserBackend()
    failing_backend.open_error = RuntimeError("launch failed")
    failing, _ = _service(settings, tmp_path, failing_backend)

    with pytest.raises(RuntimeError, match="launch failed"):
        await failing.open_resource("personal/main")

    healthy, _ = _service(settings, tmp_path)
    opened = await healthy.open_resource("personal/main")
    await healthy.close_session(opened.session_id)
    await failing.aclose()
    await healthy.aclose()


async def test_cancelled_resource_launch_releases_lease(tmp_path: Path) -> None:
    class BlockingBackend(FakeBrowserBackend):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()

        async def open_session(
            self,
            options: BrowserOpenOptions,
            *,
            destination_guard: DestinationGuard,
        ) -> FakeBrowserSession:
            del options, destination_guard
            self.entered.set()
            await asyncio.Future()
            raise AssertionError("cancelled launch unexpectedly resumed")

    settings = _settings(tmp_path)
    backend = BlockingBackend()
    blocked, _ = _service(settings, tmp_path, backend)
    opening = asyncio.create_task(blocked.open_resource("personal/main"))
    await asyncio.wait_for(backend.entered.wait(), timeout=2)

    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening

    healthy, _ = _service(settings, tmp_path)
    reopened = await healthy.open_resource("personal/main")
    await healthy.close_session(reopened.session_id)
    await blocked.aclose()
    await healthy.aclose()


async def test_failed_persistent_initial_sync_closes_session_and_releases_lease(
    tmp_path: Path,
) -> None:
    class FailingInitialSync(FakeBrowserSession):
        async def pages(self) -> tuple[FakeBrowserPage, ...]:
            raise RuntimeError("persistent initial sync failed")

    settings = _settings(tmp_path)
    failed_session = FailingInitialSync()
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(failed_session)
    failed, _ = _service(settings, tmp_path, backend)

    with pytest.raises(RuntimeError, match="persistent initial sync failed"):
        await failed.open_resource("personal/main")

    assert failed_session.closed
    healthy, _ = _service(settings, tmp_path)
    reopened = await healthy.open_resource("personal/main")
    await healthy.close_session(reopened.session_id)
    await failed.aclose()
    await healthy.aclose()


async def test_cancelled_persistent_initial_sync_closes_session_and_releases_lease(
    tmp_path: Path,
) -> None:
    class BlockingInitialSync(FakeBrowserSession):
        def __init__(self) -> None:
            super().__init__()
            self.pages_entered = asyncio.Event()

        async def pages(self) -> tuple[FakeBrowserPage, ...]:
            self.pages_entered.set()
            await asyncio.Future[None]()
            raise AssertionError("cancelled persistent sync unexpectedly resumed")

    settings = _settings(tmp_path)
    blocked_session = BlockingInitialSync()
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(blocked_session)
    blocked, _ = _service(settings, tmp_path, backend)
    opening = asyncio.create_task(blocked.open_resource("personal/main"))
    await asyncio.wait_for(blocked_session.pages_entered.wait(), timeout=2)

    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening

    assert blocked_session.closed
    healthy, _ = _service(settings, tmp_path)
    reopened = await healthy.open_resource("personal/main")
    await healthy.close_session(reopened.session_id)
    await blocked.aclose()
    await healthy.aclose()


async def test_cdp_attachment_disconnects_without_closing_external_pages(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    page = FakeBrowserPage(url="https://example.com/", title="External")
    external = FakeBrowserSession([page], process_owned=False, pages_owned=False)
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(external)
    service, _ = _service(settings, tmp_path, backend)

    opened = await service.open_resource("personal/debug")
    [options] = backend.options
    assert isinstance(options, BrowserCdpOptions)
    assert options.endpoint == "http://127.0.0.1:9222"
    assert opened.mode == "attached_cdp"
    assert opened.process_owned is False
    assert opened.headless is None

    with pytest.raises(BrowserError) as handoff:
        await service.handoff(opened.session_id, page_id=None, reason="sso")
    assert handoff.value.failure.code == "handoff_required"

    await service.close_session(opened.session_id)

    assert external.closed
    assert external.close_calls == 1
    assert page.closed is False
    await service.aclose()


async def test_complete_cdp_initialization_is_bounded_and_releases_lease(
    tmp_path: Path,
) -> None:
    class SlowStatePage(FakeBrowserPage):
        def __init__(self) -> None:
            super().__init__(url="about:blank")
            self.entered = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def state(self) -> BackendPageState:
            self.entered.set()
            try:
                await asyncio.Future()
            finally:
                self.cancelled.set()
            raise AssertionError("cancelled state read unexpectedly resumed")

    settings = _settings(tmp_path, attachment_timeout_seconds=0.02)
    page = SlowStatePage()
    external = FakeBrowserSession([page], process_owned=False, pages_owned=False)
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(external)
    service, _ = _service(settings, tmp_path, backend)

    with pytest.raises(BrowserError) as timed_out:
        await service.open_resource("personal/debug")

    assert timed_out.value.failure.code == "attachment_timeout"
    assert page.entered.is_set()
    assert page.cancelled.is_set()
    assert external.closed

    healthy, healthy_backend = _service(settings, tmp_path)
    healthy_backend.pending_sessions.append(
        FakeBrowserSession(
            [FakeBrowserPage(url="about:blank")],
            process_owned=False,
            pages_owned=False,
        )
    )
    reopened = await healthy.open_resource("personal/debug")
    await healthy.close_session(reopened.session_id)
    await service.aclose()
    await healthy.aclose()


async def test_attachment_omits_policy_blocked_tabs_without_closing_them(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    eligible = FakeBrowserPage(
        "eligible",
        url="http://127.0.0.1:8765/allowed",
        title="Eligible",
    )
    blocked = FakeBrowserPage(
        "blocked",
        url="http://10.0.0.1/private",
        title="Blocked",
    )
    external = FakeBrowserSession(
        [eligible, blocked],
        process_owned=False,
        pages_owned=False,
    )
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(external)
    service, _ = _service(settings, tmp_path, backend)

    opened = await service.open_resource("personal/debug")

    assert [page.title for page in opened.pages] == ["Eligible"]
    assert blocked.quarantined
    assert blocked.closed is False
    await service.close_session(opened.session_id)
    assert blocked.closed is False
    await service.aclose()


async def test_failed_owned_close_keeps_resource_lease_until_retry_succeeds(
    tmp_path: Path,
) -> None:
    class AmbiguousCloseSession(FakeBrowserSession):
        fail_close = True

        async def close(self) -> None:
            if self.fail_close:
                self.close_calls += 1
                raise RuntimeError("owned Chrome close was ambiguous")
            await super().close()

    settings = _settings(tmp_path)
    ambiguous = AmbiguousCloseSession()
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(ambiguous)
    service, _ = _service(settings, tmp_path, backend)
    opened = await service.open_resource("personal/main")

    with pytest.raises(RuntimeError, match="close was ambiguous"):
        await service.close_session(opened.session_id)

    contender, _ = _service(settings, tmp_path)
    with pytest.raises(BrowserError) as busy:
        await contender.open_resource("personal/main")
    assert busy.value.failure.code == "resource_busy"

    ambiguous.fail_close = False
    await service.close_session(opened.session_id)
    reopened = await contender.open_resource("personal/main")
    await contender.close_session(reopened.session_id)
    await service.aclose()
    await contender.aclose()


async def test_attached_overflow_and_unsupported_pages_are_never_closed(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, max_pages=1)
    supported = FakeBrowserPage("page-1", url="https://example.com/", title="Supported")
    overflow = FakeBrowserPage("page-2", url="https://example.com/two", title="Overflow")
    internal = FakeBrowserPage("page-3", url="chrome://settings", title="Settings")
    external = FakeBrowserSession(
        [supported, overflow, internal],
        process_owned=False,
        pages_owned=False,
    )
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(external)
    service, _ = _service(settings, tmp_path, backend)

    with pytest.raises(BrowserError) as limited:
        await service.open_resource("personal/debug")

    assert limited.value.failure.code == "attached_page_limit"
    assert supported.closed is False
    assert overflow.closed is False
    assert internal.closed is False
    assert external.closed
    await service.aclose()


async def test_failed_attached_initial_sync_preserves_external_pages_and_releases_lease(
    tmp_path: Path,
) -> None:
    class FailingAttachedSession(FakeBrowserSession):
        async def pages(self):
            raise RuntimeError("attached page discovery failed")

    settings = _settings(tmp_path)
    page = FakeBrowserPage(url="https://example.com/", title="External")
    failed = FailingAttachedSession([page], process_owned=False, pages_owned=False)
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(failed)
    service, _ = _service(settings, tmp_path, backend)

    with pytest.raises(RuntimeError, match="page discovery failed"):
        await service.open_resource("personal/debug")

    assert failed.closed
    assert page.closed is False
    healthy, healthy_backend = _service(settings, tmp_path)
    healthy_backend.pending_sessions.append(
        FakeBrowserSession(
            [FakeBrowserPage(url="https://example.com/")],
            process_owned=False,
            pages_owned=False,
        )
    )
    reopened = await healthy.open_resource("personal/debug")
    await healthy.close_session(reopened.session_id)
    await service.aclose()
    await healthy.aclose()


async def test_cancelled_attached_initial_sync_preserves_external_page_and_releases_lease(
    tmp_path: Path,
) -> None:
    class BlockingAttachedSession(FakeBrowserSession):
        def __init__(self, page: FakeBrowserPage) -> None:
            super().__init__([page], process_owned=False, pages_owned=False)
            self.entered = asyncio.Event()

        async def pages(self):
            self.entered.set()
            await asyncio.Future()
            raise AssertionError("cancelled page discovery unexpectedly resumed")

    settings = _settings(tmp_path)
    page = FakeBrowserPage(url="https://example.com/", title="External")
    blocked_session = BlockingAttachedSession(page)
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(blocked_session)
    service, _ = _service(settings, tmp_path, backend)
    opening = asyncio.create_task(service.open_resource("personal/debug"))
    await asyncio.wait_for(blocked_session.entered.wait(), timeout=2)

    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening

    assert blocked_session.closed
    assert page.closed is False
    healthy, healthy_backend = _service(settings, tmp_path)
    healthy_backend.pending_sessions.append(
        FakeBrowserSession(
            [FakeBrowserPage(url="https://example.com/")],
            process_owned=False,
            pages_owned=False,
        )
    )
    reopened = await healthy.open_resource("personal/debug")
    await healthy.close_session(reopened.session_id)
    await service.aclose()
    await healthy.aclose()


async def test_action_time_attached_overflow_is_reported_and_left_open(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, max_pages=1)
    descriptor = BackendTargetDescriptor(
        ref="e1",
        role="button",
        name="Open popup",
        control_kind="button",
    )
    opener = FakeBrowserPage(
        url="https://example.com/",
        snapshot='- button "Open popup" [ref=e1]',
        targets=(descriptor,),
    )
    popup = FakeBrowserPage("popup", url="https://example.com/popup", title="Popup")
    opener.action_popups = [popup]
    external = FakeBrowserSession([opener], process_owned=False, pages_owned=False)
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(external)
    service, _ = _service(settings, tmp_path, backend)
    opened = await service.open_resource("personal/debug")
    snapshot = await service.snapshot(opened.session_id, page_id=None)

    result = await service.action(
        BrowserActionTarget(
            session_id=opened.session_id,
            page_id=snapshot.page.page_id,
            snapshot_id=snapshot.snapshot_id,
            ref="e1",
        ),
        BrowserActionRequest(kind="click"),
    )

    assert result.disposition == "performed"
    assert result.postcondition.observation_limited
    assert result.postcondition.page_changes.created_page_ids
    assert result.postcondition.page_changes.closed_page_ids == ()
    assert result.postcondition.observation_note is not None
    assert "left external pages open" in result.postcondition.observation_note
    assert popup.closed is False

    second_popup = FakeBrowserPage(
        "popup-2",
        url="https://example.com/popup-2",
        title="Second popup",
    )
    opener.action_popups = [second_popup]
    refreshed = await service.snapshot(
        opened.session_id,
        page_id=snapshot.page.page_id,
    )
    second = await service.action(
        BrowserActionTarget(
            session_id=opened.session_id,
            page_id=refreshed.page.page_id,
            snapshot_id=refreshed.snapshot_id,
            ref="e1",
        ),
        BrowserActionRequest(kind="click"),
    )

    assert second.postcondition.observation_limited
    assert second.postcondition.page_changes.created_page_ids
    assert second_popup.closed is False
    await service.aclose()


async def test_disconnect_after_action_dispatch_returns_in_doubt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    descriptor = BackendTargetDescriptor(
        ref="e1",
        role="button",
        name="Continue",
        control_kind="button",
    )

    class DisconnectingSession(FakeBrowserSession):
        disconnected = False

        async def pages(self):
            if self.disconnected:
                raise BrowserError(
                    BrowserFailure(
                        code="attachment_disconnected",
                        message="configured browser attachment disconnected",
                    )
                )
            return await super().pages()

    page = FakeBrowserPage(
        url="https://example.com/",
        title="External",
        snapshot='- button "Continue" [ref=e1]',
        targets=(descriptor,),
    )
    external = DisconnectingSession([page], process_owned=False, pages_owned=False)

    async def disconnect(
        _request: BackendActionRequest,
        before: BackendPageState,
    ) -> BackendActionOutcome:
        external.disconnected = True
        return BackendActionOutcome(
            disposition="performed",
            dispatch_state="completed",
            state_before=before,
            state_after=before,
        )

    page.action_handler = disconnect
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(external)
    service, _ = _service(settings, tmp_path, backend)
    opened = await service.open_resource("personal/debug")
    snapshot = await service.snapshot(opened.session_id, page_id=None)
    target = BrowserActionTarget(
        session_id=opened.session_id,
        page_id=snapshot.page.page_id,
        snapshot_id=snapshot.snapshot_id,
        ref="e1",
    )

    result = await service.action(target, BrowserActionRequest(kind="click"))

    assert result.disposition == "in_doubt"
    assert result.failure is not None
    assert result.failure.code == "attachment_disconnected"
    assert result.failure.outcome_uncertain
    assert result.postcondition.observation_limited
    await service.aclose()


async def test_disconnected_attachment_invalidates_targets_and_still_closes_cleanly(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    descriptor = BackendTargetDescriptor(
        ref="e1",
        role="button",
        name="Continue",
        control_kind="button",
    )
    page = FakeBrowserPage(
        url="https://example.com/",
        title="External",
        snapshot='- button "Continue" [ref=e1]',
        targets=(descriptor,),
    )
    external = FakeBrowserSession([page], process_owned=False, pages_owned=False)
    backend = FakeBrowserBackend()
    backend.pending_sessions.append(external)
    service, _ = _service(settings, tmp_path, backend)
    opened = await service.open_resource("personal/debug")
    snapshot = await service.snapshot(opened.session_id, page_id=None)
    target = BrowserActionTarget(
        session_id=opened.session_id,
        page_id=snapshot.page.page_id,
        snapshot_id=snapshot.snapshot_id,
        ref="e1",
    )
    external.connected = False

    with pytest.raises(BrowserError) as disconnected:
        service.action_context(target)
    assert disconnected.value.failure.code == "attachment_disconnected"
    await service.close_session(opened.session_id)

    assert external.closed
    assert page.closed is False
    await service.aclose()


def test_persistent_paths_and_leases_reject_symlink_escape(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    profile = Path(settings.user_data_dir) / "profiles" / "personal"
    profile.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (profile / "browser").symlink_to(outside, target_is_directory=True)
    ref = ProfileResourceRef(profile="personal", name="main")

    with pytest.raises(ValueError, match="escapes profile data directory"):
        persistent_browser_path(settings, ref)
    with pytest.raises(ValueError, match="escapes profile data directory"):
        BrowserResourceLease(settings, ref)

    assert list(outside.iterdir()) == []
    assert not Path(settings.project_data_dir).exists()


def test_persistent_paths_reject_symlink_alias_inside_profile(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    profile = Path(settings.user_data_dir) / "profiles" / "personal"
    browser = profile / "browser"
    target = profile / "other-browser-state"
    browser.mkdir(parents=True)
    target.mkdir()
    (browser / "persistent").symlink_to(target, target_is_directory=True)
    ref = ProfileResourceRef(profile="personal", name="main")

    with pytest.raises(ValueError, match="must not contain symlinks"):
        persistent_browser_path(settings, ref)

    assert list(target.iterdir()) == []


def test_lease_metadata_is_safe_and_stale_lock_files_are_reusable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ref = ProfileResourceRef(profile="personal", name="main")
    first = BrowserResourceLease(settings, ref)
    owner = first.acquire()

    assert owner.resource == ref
    assert first.path.read_text(encoding="utf-8").find("endpoint") == -1
    first.release()
    assert first.path.exists()

    second = BrowserResourceLease(settings, ref)
    second.acquire()
    assert second.held
    second.release()


@pytest.mark.skipif(os.name != "posix", reason="POSIX flock process-death contract")
def test_process_death_releases_live_resource_lease(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    ref = ProfileResourceRef(profile="personal", name="main")
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - assertions run in the parent process.
        try:
            os.close(read_fd)
            lease = BrowserResourceLease(settings, ref)
            lease.acquire()
            os.write(write_fd, b"ready")
            signal.pause()
        finally:
            os._exit(0)

    os.close(write_fd)
    try:
        assert os.read(read_fd, 5) == b"ready"
        with pytest.raises(BrowserError) as busy:
            BrowserResourceLease(settings, ref).acquire()
        assert busy.value.failure.code == "resource_busy"

        os.kill(child_pid, signal.SIGKILL)
        waited_pid, _status = os.waitpid(child_pid, 0)
        assert waited_pid == child_pid
        child_pid = 0

        recovered = BrowserResourceLease(settings, ref)
        recovered.acquire()
        recovered.release()
    finally:
        os.close(read_fd)
        if child_pid:
            os.kill(child_pid, signal.SIGKILL)
            os.waitpid(child_pid, 0)
