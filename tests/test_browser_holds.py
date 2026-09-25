"""Owned hold deadlines and cancellation, independent of model or browser timing."""

from __future__ import annotations

import asyncio

import pytest

from ricky.browser.holds import BrowserHoldOwner

SESSION = "browser_session_" + "a" * 32
PAGE = "browser_page_" + "b" * 32


async def test_hold_releases_at_deadline_without_an_agent_response() -> None:
    released = asyncio.Event()

    async def release() -> None:
        released.set()

    async def live() -> bool:
        return True

    owner = BrowserHoldOwner(
        session_id=SESSION, page_id=PAGE, maximum_seconds=0.02, release=release, live=live
    )
    try:
        await asyncio.wait_for(released.wait(), timeout=1)
        status = await owner.stop("released")
        assert status.state == "released"
        assert status.stop_reason == "deadline"
        assert status.remaining_seconds == 0
    finally:
        await owner.stop("shutdown")


async def test_cancelled_release_joins_owned_input_cleanup_once() -> None:
    entered = asyncio.Event()
    finish = asyncio.Event()
    calls = 0

    async def release() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await finish.wait()

    async def live() -> bool:
        return True

    owner = BrowserHoldOwner(
        session_id=SESSION, page_id=PAGE, maximum_seconds=30, release=release, live=live
    )
    task = asyncio.create_task(owner.stop("cancelled"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        status = await owner.stop("released")
        assert calls == 1
        assert status.state == "released"
        assert status.stop_reason == "cancelled"
    finally:
        finish.set()
        await owner.stop("shutdown")
        await asyncio.gather(task, return_exceptions=True)


async def test_navigation_releases_input_and_does_not_resume_it() -> None:
    released = asyncio.Event()

    async def release() -> None:
        released.set()

    async def live() -> bool:
        return False

    owner = BrowserHoldOwner(
        session_id=SESSION, page_id=PAGE, maximum_seconds=30, release=release, live=live
    )
    try:
        await asyncio.wait_for(released.wait(), timeout=1)
        status = await owner.stop("released")
        assert status.state == "released"
        assert status.stop_reason == "navigation"
    finally:
        await owner.stop("shutdown")


async def test_release_failure_retains_uncertainty_without_replaying_release() -> None:
    calls = 0

    async def release() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("owned browser cleanup failed")

    async def live() -> bool:
        return True

    owner = BrowserHoldOwner(
        session_id=SESSION, page_id=PAGE, maximum_seconds=30, release=release, live=live
    )
    with pytest.raises(RuntimeError, match="cleanup failed"):
        await owner.stop("released")
    assert owner.status().state == "in_doubt"
    with pytest.raises(RuntimeError, match="cleanup failed"):
        await owner.stop("shutdown")
    assert calls == 1
