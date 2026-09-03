"""Atomic persistence and host-service configuration boundaries."""

from __future__ import annotations

import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from ricky.config import (
    GatewayServiceSettings,
    GatewaySettings,
    RickySettings,
    SessionSettings,
)
from ricky.executions import contracts
from ricky.gateway.service_unit import GatewayServiceUnit


def test_contract_snapshot_publish_is_atomic_durable_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "contract.json"
    fsync_targets: list[bool] = []
    real_fsync = os.fsync

    def tracking_fsync(descriptor: int) -> None:
        fsync_targets.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        real_fsync(descriptor)

    monkeypatch.setattr(contracts.os, "fsync", tracking_fsync)

    contracts._write_exact(target, b'{"version":2}')
    contracts._write_exact(target, b'{"version":2}')

    assert target.read_bytes() == b'{"version":2}'
    assert fsync_targets == [False, True]
    assert list(tmp_path.iterdir()) == [target]


def test_contract_snapshot_write_failure_leaves_no_final_or_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "contract.json"

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("disk refused the file sync")

    monkeypatch.setattr(contracts.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="file sync"):
        contracts._write_exact(target, b"complete")

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_contract_snapshot_directory_sync_failure_keeps_retryable_exact_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "contract.json"
    real_fsync = os.fsync
    calls = 0

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("disk refused the directory sync")
        real_fsync(descriptor)

    monkeypatch.setattr(contracts.os, "fsync", fail_directory_fsync)

    with pytest.raises(OSError, match="directory sync"):
        contracts._write_exact(target, b"complete")

    assert calls == 2
    assert target.read_bytes() == b"complete"
    assert list(tmp_path.iterdir()) == [target]


def test_concurrent_contract_snapshot_writers_preserve_exact_bytes(tmp_path: Path) -> None:
    target = tmp_path / "contract.json"

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(contracts._write_exact, target, b"same") for _ in range(8)]
        for future in futures:
            future.result()

    assert target.read_bytes() == b"same"
    assert list(tmp_path.iterdir()) == [target]


def test_concurrent_conflicting_contract_snapshot_never_overwrites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "contract.json"
    barrier = threading.Barrier(2)
    real_link = os.link

    def gated_link(source: str, destination: Path) -> None:
        barrier.wait(timeout=2)
        real_link(source, destination)

    monkeypatch.setattr(contracts.os, "link", gated_link)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(contracts._write_exact, target, content)
            for content in (b"first", b"second")
        ]
        outcomes: list[BaseException | None] = []
        for future in futures:
            try:
                future.result()
            except BaseException as exc:  # noqa: BLE001 - the outcome is the assertion
                outcomes.append(exc)
            else:
                outcomes.append(None)

    assert sum(outcome is None for outcome in outcomes) == 1
    errors = [outcome for outcome in outcomes if outcome is not None]
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert "immutable execution snapshot collision" in str(errors[0])
    assert target.read_bytes() in {b"first", b"second"}
    assert list(tmp_path.iterdir()) == [target]


def test_session_turn_wall_keeps_inbox_claim_headroom() -> None:
    assert SessionSettings(turn_wall_seconds=3_570).turn_wall_seconds == 3_570

    with pytest.raises(ValidationError, match="30 seconds of inbox-claim headroom"):
        SessionSettings(turn_wall_seconds=3_570.001)


def test_gateway_unit_directory_is_resolved_when_settings_are_built(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_xdg = tmp_path / "first-xdg"
    second_xdg = tmp_path / "second-xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(first_xdg))
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))

    monkeypatch.setenv("XDG_CONFIG_HOME", str(second_xdg))
    unit = GatewayServiceUnit(settings, project_root=tmp_path, executable="/usr/bin/uv")

    assert settings.gateway.service.unit_dir == str(first_xdg / "systemd" / "user")
    assert unit.unit_dir == first_xdg / "systemd" / "user"


def test_gateway_unit_directory_expands_user_and_requires_an_absolute_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    configured = GatewayServiceSettings(unit_dir="~/custom-units")

    assert configured.unit_dir == str(tmp_path / "home" / "custom-units")
    with pytest.raises(ValidationError, match="must resolve to an absolute path"):
        GatewayServiceSettings(unit_dir="relative/units")


def test_gateway_unit_constructor_override_remains_authoritative(tmp_path: Path) -> None:
    configured = tmp_path / "configured"
    override = tmp_path / "override"
    settings = RickySettings(
        user_data_dir=str(tmp_path / "user"),
        gateway=GatewaySettings(
            service=GatewayServiceSettings(unit_dir=str(configured)),
        ),
    )

    unit = GatewayServiceUnit(
        settings,
        project_root=tmp_path,
        unit_dir=override,
        executable="/usr/bin/uv",
    )

    assert unit.unit_dir == override
