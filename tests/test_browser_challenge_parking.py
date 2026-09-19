"""Interruption cannot leak the parked-browser reservation."""

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from ricky.executions.browser_runtime import BackgroundBrowserApprovalCoordinator


@pytest.mark.parametrize("phase", ["reserve", "wait", "release"])
async def test_challenge_park_cancellation_joins_reservation_and_release(phase):
    entered = asyncio.Event()
    proceed = asyncio.Event()
    usage = []

    async def reserve():
        if phase == "reserve":
            entered.set()
            await proceed.wait()
        usage.append("reserved")

    async def release():
        if phase == "release":
            entered.set()
            await proceed.wait()
        usage.append("released")

    coordinator = cast(
        BackgroundBrowserApprovalCoordinator,
        SimpleNamespace(reserve_challenge=reserve, release_park=release),
    )

    async def worker():
        async with BackgroundBrowserApprovalCoordinator.park_challenge(coordinator):
            if phase == "wait":
                entered.set()
                await proceed.wait()

    task = asyncio.create_task(worker())
    await entered.wait()
    task.cancel()
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert usage == ["reserved", "released"]
