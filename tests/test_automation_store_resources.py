"""Automation store operations release SQLite resources without relying on GC."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from ricky.authority.store import AuthorityStore
from ricky.config import RickySettings
from ricky.durable_tasks.store import DurableTaskStore
from ricky.executions.store import ExecutionStore
from ricky.jobs.browser_store import BrowserRunLedger
from ricky.jobs.store import JobRunStore
from ricky.notifications.store import NotificationStore
from ricky.profiles import ProfileScope


@pytest.mark.parametrize("kind", ["authority", "tasks", "executions", "jobs", "browser_jobs"])
async def test_store_reads_close_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    settings = RickySettings(
        user_data_dir=str(tmp_path / "user"), project_data_dir=str(tmp_path / "project")
    )
    scope = ProfileScope.create("personal")
    if kind == "tasks":
        store = await DurableTaskStore.create(settings, profile="personal")
    else:
        owner = {
            "authority": AuthorityStore,
            "executions": ExecutionStore,
            "jobs": JobRunStore,
            "browser_jobs": BrowserRunLedger,
        }[kind]
        store = owner(settings)
        await store.initialize()

    connections: list[TrackedConnection] = []

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self) -> None:
            self.closed = True
            super().close()

    connect = sqlite3.connect

    def tracked_connect(*args, **kwargs) -> TrackedConnection:
        connection = connect(*args, **kwargs, factory=TrackedConnection, check_same_thread=False)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    try:
        if isinstance(store, DurableTaskStore):
            assert await store.search() == []
        elif isinstance(store, BrowserRunLedger):
            assert await store.active_attempts(scope=scope) == []
        else:
            assert await store.list(scope=scope) == []
        assert connections
        assert all(connection.closed for connection in connections)
    finally:
        # Retain references through the assertion so GC cannot hide a leak.
        for connection in connections:
            if not connection.closed:
                connection.close()

    assert not (tmp_path / "project").exists()


@pytest.mark.parametrize(
    "kind", ["authority", "tasks", "executions", "jobs", "browser_jobs", "notifications"]
)
@pytest.mark.parametrize("fail_operation", [False, True])
async def test_store_workers_settle_before_repeated_cancellation_returns(
    tmp_path: Path, kind: str, fail_operation: bool
) -> None:
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    if kind == "tasks":
        task_store = await DurableTaskStore.create(settings, profile="personal")
        run = task_store._run_blocking
    elif kind == "authority":
        run = AuthorityStore(settings)._run
    elif kind == "executions":
        run = ExecutionStore(settings)._run
    elif kind == "jobs":
        run = JobRunStore(settings)._call
    elif kind == "browser_jobs":
        run = BrowserRunLedger(settings)._call
    else:
        run = NotificationStore(settings)._run
    entered = asyncio.Event()
    finished = threading.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def blocked_operation() -> None:
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(5), "store worker was not released"
            if fail_operation:
                raise sqlite3.OperationalError("injected write failure")
            (tmp_path / "committed").write_text("settled")
        finally:
            finished.set()

    operation = asyncio.create_task(run(blocked_operation))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        for _ in range(2):
            operation.cancel()
            await asyncio.sleep(0)
            assert not operation.done()
        assert not finished.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
    assert finished.is_set()
    assert (tmp_path / "committed").exists() is not fail_operation
