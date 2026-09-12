"""Deterministic in-process browser backend used by browser unit tests."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from ricky.browser.backend import (
    BackendActionOutcome,
    BackendActionPreflight,
    BackendActionRequest,
    BackendCoordinatePreflight,
    BackendCoordinateRequest,
    BackendDownloadOutcome,
    BackendPageState,
    BackendProtectedFillRequest,
    BackendSnapshot,
    BackendTargetDescriptor,
    BackendUploadFile,
    BackendVisualSnapshot,
    BrowserOpenOptions,
    DestinationGuard,
)
from ricky.browser.types import BrowserError, BrowserFailure

_REF = re.compile(r"\[ref=(e[0-9]+)\]")
type FakeActionHandler = Callable[
    [BackendActionRequest, BackendPageState], Awaitable[BackendActionOutcome]
]


class FakeBrowserPage:
    """Small controllable page handle with observable call ordering."""

    def __init__(
        self,
        key: str = "page-1",
        *,
        url: str = "about:blank",
        title: str = "",
        snapshot: str = "- document",
        targets: tuple[BackendTargetDescriptor, ...] | None = None,
    ) -> None:
        self._key = key
        self.url = url
        self.title = title
        self.snapshot_text = snapshot
        inferred_refs = tuple(dict.fromkeys(_REF.findall(snapshot)))
        self.targets = (
            targets
            if targets is not None
            else tuple(BackendTargetDescriptor(ref=ref) for ref in inferred_refs)
        )
        self.closed = False
        self.quarantined = False
        self.front_calls = 0
        self.navigations: list[str] = []
        self.scrolls: list[int] = []
        self.snapshot_depths: list[int] = []
        self.snapshot_character_limits: list[int] = []
        self.preflights: list[BackendActionRequest] = []
        self.effective_destinations: tuple[str, ...] = ()
        self.financial_signal = False
        self.actions: list[BackendActionRequest] = []
        self.protected_fills: list[BackendProtectedFillRequest] = []
        self.protected_fill_entered: asyncio.Event | None = None
        self.protected_fill_release: asyncio.Event | None = None
        self.operations: list[str] = []
        self.destination_guard: DestinationGuard | None = None
        self.navigate_entered: asyncio.Event | None = None
        self.navigate_release: asyncio.Event | None = None
        self.action_entered: asyncio.Event | None = None
        self.action_release: asyncio.Event | None = None
        self.preflight_error: BaseException | None = None
        self.action_error: BaseException | None = None
        self.action_handler: FakeActionHandler | None = None
        self.action_outcomes: list[BackendActionOutcome] = []
        self.action_popups: list[FakeBrowserPage] = []
        self.visual_capture: BackendVisualSnapshot | None = None
        self.uploads: list[tuple[BackendActionRequest, tuple[BackendUploadFile, ...]]] = []
        self.upload_outcomes: list[BackendActionOutcome] = []
        self.upload_entered: asyncio.Event | None = None
        self.upload_release: asyncio.Event | None = None
        self.downloads: list[BackendActionRequest] = []
        self.download_outcomes: list[BackendDownloadOutcome] = []
        self.download_entered: asyncio.Event | None = None
        self.download_release: asyncio.Event | None = None
        self.coordinates: list[BackendCoordinateRequest] = []
        self.coordinate_preflights: list[BackendCoordinateRequest] = []
        self.coordinate_target = BackendTargetDescriptor(ref="d0", role="other")
        self.coordinate_destinations: tuple[str, ...] = ()
        self.coordinate_financial_signal = False
        self.coordinate_equivalent_semantic_ref: str | None = None
        self.coordinate_outcomes: list[BackendActionOutcome] = []
        self.coordinate_entered: asyncio.Event | None = None
        self.coordinate_release: asyncio.Event | None = None
        self._session: FakeBrowserSession | None = None

    @property
    def key(self) -> str:
        return self._key

    async def state(self) -> BackendPageState:
        return BackendPageState(
            key=self.key,
            url=self.url if not self.closed and not self.quarantined else "[closed]",
            title=self.title,
            closed=self.closed or self.quarantined,
        )

    async def bring_to_front(self) -> None:
        self.front_calls += 1
        self.operations.append("select")

    async def navigate(self, url: str) -> BackendPageState:
        self.operations.append("navigate:start")
        self.navigations.append(url)
        if self.destination_guard is not None:
            await self.destination_guard(url)
        if self.navigate_entered is not None:
            self.navigate_entered.set()
        if self.navigate_release is not None:
            await self.navigate_release.wait()
        self.url = url
        self.title = "Navigated"
        self.operations.append("navigate:end")
        return await self.state()

    async def scroll(self, delta_y: int) -> BackendPageState:
        self.operations.append("scroll")
        self.scrolls.append(delta_y)
        return await self.state()

    async def snapshot(self, *, depth: int, character_limit: int) -> BackendSnapshot:
        self.operations.append("snapshot")
        self.snapshot_depths.append(depth)
        self.snapshot_character_limits.append(character_limit)
        return BackendSnapshot(content=self.snapshot_text, targets=self.targets)

    async def visual_snapshot(self, *, candidate_limit: int) -> BackendVisualSnapshot:
        self.operations.append(f"visual:{candidate_limit}")
        if self.visual_capture is None:
            raise AssertionError("fake visual capture was not configured")
        return self.visual_capture

    async def preflight_action(
        self,
        request: BackendActionRequest,
    ) -> BackendActionPreflight:
        self.operations.append("preflight")
        self.preflights.append(request)
        if self.preflight_error is not None:
            raise self.preflight_error
        candidates = tuple(target for target in self.targets if target.ref == request.target.ref)
        if not candidates:
            raise BrowserError(
                BrowserFailure(code="invented_target", message="unknown browser target ref")
            )
        if len(candidates) != 1:
            raise BrowserError(
                BrowserFailure(code="ambiguous_target", message="ambiguous browser target ref")
            )
        target = candidates[0]
        if target != request.target:
            raise BrowserError(
                BrowserFailure(
                    code="incompatible_target",
                    message="browser target facts changed before dispatch",
                )
            )
        if self.destination_guard is not None:
            for destination in self.effective_destinations:
                await self.destination_guard(destination)
        return BackendActionPreflight(
            target=target,
            effective_destinations=self.effective_destinations,
            financial_signal=self.financial_signal,
        )

    async def perform_action(self, request: BackendActionRequest) -> BackendActionOutcome:
        if request.expected_preflight is not None:
            candidates = tuple(
                target for target in self.targets if target.ref == request.target.ref
            )
            current = (
                BackendActionPreflight(
                    target=candidates[0],
                    effective_destinations=self.effective_destinations,
                    financial_signal=self.financial_signal,
                )
                if len(candidates) == 1
                else None
            )
            if current != request.expected_preflight:
                state = await self.state()
                return BackendActionOutcome(
                    disposition="not_performed",
                    dispatch_state="not_dispatched",
                    state_before=state,
                    state_after=state,
                    failure=BrowserFailure(
                        code="stale_target",
                        message="browser target or destination changed before action dispatch",
                    ),
                )
        self.operations.append("action:start")
        self.actions.append(request)
        state_before = await self.state()
        if self.action_entered is not None:
            self.action_entered.set()
        if self.action_release is not None:
            await self.action_release.wait()
        if self.action_error is not None:
            raise self.action_error
        if self.action_handler is not None:
            outcome = await self.action_handler(request, state_before)
        elif self.action_outcomes:
            outcome = self.action_outcomes.pop(0)
        else:
            outcome = BackendActionOutcome(
                disposition="performed",
                dispatch_state="completed",
                state_before=state_before,
                state_after=await self.state(),
            )
        if self._session is not None and self.action_popups:
            for popup in self.action_popups:
                popup.destination_guard = self.destination_guard
                popup._session = self._session
                self._session.page_handles.append(popup)
            self.action_popups = []
        self.operations.append("action:end")
        return outcome

    async def preflight_protected_target(
        self, target: BackendTargetDescriptor
    ) -> BackendTargetDescriptor:
        self.operations.append("protected:preflight")
        candidates = tuple(item for item in self.targets if item.ref == target.ref)
        if len(candidates) != 1 or candidates[0] != target:
            raise BrowserError(
                BrowserFailure(
                    code="stale_target",
                    message="protected browser target facts changed before dispatch",
                )
            )
        return candidates[0]

    async def perform_protected_fill(
        self, request: BackendProtectedFillRequest
    ) -> BackendActionOutcome:
        self.operations.append("protected:start")
        self.protected_fills.append(request)
        state = await self.state()
        if self.protected_fill_entered is not None:
            self.protected_fill_entered.set()
        if self.protected_fill_release is not None:
            await self.protected_fill_release.wait()
        self.operations.append("protected:end")
        return BackendActionOutcome(
            disposition="performed",
            dispatch_state="completed",
            state_before=state,
            state_after=await self.state(),
        )

    async def perform_upload(
        self,
        request: BackendActionRequest,
        files: tuple[BackendUploadFile, ...],
    ) -> BackendActionOutcome:
        self.uploads.append((request, files))
        if self.upload_entered is not None:
            self.upload_entered.set()
        if self.upload_release is not None:
            await self.upload_release.wait()
        state = await self.state()
        return (
            self.upload_outcomes.pop(0)
            if self.upload_outcomes
            else BackendActionOutcome(
                disposition="performed",
                dispatch_state="completed",
                state_before=state,
                state_after=state,
            )
        )

    async def perform_download(self, request: BackendActionRequest) -> BackendDownloadOutcome:
        self.downloads.append(request)
        if self.download_entered is not None:
            self.download_entered.set()
        if self.download_release is not None:
            await self.download_release.wait()
        state = await self.state()
        return (
            self.download_outcomes.pop(0)
            if self.download_outcomes
            else BackendDownloadOutcome(
                action=BackendActionOutcome(
                    disposition="not_performed",
                    dispatch_state="not_dispatched",
                    state_before=state,
                    state_after=state,
                    failure=BrowserFailure(
                        code="download_unavailable",
                        message="fake download was not configured",
                    ),
                )
            )
        )

    async def preflight_coordinate_commit(
        self,
        request: BackendCoordinateRequest,
    ) -> BackendCoordinatePreflight:
        self.coordinate_preflights.append(request)
        if self.destination_guard is not None:
            for destination in self.coordinate_destinations:
                await self.destination_guard(destination)
        return BackendCoordinatePreflight(
            target=self.coordinate_target,
            effective_destinations=self.coordinate_destinations,
            financial_signal=self.coordinate_financial_signal,
            equivalent_semantic_ref=self.coordinate_equivalent_semantic_ref,
        )

    async def perform_coordinate_commit(
        self,
        request: BackendCoordinateRequest,
        *,
        expected: BackendCoordinatePreflight | None = None,
    ) -> BackendActionOutcome:
        if expected is not None:
            current = BackendCoordinatePreflight(
                target=self.coordinate_target,
                effective_destinations=self.coordinate_destinations,
                financial_signal=self.coordinate_financial_signal,
                equivalent_semantic_ref=self.coordinate_equivalent_semantic_ref,
            )
            if current != expected:
                state = await self.state()
                return BackendActionOutcome(
                    disposition="not_performed",
                    dispatch_state="not_dispatched",
                    state_before=state,
                    state_after=state,
                    failure=BrowserFailure(
                        code="stale_target",
                        message=(
                            "browser coordinate target or destination changed before dispatch"
                        ),
                    ),
                )
        self.coordinates.append(request)
        if self.coordinate_entered is not None:
            self.coordinate_entered.set()
        if self.coordinate_release is not None:
            await self.coordinate_release.wait()
        state = await self.state()
        return (
            self.coordinate_outcomes.pop(0)
            if self.coordinate_outcomes
            else BackendActionOutcome(
                disposition="performed",
                dispatch_state="completed",
                state_before=state,
                state_after=state,
            )
        )

    async def close(self) -> None:
        self.operations.append("close")
        if self._session is not None and not self._session.pages_owned:
            self.quarantined = True
            return
        self.closed = True


class FakeBrowserSession:
    """Ricky-owned session handle exposing a mutable page collection."""

    def __init__(
        self,
        pages: list[FakeBrowserPage] | None = None,
        *,
        process_owned: bool = True,
        pages_owned: bool = True,
    ) -> None:
        self.process_owned = process_owned
        self.pages_owned = pages_owned
        self.page_handles = pages or [FakeBrowserPage()]
        for page in self.page_handles:
            page._session = self
        self.closed = False
        self.connected = True
        self.close_calls = 0
        self.page_overflow_count = 0

    async def pages(self) -> tuple[FakeBrowserPage, ...]:
        return tuple(page for page in self.page_handles if not page.quarantined)

    def take_page_overflow_count(self) -> int:
        count, self.page_overflow_count = self.page_overflow_count, 0
        return count

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        self.connected = False
        if self.pages_owned:
            for page in self.page_handles:
                await page.close()


class FakeBrowserBackend:
    """Backend recording launch policy without starting a browser process."""

    def __init__(self) -> None:
        self.options: list[BrowserOpenOptions] = []
        self.sessions: list[FakeBrowserSession] = []
        self.pending_sessions: list[FakeBrowserSession] = []
        self.open_error: BaseException | None = None
        self.close_calls = 0
        self.closed = False
        self.close_entered: asyncio.Event | None = None
        self.close_release: asyncio.Event | None = None

    async def open_session(
        self,
        options: BrowserOpenOptions,
        *,
        destination_guard: DestinationGuard,
    ) -> FakeBrowserSession:
        self.options.append(options)
        if self.open_error is not None:
            raise self.open_error
        session = self.pending_sessions.pop(0) if self.pending_sessions else FakeBrowserSession()
        for page in session.page_handles:
            page.destination_guard = destination_guard
        self.sessions.append(session)
        return session

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.close_entered is not None:
            self.close_entered.set()
        if self.close_release is not None:
            await self.close_release.wait()
        for session in reversed(self.sessions):
            await session.close()
        self.closed = True


def fake_executable(tmp_path: Path) -> Path:
    """Return a harmless existing path for launch-option assertions."""

    path = tmp_path / "fake-chrome"
    path.touch()
    return path
