"""Connection lifetime across conversation and protected-value store reads."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import SecretStr

from ricky.config import ProtectedValuesSettings, RickySettings
from ricky.gateway.store import GatewayStore
from ricky.protected_values.store import ProfileVaultStore
from ricky.sessions.store import SessionStore


@pytest.mark.parametrize("owner", ["sessions", "gateway", "vault"])
@pytest.mark.parametrize("fail", [False, True])
async def test_store_read_closes_connection_even_when_query_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str, fail: bool
) -> None:
    settings = RickySettings(
        user_data_dir=str(tmp_path / "user"),
        project_data_dir=str(tmp_path / "project"),
        protected_values=ProtectedValuesSettings(
            argon2_iterations=1, argon2_lanes=1, argon2_memory_kib=8192
        ),
    )
    scope = settings.resolve_profile_scope()
    if owner == "vault":
        store = ProfileVaultStore(settings, scope.primary)
        await store.initialize(SecretStr("connection-lifetime-test"))
        path = store.path
    else:
        store = SessionStore(settings) if owner == "sessions" else GatewayStore(settings)
        await store.initialize()
        path = store.db_path
    connections: list[sqlite3.Connection] = []
    closed: list[sqlite3.Connection] = []

    class TrackedConnection(sqlite3.Connection):
        def execute(self, *args, **kwargs):
            if fail:
                raise sqlite3.OperationalError("injected read failure")
            return super().execute(*args, **kwargs)

        def close(self):
            super().close()
            closed.append(self)

    def connect():
        connection = sqlite3.connect(path, factory=TrackedConnection)
        connection.row_factory = sqlite3.Row
        connections.append(connection)
        return connection

    monkeypatch.setattr(store, "_connect", connect)

    async def read():
        if isinstance(store, ProfileVaultStore):
            return await store.list(limit=1)
        return await store.list(scope=scope)

    if fail:
        with pytest.raises(RuntimeError) as caught:
            await read()
        assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
        assert str(caught.value.__cause__) == "injected read failure"
    else:
        assert await read() == []
    assert connections
    assert closed == connections
    assert not (tmp_path / "project").exists()


@pytest.mark.parametrize("owner", ["sessions", "gateway", "vault", "messaging"])
async def test_store_initialization_joins_worker_after_repeated_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    import asyncio

    from ricky.messaging.store import MessagingStore

    settings = RickySettings(
        user_data_dir=str(tmp_path / "user"),
        project_data_dir=str(tmp_path / "project"),
        protected_values=ProtectedValuesSettings(
            argon2_iterations=1, argon2_lanes=1, argon2_memory_kib=8192
        ),
    )
    scope = settings.resolve_profile_scope()
    stores = {
        "sessions": SessionStore(settings),
        "gateway": GatewayStore(settings),
        "vault": ProfileVaultStore(settings, scope.primary),
        "messaging": MessagingStore(settings),
    }
    store = stores[owner]
    original = store._initialize
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    loop = asyncio.get_running_loop()

    def blocked(*args):
        loop.call_soon_threadsafe(started.set)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=5)
        original(*args)
        loop.call_soon_threadsafe(completed.set)

    monkeypatch.setattr(store, "_initialize", blocked)
    operation = (
        store.initialize(SecretStr("connection-lifetime-test"))
        if isinstance(store, ProfileVaultStore)
        else store.initialize()
    )
    task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert completed.is_set()
        assert (store.path if isinstance(store, ProfileVaultStore) else store.db_path).is_file()
        assert not (tmp_path / "project").exists()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
