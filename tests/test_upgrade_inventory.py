"""Whole-installation read-only upgrade inventory coverage."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ricky.installation import initialize_installation
from ricky.upgrades.inventory import inspect_upgrade_inventory
from ricky.upgrades.service import check_upgrade

_SQL_ADAPTERS = {
    "authority",
    "durable_tasks",
    "executions",
    "gateway",
    "jobs",
    "messaging",
    "notifications",
    "protected_values",
    "sessions",
}


def _snapshot(root: Path) -> dict[str, tuple[bytes | None, int, int]]:
    return {
        str(path.relative_to(root)): (
            path.read_bytes() if path.is_file() else None,
            path.stat().st_mode,
            path.stat().st_mtime_ns,
        )
        for path in (root, *sorted(root.rglob("*")))
    }


def test_fresh_install_inventory_lists_all_sql_owners_without_creating_stores(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ricky-data"
    initialize_installation(root)
    before = _snapshot(root)

    inventory = inspect_upgrade_inventory(root)

    sql = tuple(item for item in inventory if item.target.kind == "sqlite")
    assert {item.target.adapter_id for item in sql} == _SQL_ADAPTERS
    assert len(sql) == len(_SQL_ADAPTERS)
    assert all(item.state == "absent" for item in sql)
    messaging = next(item for item in sql if item.target.adapter_id == "messaging")
    notifications = next(item for item in sql if item.target.adapter_id == "notifications")
    assert messaging.target.physical_path == notifications.target.physical_path
    assert messaging.target.target_id != notifications.target.target_id
    assert _snapshot(root) == before


def test_inventory_uses_custom_paths_and_includes_disabled_profile_stores(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ricky-data"
    initialize_installation(root)
    config = root / "ricky.toml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + """

[authority]
store_path = "custom/authority.sqlite3"
[durable_tasks]
dir = "custom-tasks"
[executions]
store_path = "custom/executions.sqlite3"
contract_snapshot_dir = "custom/contracts"
[gateway]
store_path = "custom/gateway.sqlite3"
[jobs]
run_dir = "custom-runs"
[messaging]
store_path = "custom/notifications.sqlite3"
[protected_values]
dir = "custom-vault"
[sessions]
store_path = "custom/sessions.sqlite3"
[workflow]
run_dir = "custom-workflows"
""",
        encoding="utf-8",
    )
    disabled = root / "profiles" / "archive"
    disabled.mkdir()
    before = _snapshot(root)

    inventory = inspect_upgrade_inventory(root)

    paths = {
        (item.target.adapter_id, item.target.path)
        for item in inventory
        if item.target.kind == "sqlite"
    }
    assert ("authority", str(root / "custom" / "authority.sqlite3")) in paths
    assert ("jobs", str(root / "custom-runs" / "runs.sqlite3")) in paths
    for profile in ("shared", "archive"):
        assert (
            "durable_tasks",
            str(root / "profiles" / profile / "custom-tasks" / "tasks.sqlite3"),
        ) in paths
        assert (
            "protected_values",
            str(root / "profiles" / profile / "custom-vault" / "protected-values.sqlite3"),
        ) in paths
    assert _snapshot(root) == before


def test_corrupt_configuration_stops_before_trusting_configured_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ricky-data"
    initialize_installation(root)
    config = root / "ricky.toml"
    config.write_text("not = [valid", encoding="utf-8")
    before = _snapshot(root)

    inventory = inspect_upgrade_inventory(root)

    assert {item.target.adapter_id for item in inventory} == {"configuration"}
    installation = next(item for item in inventory if item.target.target_id == "installation")
    assert installation.state == "corrupt"
    assert installation.integrity_valid is False
    assert _snapshot(root) == before


def test_upgrade_check_reports_corrupt_inventory_as_incompatible(tmp_path: Path) -> None:
    root = tmp_path / "ricky-data"
    initialize_installation(root)
    config = root / "ricky.toml"
    config.write_text("not = [valid", encoding="utf-8")
    before = _snapshot(root)

    result = asyncio.run(check_upgrade())

    assert result.status == "incompatible"
    assert result.compatibility.compatible is False
    assert any("configuration target" in issue for issue in result.compatibility.issues)
    assert any(item.state == "corrupt" for item in result.inventory)
    assert _snapshot(root) == before
