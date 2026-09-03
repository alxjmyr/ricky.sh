"""Dependency-free ownership lifecycle for work protected by an expiring lease."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any


async def run_with_lease_heartbeat[T, LeaseT](
    operation: Awaitable[T],
    *,
    lease: LeaseT,
    renew: Callable[[LeaseT], Awaitable[LeaseT]],
    interval_seconds: float,
) -> T:
    """Run owned work while renewing its lease, cancelling immediately on loss."""

    if interval_seconds <= 0:
        raise ValueError("heartbeat interval_seconds must be positive")
    operation_task = asyncio.ensure_future(operation)
    heartbeat_task = asyncio.create_task(
        _heartbeat(operation_task, lease=lease, renew=renew, interval_seconds=interval_seconds)
    )
    try:
        done, _ = await asyncio.wait(
            {operation_task, heartbeat_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        # The operation includes its fenced durable commit. If commit and a
        # renewal failure become observable in the same scheduler turn, that
        # completed commit is stronger evidence than the concurrent renewal
        # error and must not be reported as a failed operation.
        if operation_task in done:
            return await operation_task
        if heartbeat_task in done:
            heartbeat_error = heartbeat_task.exception()
            if heartbeat_error is not None:
                if not operation_task.done():
                    operation_task.cancel()
                with suppress(BaseException):
                    await operation_task
                raise heartbeat_error
        return await operation_task
    except asyncio.CancelledError:
        if not operation_task.done():
            operation_task.cancel()
        with suppress(BaseException):
            await operation_task
        raise
    finally:
        if not heartbeat_task.done():
            heartbeat_task.cancel()
        with suppress(BaseException):
            await heartbeat_task


async def _heartbeat[LeaseT](
    operation_task: asyncio.Future[Any],
    *,
    lease: LeaseT,
    renew: Callable[[LeaseT], Awaitable[LeaseT]],
    interval_seconds: float,
) -> None:
    current = lease
    while not operation_task.done():
        await asyncio.sleep(interval_seconds)
        if operation_task.done():
            return
        current = await renew(current)
