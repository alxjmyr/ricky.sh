"""Resident vault ownership and one-shot gateway startup handoff tests."""

from __future__ import annotations

import asyncio
import os
import socket
import stat
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from ricky.agent import AgentSession
from ricky.config import GatewaySettings, ProtectedValuesSettings, RickySettings, user_data_path
from ricky.gateway.lock import GatewayLock
from ricky.gateway.service_unit import CommandResult
from ricky.gateway.vault_bootstrap import (
    GatewayVaultBootstrapError,
    GatewayVaultBootstrapServer,
    consume_gateway_vault_bootstrap,
    vault_bootstrap_socket_path,
)
from ricky.interfaces.cli import gateway as gateway_cli
from ricky.profiles import ProfileScope
from ricky.protected_values import (
    ProtectedValueBroker,
    ProtectedValueStoreError,
    ResidentProtectedValueRegistry,
)
from ricky.runtime import build_capability_runtime

_PERSONAL_PASSPHRASE = "personal-startup-passphrase-8412"
_WORK_PASSPHRASE = "work-startup-passphrase-3791"


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings(
        user_data_dir=str(tmp_path / "user"),
        project_data_dir=str(tmp_path / "project"),
        protected_values=ProtectedValuesSettings(
            enabled=True,
            argon2_iterations=1,
            argon2_lanes=1,
            argon2_memory_kib=8_192,
        ),
        gateway=GatewaySettings(enabled=True),
    )


async def _initialize_vaults(settings: RickySettings) -> None:
    broker = ProtectedValueBroker(
        settings,
        scope=ProfileScope.create("personal", access_profiles=("work",)),
    )
    try:
        await broker.initialize("personal", SecretStr(_PERSONAL_PASSPHRASE))
        await broker.initialize("work", SecretStr(_WORK_PASSPHRASE))
    finally:
        await broker.aclose()


