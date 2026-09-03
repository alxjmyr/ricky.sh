"""Read-only inventory coverage for non-SQL installation formats."""

from __future__ import annotations

from pathlib import Path

import pytest

from ricky.installation import initialize_installation
from ricky.upgrades.non_sql import (
    ConfigurationUpgradeAdapter,
    ExecutionContractsUpgradeAdapter,
    JobGeneratedStateUpgradeAdapter,
    MemoryUpgradeAdapter,
    SchedulesUpgradeAdapter,
    WorkflowRunsUpgradeAdapter,
    confined_profile_roots,
)
from ricky.upgrades.registry import UpgradeRegistry


def _snapshot(root: Path) -> dict[str, tuple[bytes | None, int, int]]:
    return {
        str(path.relative_to(root)): (
            path.read_bytes() if path.is_file() else None,
            path.stat().st_mode,
            path.stat().st_mtime_ns,
        )
        for path in (root, *sorted(root.rglob("*")))
    }


def _registry(root: Path) -> UpgradeRegistry:
    profiles = confined_profile_roots(root)
    return UpgradeRegistry(
        (
            ConfigurationUpgradeAdapter(user_data_dir=root, profile_roots=profiles),
            SchedulesUpgradeAdapter(root / "schedules.toml"),
            WorkflowRunsUpgradeAdapter(root / "workflow-runs"),
            ExecutionContractsUpgradeAdapter(root / "executions" / "contracts"),
            JobGeneratedStateUpgradeAdapter(root / "agent-runs"),
            MemoryUpgradeAdapter(profiles),
        )
    )


def test_non_sql_inventory_includes_disabled_profiles_and_creates_nothing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ricky-data"
    initialize_installation(root)
    disabled = root / "profiles" / "archive"
    disabled.mkdir()
    (disabled / "ricky.toml").write_text(
        '[profile]\ndescription = "Archived context"\n', encoding="utf-8"
    )
    before = _snapshot(root)

    inspections = _registry(root).inspect(user_data_dir=root)

    by_identity = {(item.target.adapter_id, item.target.target_id): item for item in inspections}
    assert by_identity[("configuration", "installation")].state == "current"
    assert by_identity[("configuration", "profile-archive")].state == "current"
    assert by_identity[("configuration", "profile-shared")].state == "absent"
    assert by_identity[("memory", "profile-archive")].state == "absent"
    assert by_identity[("memory", "profile-shared")].state == "absent"
    assert by_identity[("schedules", "desired-state")].state == "absent"
    assert by_identity[("workflow_runs", "checkpoints")].state == "absent"
    assert by_identity[("execution_contracts", "snapshots")].state == "absent"
    assert by_identity[("job_generated_state", "generated-state")].state == "absent"
    assert _snapshot(root) == before


def test_non_sql_inventory_validates_current_formats_without_rewriting(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ricky-data"
    initialize_installation(root)
    (root / "schedules.toml").write_text("version = 1\nschedules = []\n", encoding="utf-8")
    (root / "workflow-runs").mkdir()
    (root / "executions" / "contracts").mkdir(parents=True)
    (root / "agent-runs" / "batches").mkdir(parents=True)
    (root / "agent-runs" / "batches" / "batch.json").write_text('{"items": []}\n', encoding="utf-8")
    before = _snapshot(root)

    inspections = _registry(root).inspect(user_data_dir=root)

    states = {item.target.adapter_id: item.state for item in inspections}
    assert states["schedules"] == "current"
    assert states["workflow_runs"] == "current"
    assert states["execution_contracts"] == "current"
    assert states["job_generated_state"] == "current"
    assert _snapshot(root) == before


@pytest.mark.parametrize(
    ("relative", "content", "adapter_id"),
    [
        ("schedules.toml", "not = [toml", "schedules"),
        ("agent-runs/batch.json", "{not-json", "job_generated_state"),
    ],
)
def test_non_sql_inventory_reports_corrupt_owned_formats(
    tmp_path: Path,
    relative: str,
    content: str,
    adapter_id: str,
) -> None:
    root = tmp_path / "ricky-data"
    initialize_installation(root)
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    before = path.read_bytes()

    inspections = _registry(root).inspect(user_data_dir=root)

    owned = next(item for item in inspections if item.target.adapter_id == adapter_id)
    assert owned.state == "corrupt"
    assert owned.integrity_valid is False
    assert path.read_bytes() == before


def test_profile_inventory_rejects_symlinked_or_invalid_roots(tmp_path: Path) -> None:
    root = tmp_path / "ricky-data"
    initialize_installation(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "profiles" / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="real directories"):
        confined_profile_roots(root)
