"""Resident ownership and independent deadlines for stationary browser input."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Literal
from uuid import uuid4

from pydantic import Field

from ricky.browser.types import BrowserModel

HOLD_TOOLS = frozenset({"browser_hold_start", "browser_hold_status", "browser_hold_release"})
HOLD_EFFECT_TOOLS = HOLD_TOOLS - {"browser_hold_status"}

HoldStopReason = Literal[
    "released", "deadline", "navigation", "observation_failed", "cancelled", "shutdown"
]


class BrowserHoldStatus(BrowserModel):
    """Safe evidence about an input occurrence, never proof of verification success."""

    hold_id: str = Field(pattern=r"^browser_hold_[0-9a-f]{32}$")
    session_id: str = Field(pattern=r"^browser_session_[0-9a-f]{32}$")
    page_id: str = Field(pattern=r"^browser_page_[0-9a-f]{32}$")
    state: Literal["holding", "released", "in_doubt"]
    elapsed_seconds: float = Field(ge=0)
    remaining_seconds: float = Field(ge=0)
    stop_reason: HoldStopReason | None = None


class BrowserHoldOwner:
    """One input owner; release is shielded, joined, and independent of model work.

    Construct only after backend input dispatch is owned by the caller. The
    release callback must release input or close its owned browser on failure.
    A live check may observe navigation/ownership loss, but cannot extend time.
    """

    def __init__(
        self,
        *,
        session_id: str,
        page_id: str,
        maximum_seconds: float,
        release: Callable[[], Awaitable[None]],
        live: Callable[[], Awaitable[bool]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if maximum_seconds <= 0:
            raise ValueError("hold duration must be positive")
        self.id = f"browser_hold_{uuid4().hex}"
        self.session_id = session_id
        self.page_id = page_id
        self._clock = clock
        self._started = clock()
        self._deadline = self._started + maximum_seconds
        self._release = release
        self._live = live
        self._state: Literal["holding", "released", "in_doubt"] = "holding"
        self._reason: HoldStopReason | None = None
        self._ended: float | None = None
        self._release_task: asyncio.Task[None] | None = None
        self._watcher = asyncio.create_task(self._watch(), name=f"hold-deadline-{self.id}")

    def status(self) -> BrowserHoldStatus:
        now = self._clock() if self._ended is None else self._ended
        return BrowserHoldStatus(
            hold_id=self.id,
            session_id=self.session_id,
            page_id=self.page_id,
            state=self._state,
            elapsed_seconds=max(0.0, now - self._started),
            remaining_seconds=max(0.0, self._deadline - now) if self._state == "holding" else 0.0,
            stop_reason=self._reason,
        )

    async def stop(self, reason: HoldStopReason) -> BrowserHoldStatus:
        if self._release_task is None:
            self._reason = reason
            self._release_task = asyncio.create_task(self._finish(), name=f"hold-release-{self.id}")
        try:
            await asyncio.shield(self._release_task)
        except asyncio.CancelledError:
            # A cancelled observation/turn cannot leave an unjoined mouse-up.
            with suppress(Exception):
                await self._release_task
            raise
        finally:
            if asyncio.current_task() is not self._watcher:
                self._watcher.cancel()
                await asyncio.gather(self._watcher, return_exceptions=True)
        return self.status()

    async def _finish(self) -> None:
        try:
            await self._release()
        except BaseException:
            self._state = "in_doubt"
            raise
        else:
            self._state = "released"
        finally:
            self._ended = self._clock()

    async def _watch(self) -> None:
        try:
            while self._state == "holding":
                remaining = self._deadline - self._clock()
                if remaining <= 0:
                    await self.stop("deadline")
                    return
                try:
                    async with asyncio.timeout(min(remaining, 1.0)):
                        live = await self._live()
                except TimeoutError:
                    await self.stop(
                        "deadline" if self._clock() >= self._deadline else "observation_failed"
                    )
                    return
                if not live:
                    await self.stop("navigation")
                    return
                await asyncio.sleep(min(0.1, remaining))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Preserve uncertainty in status; never emit task-exception warnings
            # or leave input held because a liveness check itself failed.
            if self._release_task is None:
                with suppress(Exception):
                    await self.stop("observation_failed")