@pytest.mark.asyncio
async def test_resident_registry_leases_share_unlock_but_never_widen_scope(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    await _initialize_vaults(settings)
    registry = ResidentProtectedValueRegistry(settings)
    await registry.unlock_many({"personal": SecretStr(_PERSONAL_PASSPHRASE)})

    lease = registry.lease(scope=ProfileScope.create("personal"))
    assert (await lease.status("personal")).unlocked is True
    with pytest.raises(ProtectedValueStoreError, match="unavailable"):
        await lease.status("work")

    await lease.aclose()
    second = registry.lease(scope=ProfileScope.create("personal"))
    assert (await second.status("personal")).unlocked is True

    await registry.aclose()
    with pytest.raises(ProtectedValueStoreError, match="registry is closed"):
        await second.status("personal")


@pytest.mark.asyncio
async def test_resident_multi_profile_unlock_is_all_or_none(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    await _initialize_vaults(settings)
    registry = ResidentProtectedValueRegistry(settings)

    with pytest.raises(ProtectedValueStoreError, match="unlock failed"):
        await registry.unlock_many(
            {
                "personal": SecretStr(_PERSONAL_PASSPHRASE),
                "work": SecretStr("wrong-work-passphrase"),
            }
        )

    assert registry.unlocked_profiles == ()
    await registry.aclose()


@pytest.mark.asyncio
async def test_capability_runtime_borrows_resident_unlock_without_owning_it(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    await _initialize_vaults(settings)
    registry = ResidentProtectedValueRegistry(settings)
    await registry.unlock("personal", SecretStr(_PERSONAL_PASSPHRASE))
    session = AgentSession.create(settings, profile_scope=ProfileScope.create("personal"))

    async with build_capability_runtime(
        settings,
        session=session,
        project_root=tmp_path,
        protected_value_registry=registry,
    ) as runtime:
        assert runtime.protected_values is not None
        assert (await runtime.protected_values.status("personal")).unlocked is True

    assert registry.unlocked_profiles == ("personal",)
    await registry.aclose()


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(__import__("socket"), "SO_PEERCRED"),
    reason="managed service handoff requires POSIX peer credentials",
)
@pytest.mark.asyncio
async def test_one_shot_handoff_unlocks_exact_gateway_owner_and_removes_socket(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    await _initialize_vaults(settings)
    registry = ResidentProtectedValueRegistry(settings)
    lock = GatewayLock(settings)
    lock.acquire()
    path = vault_bootstrap_socket_path(settings)
    try:
        async with GatewayVaultBootstrapServer(
            settings,
            {
                "personal": SecretStr(_PERSONAL_PASSPHRASE),
                "work": SecretStr(_WORK_PASSPHRASE),
            },
        ) as bootstrap:
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            exchange = asyncio.create_task(bootstrap.exchange())
            consumed = await consume_gateway_vault_bootstrap(settings, registry)
            acknowledged = await exchange
            assert consumed == acknowledged == ("personal", "work")
        assert not path.exists()
        assert registry.unlocked_profiles == ("personal", "work")
    finally:
        lock.release()
        await registry.aclose()

    durable_bytes = b"".join(
        item.read_bytes() for item in (tmp_path / "user").rglob("*") if item.is_file()
    )
    assert _PERSONAL_PASSPHRASE.encode() not in durable_bytes
    assert _WORK_PASSPHRASE.encode() not in durable_bytes


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(__import__("socket"), "SO_PEERCRED"),
    reason="managed service handoff requires POSIX peer credentials",
)
@pytest.mark.asyncio
async def test_failed_handoff_relocks_every_profile_and_reports_only_safe_error(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    await _initialize_vaults(settings)
    registry = ResidentProtectedValueRegistry(settings)
    lock = GatewayLock(settings)
    lock.acquire()
    try:
        async with GatewayVaultBootstrapServer(
            settings,
            {
                "personal": SecretStr(_PERSONAL_PASSPHRASE),
                "work": SecretStr("wrong-work-passphrase"),
            },
        ) as bootstrap:
            exchange = asyncio.create_task(bootstrap.exchange())
            with pytest.raises(GatewayVaultBootstrapError, match="requested vault unlock failed"):
                await consume_gateway_vault_bootstrap(settings, registry)
            with pytest.raises(GatewayVaultBootstrapError, match="requested vault unlock failed"):
                await exchange
        assert registry.unlocked_profiles == ()
    finally:
        lock.release()
        await registry.aclose()


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(__import__("socket"), "SO_PEERCRED"),
    reason="managed service handoff requires POSIX peer credentials",
)
@pytest.mark.asyncio
async def test_cancelled_bootstrap_server_leaves_no_socket(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    path = vault_bootstrap_socket_path(settings)
    async with GatewayVaultBootstrapServer(
        settings,
        {"personal": SecretStr(_PERSONAL_PASSPHRASE)},
    ) as bootstrap:
        exchange = asyncio.create_task(bootstrap.exchange())
        exchange.cancel()
        with pytest.raises(asyncio.CancelledError):
            await exchange
    assert not path.exists()


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(socket, "SO_PEERCRED"),
    reason="managed service handoff requires POSIX peer credentials",
)
@pytest.mark.asyncio
async def test_stale_bootstrap_socket_does_not_prevent_locked_start(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    registry = ResidentProtectedValueRegistry(settings)
    path = vault_bootstrap_socket_path(settings)
    path.parent.mkdir(parents=True, mode=0o700)
    stale = socket.socket(socket.AF_UNIX)
    try:
        stale.bind(str(path))
    finally:
        stale.close()
    os.chmod(path, 0o600)

    assert await consume_gateway_vault_bootstrap(settings, registry) == ()
    assert not path.exists()
    assert registry.unlocked_profiles == ()

    async with GatewayVaultBootstrapServer(
        settings,
        {"personal": SecretStr(_PERSONAL_PASSPHRASE)},
    ):
        assert path.exists()
        with pytest.raises(GatewayVaultBootstrapError, match="already exists"):
            async with GatewayVaultBootstrapServer(
                settings,
                {"personal": SecretStr(_PERSONAL_PASSPHRASE)},
            ):
                raise AssertionError("a second live bootstrap server must not start")
    await registry.aclose()


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(__import__("socket"), "SO_PEERCRED"),
    reason="managed service handoff requires POSIX peer credentials",
)
@pytest.mark.asyncio
async def test_handoff_rejects_peer_that_is_not_current_lock_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    fast_service = settings.gateway.service.model_copy(update={"start_timeout_seconds": 0.05})
    settings = settings.model_copy(
        update={"gateway": settings.gateway.model_copy(update={"service": fast_service})}
    )
    registry = ResidentProtectedValueRegistry(settings)
    lock = GatewayLock(settings)
    lock.acquire()
    real_read_owner = GatewayLock.read_owner

    def wrong_owner(self: GatewayLock) -> Any:
        owner = real_read_owner(self)
        assert owner is not None
        return owner.model_copy(update={"pid": owner.pid + 1})

    monkeypatch.setattr(GatewayLock, "read_owner", wrong_owner)
    try:
        async with GatewayVaultBootstrapServer(
            settings,
            {"personal": SecretStr(_PERSONAL_PASSPHRASE)},
        ) as bootstrap:
            exchange = asyncio.create_task(bootstrap.exchange())
            with pytest.raises(GatewayVaultBootstrapError, match="handoff failed"):
                await consume_gateway_vault_bootstrap(settings, registry)
            with pytest.raises(GatewayVaultBootstrapError, match="timed out"):
                await exchange
        assert registry.unlocked_profiles == ()
    finally:
        lock.release()
        await registry.aclose()


@pytest.mark.asyncio
async def test_joined_service_control_thread_finishes_before_cancellation_propagates() -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocking_control() -> str:
        entered.set()
        assert release.wait(timeout=2)
        return "joined"

    task = asyncio.create_task(gateway_cli._joined_thread(blocking_control))
    await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(__import__("socket"), "SO_PEERCRED"),
    reason="managed service handoff requires POSIX peer credentials",
)
@pytest.mark.asyncio
async def test_cancelled_managed_start_joins_control_and_stops_launched_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    await _initialize_vaults(settings)
    entered = threading.Event()
    release = threading.Event()
    stop_calls = 0

    class ManagedUnit:
        def __init__(self, selected: RickySettings) -> None:
            assert selected is settings

        def status(self) -> CommandResult:
            return CommandResult(args=("status",), returncode=3, stdout="", stderr="")

        def start(self) -> CommandResult:
            entered.set()
            assert release.wait(timeout=2)
            return CommandResult(args=("start",), returncode=0, stdout="", stderr="")

        def stop(self) -> CommandResult:
            nonlocal stop_calls
            stop_calls += 1
            return CommandResult(args=("stop",), returncode=0, stdout="", stderr="")

        def restart(self) -> CommandResult:
            raise AssertionError("start test must not restart")

    class Renderer:
        async def request_protected_unlock(self, request: object) -> SecretStr:
            del request
            return SecretStr(_PERSONAL_PASSPHRASE)

        def render_status(self, message: str, *, style: str) -> None:
            del message, style

    monkeypatch.setattr(gateway_cli, "load_settings", lambda: settings)
    monkeypatch.setattr(gateway_cli, "GatewayServiceUnit", ManagedUnit)
    task = asyncio.create_task(
        gateway_cli._service_control(
            "start",
            Renderer(),  # type: ignore[arg-type]
            unlock_vault=("personal",),
        )
    )
    await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stop_calls == 1
    assert not vault_bootstrap_socket_path(settings).exists()


@pytest.mark.asyncio
async def test_direct_run_takes_gateway_lock_before_prompting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    holder = GatewayLock(settings)
    holder.acquire()
    prompt_calls = 0

    class Renderer:
        async def request_protected_unlock(self, request: object) -> SecretStr:
            nonlocal prompt_calls
            del request
            prompt_calls += 1
            return SecretStr(_PERSONAL_PASSPHRASE)

    monkeypatch.setattr(gateway_cli, "load_settings", lambda: settings)
    try:
        with pytest.raises(ValueError, match="another gateway"):
            await gateway_cli._run_service(
                ("personal",),
                Renderer(),  # type: ignore[arg-type]
            )
    finally:
        holder.release()
    assert prompt_calls == 0


@pytest.mark.asyncio
async def test_service_start_rejects_active_gateway_before_prompting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    prompt_calls = 0

    class ActiveUnit:
        def __init__(self, selected: RickySettings) -> None:
            assert selected is settings

        def status(self) -> Any:
            return SimpleNamespace(returncode=0)

        def start(self) -> Any:
            raise AssertionError("active service must not be started")

        def stop(self) -> Any:
            raise AssertionError("active service must not be stopped")

        def restart(self) -> Any:
            raise AssertionError("active service must not be restarted")

    class Renderer:
        async def request_protected_unlock(self, request: object) -> SecretStr:
            nonlocal prompt_calls
            del request
            prompt_calls += 1
            return SecretStr(_PERSONAL_PASSPHRASE)

    monkeypatch.setattr(gateway_cli, "load_settings", lambda: settings)
    monkeypatch.setattr(gateway_cli, "GatewayServiceUnit", ActiveUnit)

    with pytest.raises(Exception, match="already active"):
        await gateway_cli._service_control(
            "start",
            Renderer(),  # type: ignore[arg-type]
            unlock_vault=("personal",),
        )
    assert prompt_calls == 0


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(socket, "SO_PEERCRED"),
    reason="managed service handoff requires POSIX peer credentials",
)
@pytest.mark.asyncio
async def test_service_start_does_not_stop_gateway_that_appears_during_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    await _initialize_vaults(settings)
    status_calls = 0

    class RacingUnit:
        def __init__(self, selected: RickySettings) -> None:
            assert selected is settings

        def status(self) -> CommandResult:
            nonlocal status_calls
            status_calls += 1
            return CommandResult(
                args=("status",),
                returncode=3 if status_calls == 1 else 0,
                stdout="",
                stderr="",
            )

        def start(self) -> CommandResult:
            raise AssertionError("a gateway that became active must not be started again")

        def stop(self) -> CommandResult:
            raise AssertionError("a gateway this invocation did not launch must not be stopped")

        def restart(self) -> CommandResult:
            raise AssertionError("start test must not restart")

    class Renderer:
        async def request_protected_unlock(self, request: object) -> SecretStr:
            del request
            return SecretStr(_PERSONAL_PASSPHRASE)

    monkeypatch.setattr(gateway_cli, "load_settings", lambda: settings)
    monkeypatch.setattr(gateway_cli, "GatewayServiceUnit", RacingUnit)

    with pytest.raises(Exception, match="became active"):
        await gateway_cli._service_control(
            "start",
            Renderer(),  # type: ignore[arg-type]
            unlock_vault=("personal",),
        )
    assert status_calls == 2
    assert not vault_bootstrap_socket_path(settings).exists()


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(__import__("socket"), "SO_PEERCRED"),
    reason="managed service handoff requires POSIX peer credentials",
)
@pytest.mark.asyncio
async def test_managed_start_handoff_never_places_passphrase_in_supervisor_args_or_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    await _initialize_vaults(settings)
    loop = asyncio.get_running_loop()
    child_registries: list[ResidentProtectedValueRegistry] = []
    rendered: list[str] = []

    class ManagedUnit:
        def __init__(self, selected: RickySettings) -> None:
            assert selected is settings

        def status(self) -> CommandResult:
            return CommandResult(
                args=("systemctl", "--user", "is-active", "ricky-gateway.service"),
                returncode=3,
                stdout="inactive",
                stderr="",
            )

        def start(self) -> CommandResult:
            registry = ResidentProtectedValueRegistry(settings)
            child_registries.append(registry)
            lock = GatewayLock(settings)
            lock.acquire()
            try:
                future = asyncio.run_coroutine_threadsafe(
                    consume_gateway_vault_bootstrap(settings, registry),
                    loop,
                )
                assert future.result(timeout=2) == ("personal",)
            finally:
                lock.release()
            return CommandResult(
                args=("systemctl", "--user", "start", "ricky-gateway.service"),
                returncode=0,
                stdout="",
                stderr="",
            )

        def stop(self) -> CommandResult:
            raise AssertionError("successful startup must not be stopped")

        def restart(self) -> CommandResult:
            raise AssertionError("start test must not restart")

    class Renderer:
        async def request_protected_unlock(self, request: object) -> SecretStr:
            del request
            return SecretStr(_PERSONAL_PASSPHRASE)

        def render_status(self, message: str, *, style: str) -> None:
            del style
            rendered.append(message)

    monkeypatch.setattr(gateway_cli, "load_settings", lambda: settings)
    monkeypatch.setattr(gateway_cli, "GatewayServiceUnit", ManagedUnit)

    await gateway_cli._service_control(
        "start",
        Renderer(),  # type: ignore[arg-type]
        unlock_vault=("personal",),
    )

    assert len(child_registries) == 1
    assert child_registries[0].unlocked_profiles == ("personal",)
    exposed = "\n".join(rendered)
    assert _PERSONAL_PASSPHRASE not in exposed
    assert "personal" not in exposed
    assert str(user_data_path(settings)) not in exposed
    await child_registries[0].aclose()
