"""Waiting pauses active work only within one cumulative allowance."""

import asyncio

from ricky.browser.challenge_wait import ChallengeWaitBudget


async def test_user_wait_can_outlive_active_work_budget() -> None:
    budget = ChallengeWaitBudget(0.5)

    async def work() -> None:
        with budget.pause():
            await asyncio.sleep(0.08)

    task = asyncio.create_task(work())
    assert await budget.wait_for_task(task, active_seconds=0.04)
    await task


async def test_wait_allowance_is_not_reset_by_repeated_challenges() -> None:
    budget = ChallengeWaitBudget(0.08)

    async def work() -> None:
        with budget.pause():
            await asyncio.sleep(0.05)
        with budget.pause():
            await asyncio.sleep(1)

    task = asyncio.create_task(work())
    assert not await budget.wait_for_task(task, active_seconds=0.2)
    assert not task.done(), "the observer must not cancel an effect owner"
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_active_work_still_expires_without_user_wait() -> None:
    budget = ChallengeWaitBudget(1)
    task = asyncio.create_task(asyncio.sleep(1))
    assert not await budget.wait_for_task(task, active_seconds=0.02)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
