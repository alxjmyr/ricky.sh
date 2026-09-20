"""Waiting pauses active work only within one cumulative allowance."""

import asyncio

import pytest

from ricky.browser import challenge_wait
from ricky.browser.challenge_wait import ChallengeWaitBudget


class Clock:
    now = 0.0
    observed: asyncio.Event | None = None

    def monotonic(self) -> float:
        if self.observed is not None:
            self.observed.set()
        return self.now


async def test_user_wait_can_outlive_active_work_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    # Replace only this owner's clock, never the event loop or process-wide time.
    clock = Clock()
    monkeypatch.setattr(challenge_wait, "time", clock)
    budget = ChallengeWaitBudget(120)
    entered = asyncio.Event()
    release = asyncio.Event()
    observed = asyncio.Event()
    finish = asyncio.Event()

    async def work() -> None:
        with budget.pause():
            entered.set()
            await release.wait()
        clock.observed = observed
        await finish.wait()

    task = asyncio.create_task(work())
    observer = asyncio.create_task(budget.wait_for_task(task, active_seconds=30))
    try:
        await entered.wait()
        clock.now += 31
        assert budget.remaining_seconds == 89
        assert not task.done()
        release.set()
        await asyncio.wait_for(observed.wait(), timeout=1)
        assert not observer.done(), "resumed work must still have its active budget"
        finish.set()
        assert await asyncio.wait_for(observer, timeout=1)
        await task
        assert budget.remaining_seconds == 89
    finally:
        task.cancel()
        observer.cancel()
        await asyncio.gather(task, observer, return_exceptions=True)


async def test_wait_allowance_is_not_reset_by_repeated_challenges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    monkeypatch.setattr(challenge_wait, "time", clock)
    budget = ChallengeWaitBudget(80)

    async def work() -> None:
        with budget.pause():
            clock.now += 50
        with budget.pause():
            clock.now += 40
            await asyncio.Event().wait()

    task = asyncio.create_task(work())
    try:
        assert not await asyncio.wait_for(budget.wait_for_task(task, active_seconds=200), timeout=1)
        assert budget.remaining_seconds == 0
        assert not task.done(), "the observer must not cancel an effect owner"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_active_work_still_expires_without_user_wait() -> None:
    budget = ChallengeWaitBudget(1)
    task = asyncio.create_task(asyncio.sleep(1))
    assert not await budget.wait_for_task(task, active_seconds=0.02)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
