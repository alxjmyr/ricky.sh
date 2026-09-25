"""Browser-service integration for bounded automatic verification input."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from functools import wraps
from typing import TYPE_CHECKING, Any, Concatenate

from ricky.browser.backend import BackendCoordinateRequest
from ricky.browser.holds import BrowserHoldOwner, BrowserHoldStatus, HoldStopReason
from ricky.browser.types import (
    BrowserActionTarget,
    BrowserCoordinateTarget,
    BrowserError,
    BrowserFailure,
)

if TYPE_CHECKING:
    from ricky.browser.service import BrowserService


def release_on_observation_error[T, **P](
    operation: Callable[Concatenate[BrowserService, P], Awaitable[T]],
) -> Callable[Concatenate[BrowserService, P], Coroutine[Any, Any, T]]:
    @wraps(operation)
    async def guarded(service: BrowserService, *args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return await operation(service, *args, **kwargs)
        except BaseException:
            await service.holds.stop_all("observation_failed")
            raise

    return guarded


class BrowserHoldController:
    def __init__(self, service: BrowserService) -> None:
        self._service = service
        self._owners: dict[str, BrowserHoldOwner] = {}
        self._attempts: dict[tuple[str, str, int], int] = {}
        self._reserved_seconds = 0.0
        self._starting = False
        self._uncertain = False
        self._lock = asyncio.Lock()

    def coordinate_target(self, target: BrowserActionTarget) -> BrowserCoordinateTarget:
        """Resolve a disclosed visual candidate locally; models never convert its scale."""
        service = self._service
        entry = service._require_session(target.session_id)
        page = service._page_entry(entry, target.page_id)
        service._cached_target(page, target)
        visual = page.visual
        if visual is None or visual.snapshot_id != target.snapshot_id:
            raise BrowserError(
                BrowserFailure(
                    code="stale_target", message="hold requires a fresh visual candidate"
                )
            )
        candidate = next(
            (item for item in visual.candidates if item.descriptor.ref == target.ref), None
        )
        if candidate is None:
            raise BrowserError(
                BrowserFailure(
                    code="stale_target", message="hold target is absent from the visual snapshot"
                )
            )
        box = candidate.bounding_box
        # Use the center of the visible portion, including partially clipped controls.
        left, top = max(0.0, box.x), max(0.0, box.y)
        right = min(float(visual.viewport.width), box.x + box.width)
        bottom = min(float(visual.viewport.height), box.y + box.height)
        if right <= left or bottom <= top:
            raise BrowserError(
                BrowserFailure(code="stale_target", message="hold target is outside the viewport")
            )
        return BrowserCoordinateTarget(
            session_id=target.session_id,
            page_id=target.page_id,
            screenshot_id=target.snapshot_id,
            x=(left + right) / 2 * visual.composed.width / visual.viewport.width,
            y=(top + bottom) / 2 * visual.composed.height / visual.viewport.height,
        )

    def check_operation(self, tool_name: str) -> None:
        active = (
            self._starting
            or self._uncertain
            or any(
                owner.status().state in {"holding", "in_doubt"} for owner in self._owners.values()
            )
        )
        if active and tool_name not in {
            "browser_hold_start",
            "browser_hold_status",
            "browser_hold_release",
            "browser_snapshot",
            "browser_visual_snapshot",
            "browser_pages",
            "browser_resources",
            "browser_session_close",
        }:
            raise BrowserError(
                BrowserFailure(
                    code="incompatible_target",
                    message="release the active verification hold before another browser action",
                )
            )

    async def start(
        self, target: BrowserActionTarget | BrowserCoordinateTarget, *, provider: str
    ) -> BrowserHoldStatus:
        service = self._service
        async with self._lock:
            if self._uncertain or any(
                owner.status().state != "released" for owner in self._owners.values()
            ):
                raise BrowserError(
                    BrowserFailure(
                        code="incompatible_target",
                        message="a hold is already active or its release is uncertain",
                    )
                )
            verification_ref = target.ref if isinstance(target, BrowserActionTarget) else None
            if isinstance(target, BrowserActionTarget):
                target = self.coordinate_target(target)
            entry, page = await service._require_page(target.session_id, target.page_id)
            if not entry.handle.process_owned:
                raise BrowserError(
                    BrowserFailure(
                        code="incompatible_target",
                        message="verification holds require a Ricky-owned browser",
                    )
                )
            settings = service._settings.browser
            episode = (entry.id, page.id, page.generation)
            duration = min(
                settings.hold_max_seconds, settings.hold_total_seconds - self._reserved_seconds
            )
            if duration <= 0 or self._attempts.get(episode, 0) >= settings.hold_attempt_limit:
                raise BrowserError(
                    BrowserFailure(
                        code="incompatible_target",
                        message=(
                            "verification hold budget exhausted; report the unresolved challenge"
                        ),
                    )
                )
            self._starting = True
            try:
                async with entry.action_lock, page.lock:
                    service._require_screenshot_disclosure(entry, provider)
                    context = service.coordinate_context(target)
                    visual = page.visual
                    assert visual is not None
                    request = BackendCoordinateRequest(
                        action_id=f"browser_action_{uuid.uuid4().hex}",
                        x=context.css_x,
                        y=context.css_y,
                        masked_base_sha256=context.masked_base_sha256,
                        viewport=visual.viewport,
                        verification_ref=verification_ref,
                    )
                    preflight = await page.handle.preflight_verification_hold(request)
                    facts = service._guard_facts(
                        "browser_hold_start",
                        entry=entry,
                        page=page,
                        provider=provider,
                        snapshot_id=target.screenshot_id,
                        target_frame_origin=preflight.target.frame_origin,
                        effective_destinations=preflight.effective_destinations,
                        action_kind="verification_hold",
                    )
                    await service._check_guard(facts)
                    await service._reserve_action_budgets(
                        entry, facts, (("verification_attempts", 1),)
                    )
                    self._attempts[episode] = self._attempts.get(episode, 0) + 1
                    self._reserved_seconds += duration
                    service._invalidate_snapshot(page)
                    before_pages = set(entry.pages_by_key)
                    try:
                        handle = await page.handle.start_verification_hold(
                            request, expected=preflight
                        )
                    except BaseException as exc:
                        self._uncertain = (
                            not isinstance(exc, BrowserError) or exc.failure.outcome_uncertain
                        )
                        await service._record_guard(
                            facts,
                            disposition="in_doubt" if self._uncertain else "not_performed",
                            action_id=request.action_id,
                            failure=exc.failure
                            if isinstance(exc, BrowserError)
                            else BrowserFailure(
                                code="action_in_doubt",
                                message="verification input was interrupted during startup",
                                outcome_uncertain=True,
                            ),
                        )
                        raise

                    async def release() -> None:
                        result = await handle.release()
                        service._invalidate_snapshot(page)
                        if result.navigation_occurred:
                            page.generation += 1
                            page.protected_uses.clear()
                        synced = await service._sync_pages(entry, discard_blocked_owned=True)
                        await service._record_guard(
                            facts,
                            disposition=result.disposition,
                            action_id=request.action_id,
                            failure=result.failure,
                            input_lifecycle="released",
                            created_page_count=(
                                len(set(entry.pages_by_key) - before_pages)
                                + synced.discarded_page_count
                            ),
                        )
                        if result.disposition == "in_doubt":
                            raise BrowserError(
                                result.failure
                                or BrowserFailure(
                                    code="action_in_doubt",
                                    message="verification release outcome is uncertain",
                                )
                            )

                    async def live() -> bool:
                        await service._check_guard(facts)
                        return await handle.live()

                    owner = BrowserHoldOwner(
                        session_id=entry.id,
                        page_id=page.id,
                        maximum_seconds=duration,
                        release=release,
                        live=live,
                    )
                    self._owners[owner.id] = owner
                    try:
                        await service._record_guard(
                            facts,
                            disposition="performed",
                            action_id=request.action_id,
                            input_lifecycle="started",
                        )
                    except BaseException:
                        await owner.stop("cancelled")
                        raise
                    return owner.status()
            finally:
                self._starting = False

    def status(self, hold_id: str) -> BrowserHoldStatus:
        return self._owner(hold_id).status()

    def is_holding(self, session_id: str, page_id: str) -> bool:
        return any(
            owner.session_id == session_id
            and owner.page_id == page_id
            and owner.status().state == "holding"
            for owner in self._owners.values()
        )

    async def release(self, hold_id: str) -> BrowserHoldStatus:
        return await self._owner(hold_id).stop("released")

    async def stop_all(
        self, reason: HoldStopReason = "cancelled", *, session_id: str | None = None
    ) -> None:
        failure: Exception | None = None
        for owner in tuple(self._owners.values()):
            if session_id is None or owner.session_id == session_id:
                try:
                    await owner.stop(reason)
                except Exception as exc:
                    failure = failure or exc
        if failure is not None:
            raise failure

    def _owner(self, hold_id: str) -> BrowserHoldOwner:
        owner = self._owners.get(hold_id)
        if owner is None:
            raise BrowserError(
                BrowserFailure(
                    code="stale_target",
                    message="verification hold belongs to no live occurrence in this runtime",
                )
            )
        return owner
