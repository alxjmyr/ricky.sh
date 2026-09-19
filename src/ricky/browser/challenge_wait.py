"""Bounded user waiting separate from active browser execution time."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager


class ChallengeWaitBudget:
    """One runtime's cumulative wait allowance, without resetting work budgets."""

    def __init__(
        self,
        maximum_wait_seconds: float,
        *,
        park: Callable[[], AbstractAsyncContextManager[None]] | None = None,
    ) -> None:
        if maximum_wait_seconds <= 0:
            raise ValueError("browser challenge wait allowance must be positive")
        self.maximum_wait_seconds = maximum_wait_seconds
        self._used = 0.0
        self._started: float | None = None
        self._changed = asyncio.Event()
        self._park = park

    @asynccontextmanager
    async def waiting(self) -> AsyncIterator[None]:
        """Account for automatic retrieval and user input under the same capacity ceiling."""
        with self.pause():
            if self._park is None:
                yield
            else:
                async with self._park():
                    yield

    def _elapsed(self, now: float) -> float:
        return self._used + (0.0 if self._started is None else now - self._started)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.maximum_wait_seconds - self._elapsed(time.monotonic()))

    @contextmanager
    def pause(self) -> Iterator[None]:
        if self._started is not None:
            raise ValueError("browser execution is already waiting for user input")
        if self._used >= self.maximum_wait_seconds:
            raise TimeoutError("browser challenge wait allowance exhausted")
        self._started = time.monotonic()
        self._changed.set()
        try:
            yield
        finally:
            self._used = self._elapsed(time.monotonic())
            self._started = None
            self._changed.set()

    async def wait_for_task(self, task: asyncio.Task, *, active_seconds: float) -> bool:
        """Observe completion within active work plus cumulative bounded waits.

        The caller still owns cancellation and joining the worker. This observer
        never cancels or restarts an effect task on its own.
        """

        started = time.monotonic()
        initial_pause = self._elapsed(started)
        while not task.done():
            self._changed.clear()
            now = time.monotonic()
            paused = self._elapsed(now)
            active_left = active_seconds - (now - started - (paused - initial_pause))
            absolute_left = active_seconds + self.maximum_wait_seconds - (now - started)
            allowance = (
                self.maximum_wait_seconds - paused if self._started is not None else active_left
            )
            remaining = min(absolute_left, allowance)
            if active_left <= 0 or remaining <= 0:
                return False
            changed = asyncio.create_task(self._changed.wait())
            try:
                done, _ = await asyncio.wait(
                    {task, changed},
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    return task.done()
            finally:
                changed.cancel()
                await asyncio.gather(changed, return_exceptions=True)
        return True
