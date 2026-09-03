"""Whole-installation discovery for subsystem-owned durable upgrade targets."""

from __future__ import annotations

from pathlib import Path

from ricky.authority.upgrade import AuthorityUpgradeAdapter
from ricky.config import load_settings_at
from ricky.durable_tasks.upgrade import DurableTasksUpgradeAdapter
from ricky.executions.upgrade import ExecutionsUpgradeAdapter
from ricky.gateway.upgrade import GatewayUpgradeAdapter
from ricky.jobs.upgrade import JobsUpgradeAdapter
from ricky.messaging.upgrade import MessagingUpgradeAdapter
from ricky.notifications.upgrade import NotificationsUpgradeAdapter
from ricky.protected_values.upgrade import ProtectedValuesUpgradeAdapter
from ricky.sessions.upgrade import SessionsUpgradeAdapter
from ricky.upgrades.models import AdapterInspection
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


def inspect_upgrade_inventory(user_data_dir: Path) -> tuple[AdapterInspection, ...]:
    """Inspect every safely discoverable durable target without creating it.

    Configuration is the authority for customized target paths, so its raw and
    typed validation runs first. If that boundary is corrupt, only the targets
    that can be discovered without trusting it are returned.
    """

    root = _real_root(user_data_dir)
    profile_roots = confined_profile_roots(root)
    configuration = ConfigurationUpgradeAdapter(
        user_data_dir=root,
        profile_roots=profile_roots,
    )
    configuration_inventory = UpgradeRegistry((configuration,)).inspect(user_data_dir=root)
    if any(item.state in {"corrupt", "unsupported"} for item in configuration_inventory):
        return configuration_inventory

    registry = build_upgrade_registry(root, profile_roots=profile_roots)
    return registry.inspect(user_data_dir=root)


def build_upgrade_registry(
    user_data_dir: Path,
    *,
    profile_roots: tuple[Path, ...] | None = None,
) -> UpgradeRegistry:
    """Compose the complete current adapter registry for one installation."""

    root = _real_root(user_data_dir)
    profiles = confined_profile_roots(root) if profile_roots is None else profile_roots
    settings = load_settings_at(root)

    durable_task_paths = tuple(
        _confined(root, profile / settings.durable_tasks.dir / "tasks.sqlite3")
        for profile in profiles
    )
    protected_value_paths = tuple(
        _confined(
            root,
            profile / settings.protected_values.dir / "protected-values.sqlite3",
        )
        for profile in profiles
    )
    messaging_path = _configured(root, settings.messaging.store_path)
    job_run_root = _configured(root, settings.jobs.run_dir)

    return UpgradeRegistry(
        (
            AuthorityUpgradeAdapter((_configured(root, settings.authority.store_path),)),
            ConfigurationUpgradeAdapter(user_data_dir=root, profile_roots=profiles),
            DurableTasksUpgradeAdapter(durable_task_paths),
            ExecutionContractsUpgradeAdapter(
                _configured(root, settings.executions.contract_snapshot_dir)
            ),
            ExecutionsUpgradeAdapter((_configured(root, settings.executions.store_path),)),
            GatewayUpgradeAdapter((_configured(root, settings.gateway.store_path),)),
            JobGeneratedStateUpgradeAdapter(job_run_root),
            JobsUpgradeAdapter((_confined(root, job_run_root / "runs.sqlite3"),)),
            MemoryUpgradeAdapter(profiles),
            MessagingUpgradeAdapter((messaging_path,)),
            NotificationsUpgradeAdapter((messaging_path,)),
            ProtectedValuesUpgradeAdapter(protected_value_paths),
            SchedulesUpgradeAdapter(_confined(root, root / "schedules.toml")),
            SessionsUpgradeAdapter((_configured(root, settings.sessions.store_path),)),
            WorkflowRunsUpgradeAdapter(_configured(root, settings.workflow.run_dir)),
        )
    )


def _real_root(user_data_dir: Path) -> Path:
    root = user_data_dir.expanduser().resolve()
    if not root.exists() or not root.is_dir() or root.is_symlink():
        raise ValueError("user_data_dir must be an existing real directory")
    return root


def _configured(root: Path, configured: str) -> Path:
    return _confined(root, root / configured)


def _confined(root: Path, path: Path) -> Path:
    canonical = path.resolve()
    if not canonical.is_relative_to(root):
        raise ValueError("upgrade target escapes user_data_dir")
    return canonical
