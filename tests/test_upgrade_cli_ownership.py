"""Cancellation cannot release upgrade resources while their worker is alive."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import cast

import pytest

from ricky.interfaces.cli.installation import _resume_owned_upgrade
from ricky.upgrades.orchestrator import UpgradeCoordinator
from ricky.upgrades.software import UpgradeHandoffComplete


@pytest.mark.parametrize("handoff", [False, True])
async def test_cancelled_upgrade_joins_worker_before_releasing_owner(handoff: bool) -> None:
    started = threading.Event()
    finish = threading.Event()
    exited = threading.Event()

    def resume() -> None:
        started.set()
        assert finish.wait(timeout=5)
        exited.set()
        if handoff:
            raise UpgradeHandoffComplete(0)

    coordinator = cast(UpgradeCoordinator, SimpleNamespace(resume=resume))
    task = asyncio.create_task(_resume_owned_upgrade(coordinator))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()  # Repeated interruption must not abandon the worker either.
        await asyncio.sleep(0)
        assert not task.done()
        assert not exited.is_set()
    finally:
        finish.set()
    with pytest.raises(UpgradeHandoffComplete if handoff else asyncio.CancelledError):
        await task
    assert exited.is_set()
