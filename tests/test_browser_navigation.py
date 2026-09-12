"""Native Chrome request and response enforcement without secondary HTTP fetches."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from ricky.browser.playwright_backend import _PlaywrightSession
from ricky.browser.types import BrowserError, BrowserFailure


class Protocol:
    def __init__(self) -> None:
        self.commands: list[tuple[str, dict[str, object]]] = []

    async def send(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.commands.append((method, params))
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"type": "page"}}
        return {}


async def noop() -> None:
    pass


def pause(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "requestId": "request",
        "frameId": "popup",
        "request": {"url": "https://example.com/start"},
    }
    result.update(changes)
    return result


def session(guard: Any) -> tuple[_PlaywrightSession, Protocol]:
    owner = _PlaywrightSession(
        (),
        process_owned=True,
        pages_owned=True,
        close_owner=noop,
        is_connected=lambda: True,
        destination_guard=guard,
        max_redirects=1,
        navigation_timeout_ms=500,
        operation_timeout_ms=500,
        page_discovery_limit=2,
        download_file_byte_limit=10,
    )
    protocol = Protocol()
    owner._navigation_cdp = cast(Any, protocol)
    return owner, protocol


async def allow(_url: str) -> None:
    pass


async def test_native_navigation_continues_chrome_request_and_response() -> None:
    urls: list[str] = []

    async def guard(url: str) -> None:
        urls.append(url)

    owner, protocol = session(guard)
    await owner._native_navigation(pause())
    await owner._native_navigation(pause(responseStatusCode=200))
    assert urls == ["https://example.com/start"]
    assert [method for method, _ in protocol.commands] == [
        "Target.getTargetInfo",
        "Fetch.continueRequest",
        "Target.getTargetInfo",
        "Fetch.continueRequest",
    ]


async def test_redirect_destination_is_rejected_before_chrome_follows_it() -> None:
    async def guard(url: str) -> None:
        if "private" in url:
            raise BrowserError(BrowserFailure(code="destination_blocked", message="blocked"))

    owner, protocol = session(guard)
    await owner._native_navigation(
        pause(
            responseStatusCode=302,
            responseHeaders=[
                {"name": "Location", "value": "https://private.example/"},
            ],
        )
    )
    assert protocol.commands[-1][0] == "Fetch.failRequest"
    assert owner._unattributed_blocked is not None
    assert owner._unattributed_blocked.failure.code == "destination_blocked"


async def test_native_redirect_chain_is_bounded() -> None:
    owner, protocol = session(allow)
    await owner._native_navigation(pause())
    redirect = pause(
        responseStatusCode=302, responseHeaders=[{"name": "location", "value": "/next"}]
    )
    await owner._native_navigation(redirect)
    assert protocol.commands[-1][0] == "Fetch.continueRequest"
    await owner._native_navigation(pause(requestId="second", redirectedRequestId="request"))
    await owner._native_navigation(redirect)
    assert protocol.commands[-1][0] == "Fetch.failRequest"
    assert owner._unattributed_blocked is not None
    assert owner._unattributed_blocked.failure.message == "navigation exceeded the redirect limit"


@pytest.mark.parametrize(
    "explicit,expected", [(False, "download_blocked"), (True, "download_too_large")]
)
async def test_attachment_response_is_rejected_before_body_transfer(
    explicit: bool, expected: str
) -> None:
    owner, protocol = session(allow)
    if explicit:
        owner._explicit_download_pages.add(cast(Any, object()))
    await owner._native_navigation(
        pause(
            responseStatusCode=200,
            responseHeaders=[
                {"name": "Content-Disposition", "value": "attachment; filename=example"},
                {"name": "Content-Length", "value": "11"},
            ],
        )
    )
    assert protocol.commands[-1][0] == "Fetch.failRequest"
    assert owner._unattributed_blocked is not None
    assert owner._unattributed_blocked.failure.code == expected


async def test_rewritten_popup_destination_is_blocked_before_dispatch() -> None:
    owner, protocol = session(allow)
    page = cast(Any, object())
    owner.begin_action(page, reviewed_destinations=("https://example.com/reviewed",))
    await owner._native_navigation(pause())
    assert protocol.commands[-1][0] == "Fetch.failRequest"
    assert owner._blocked[page].failure.outcome_uncertain


async def test_malformed_response_headers_fail_closed_without_raw_diagnostics() -> None:
    owner, protocol = session(allow)
    await owner._native_navigation(
        pause(
            responseStatusCode=200,
            responseHeaders=[
                {"name": "Content-Disposition", "value": {"credential": "private"}},
            ],
        )
    )
    assert protocol.commands[-1][0] == "Fetch.failRequest"
    assert owner._unattributed_blocked is not None
    assert "private" not in owner._unattributed_blocked.failure.model_dump_json()


async def test_cancelled_destination_validation_aborts_the_paused_request() -> None:
    entered = asyncio.Event()

    async def guard(_url: str) -> None:
        entered.set()
        await asyncio.Event().wait()

    owner, protocol = session(guard)
    task = asyncio.create_task(owner._native_navigation(pause()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert protocol.commands[-1] == (
        "Fetch.failRequest",
        {"requestId": "request", "errorReason": "Aborted"},
    )


async def test_second_navigation_resets_the_redirect_budget() -> None:
    owner, protocol = session(allow)
    response = pause(
        responseStatusCode=302, responseHeaders=[{"name": "location", "value": "/next"}]
    )
    await owner._native_navigation(pause())
    await owner._native_navigation(response)
    await owner._native_navigation(pause(requestId="new-navigation"))
    await owner._native_navigation(response)
    assert protocol.commands[-1][0] == "Fetch.continueRequest"
    assert owner._unattributed_blocked is None


async def test_redirect_does_not_reapply_first_action_destination_binding() -> None:
    owner, protocol = session(allow)
    page = cast(Any, object())
    owner.begin_action(page, reviewed_destinations=("https://example.com/start",))
    await owner._native_navigation(pause())
    await owner._native_navigation(
        pause(
            requestId="redirected",
            redirectedRequestId="request",
            request={"url": "https://example.com/after-login"},
        )
    )
    assert protocol.commands[-1][0] == "Fetch.continueRequest"
    assert owner._blocked == {}


async def test_shutdown_aborts_pending_navigation_before_detaching() -> None:
    class CloseProtocol(Protocol):
        async def send(
            self, method: str, params: dict[str, object] | None = None
        ) -> dict[str, object]:
            return await super().send(method, params or {})

        async def detach(self) -> None:
            self.commands.append(("detach", {}))

        def remove_listener(self, event: str, handler: object) -> None:
            pass

    entered = asyncio.Event()

    async def guard(_url: str) -> None:
        entered.set()
        await asyncio.Event().wait()

    owner, _ = session(guard)
    protocol = CloseProtocol()
    owner._navigation_cdp = cast(Any, protocol)
    owner._observe_navigation_pause(pause())
    await entered.wait()
    await owner.close()
    assert [method for method, _ in protocol.commands][-3:] == [
        "Fetch.failRequest",
        "Fetch.disable",
        "detach",
    ]
    assert not owner._navigation_tasks
    assert not owner.connected


@pytest.mark.parametrize(
    "message,expected_method",
    [
        (
            "CDPSession.send: Protocol error (Target.getTargetInfo): No target with given id found",
            "Fetch.continueRequest",
        ),
        ("CDPSession.send: Session closed", "Fetch.failRequest"),
    ],
)
async def test_omitted_child_frame_lookup_is_distinct_from_protocol_failure(
    message: str,
    expected_method: str,
) -> None:
    from playwright.async_api import Error as PlaywrightError

    class LookupProtocol(Protocol):
        async def send(self, method: str, params: dict[str, object]) -> dict[str, object]:
            if method == "Target.getTargetInfo":
                raise PlaywrightError(message)
            return await super().send(method, params)

    guarded: list[str] = []

    async def guard(url: str) -> None:
        guarded.append(url)

    owner, _ = session(guard)
    owner.pages_owned = False
    protocol = LookupProtocol()
    owner._navigation_cdp = cast(Any, protocol)
    await owner._native_navigation(pause())
    assert protocol.commands[-1][0] == expected_method
    assert guarded == []


@pytest.mark.parametrize("cancel", [False, True], ids=["enable-failure", "enable-cancelled"])
async def test_partial_initialization_joins_paused_requests_without_closing_external_contexts(
    cancel: bool,
) -> None:
    from playwright.async_api import Error as PlaywrightError

    entered = asyncio.Event()
    guarding = asyncio.Event()
    owner_closed = False

    async def guard(_url: str) -> None:
        guarding.set()
        await asyncio.Event().wait()

    async def close_owner() -> None:
        nonlocal owner_closed
        owner_closed = True

    owner, _ = session(guard)
    owner._close_owner = close_owner
    owner.pages_owned = False
    owner.process_owned = False

    class StartupProtocol(Protocol):
        def on(self, _event: str, _handler: object) -> None:
            pass

        def remove_listener(self, _event: str, _handler: object) -> None:
            pass

        async def detach(self) -> None:
            self.commands.append(("detach", {}))

        async def new_browser_cdp_session(self) -> StartupProtocol:
            return self

        async def send(
            self, method: str, params: dict[str, object] | None = None
        ) -> dict[str, object]:
            if method == "Fetch.enable":
                # A real browser may deliver a pause before enable is acknowledged.
                owner._observe_navigation_pause(pause())
                await guarding.wait()
                entered.set()
                if cancel:
                    await asyncio.Event().wait()
                raise PlaywrightError("enable failed")
            return await super().send(method, params or {})

    class Context:
        pages: list[object] = []

        def __init__(self, protocol: StartupProtocol) -> None:
            self.browser = protocol

        def on(self, _event: str, _handler: object) -> None:
            pass

    protocol = StartupProtocol()
    owner._contexts = (cast(Any, Context(protocol)),)
    # Mark one synthetic active owner so the new popup is a controlled destination.
    active = cast(Any, object())
    owner._frame_pages["opener"] = active
    owner._active_actions.add(active)

    async def controlled_send(
        method: str, params: dict[str, object] | None = None
    ) -> dict[str, object]:
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"type": "page", "openerId": "opener"}}
        return await original_send(method, params)

    original_send = protocol.send
    protocol.send = controlled_send
    opening = asyncio.create_task(owner.initialize())
    await entered.wait()
    if cancel:
        opening.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else PlaywrightError):
        await opening
    assert not owner._navigation_tasks
    assert owner._navigation_cdp is None
    assert not owner_closed
    assert [method for method, _ in protocol.commands][-3:] == [
        "Fetch.failRequest",
        "Fetch.disable",
        "detach",
    ]
