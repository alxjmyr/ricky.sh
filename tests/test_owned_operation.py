"""Owned lease heartbeat cancellation and cleanup guarantees."""

import asyncio

import pytest

from ricky.runtime.owned_operation import run_with_lease_heartbeat


async def test_renewal_failure_cancels_and_joins_owned_operation() -> None:
    started = asyncio.Event()
    joined = asyncio.Event()

    async def operation() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            joined.set()

    async def renew(lease: int) -> int:
        del lease
        raise RuntimeError("lease renewal failed")

    task = asyncio.create_task(
        run_with_lease_heartbeat(operation(), lease=1, renew=renew, interval_seconds=0.001)
    )
    await started.wait()
    with pytest.raises(RuntimeError, match="lease renewal failed"):
        await task
    assert joined.is_set()


async def test_success_stops_heartbeat_and_returns_result() -> None:
    renewals = 0

    async def operation() -> str:
        await asyncio.sleep(0.005)
        return "done"

    async def renew(lease: int) -> int:
        nonlocal renewals
        renewals += 1
        return lease + 1

    assert (
        await run_with_lease_heartbeat(operation(), lease=1, renew=renew, interval_seconds=0.001)
        == "done"
    )
    assert renewals >= 1


async def test_caller_cancellation_joins_operation() -> None:
    started = asyncio.Event()
    joined = asyncio.Event()

    async def operation() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            joined.set()

    async def renew(lease: int) -> int:
        return lease + 1

    task = asyncio.create_task(
        run_with_lease_heartbeat(operation(), lease=1, renew=renew, interval_seconds=10)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert joined.is_set()


async def test_completed_operation_wins_exact_renewal_failure_boundary() -> None:
    """A durable commit result is authoritative if both tasks finish together."""

    release = asyncio.Event()
    renewal_started = asyncio.Event()

    async def operation() -> str:
        await release.wait()
        return "committed"

    async def renew(lease: int) -> int:
        del lease
        renewal_started.set()
        await release.wait()
        raise RuntimeError("boundary renewal failed")

    owned = asyncio.create_task(
        run_with_lease_heartbeat(
            operation(),
            lease=1,
            renew=renew,
            interval_seconds=0.001,
        )
    )
    await renewal_started.wait()
    release.set()

    assert await owned == "committed"
