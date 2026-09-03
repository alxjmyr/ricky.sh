"""Managed gateway, profile-job, schedule, and crontab upgrade reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable, Coroutine
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ricky.config import RickySettings, find_project_root, load_settings_at
from ricky.gateway.lock import GatewayLock
from ricky.gateway.service_unit import MARKER, GatewayServiceUnit
from ricky.installation import managed_schedules_installed, write_private_file
from ricky.jobs.registry import JobRegistry
from ricky.schedules.cron import UserCrontabBackend, render_fragment
from ricky.schedules.service import ScheduleService, ScheduleServiceError
from ricky.schedules.store import ScheduleStore
from ricky.schedules.types import ScheduleSpec
from ricky.upgrades.journal import UpgradeJournal, UpgradeManagedBinding


class ManagedIntegrationError(RuntimeError):
    """A managed launch surface is foreign, ambiguous, or failed reconciliation."""


class ManagedUpgradeResult(BaseModel):
    """Strict, separately reportable launch and schedule outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    gateway: Literal["not_installed", "reconciled", "restarted", "inactive"]
    cron_changed: bool = False
    schedules_installed: tuple[str, ...] = ()
    schedules_ready: tuple[str, ...] = ()
    schedules_disabled: tuple[str, ...] = ()
    schedules_validation_required: tuple[str, ...] = ()
    schedules_lineage_required: tuple[str, ...] = ()
    schedules_approval_required: tuple[str, ...] = ()
    schedules_unavailable: tuple[str, ...] = ()
    profile_jobs_valid: tuple[str, ...] = ()
    profile_jobs_invalid: tuple[str, ...] = ()
    detail: str = Field(default="managed integrations reconciled", max_length=2_000)


async def prepare_managed_upgrade(
    *,
    settings: RickySettings,
    source_executable: Path,
    update_jobs: bool,
    drain_timeout_seconds: float = 30.0,
) -> UpgradeManagedBinding:
    """Validate and stop an exact owned gateway before the caller asks for EX."""

    unit = GatewayServiceUnit(settings, executable=str(source_executable.resolve()))
    installed = unit.installed()
    unit_digest: str | None = None
    enabled = False
    active = False
    stopped_gateway = False
    try:
        if installed is not None:
            if not installed.startswith(MARKER):
                raise ManagedIntegrationError(
                    f"{unit.unit_path} is not owned by Ricky; upgrade made no service change"
                )
            if installed != unit.render():
                raise ManagedIntegrationError(
                    "Ricky gateway unit does not bind the installed release executable; "
                    "reinstall the managed gateway service before upgrading"
                )
            unit_digest = hashlib.sha256(installed.encode("utf-8")).hexdigest()
            enabled = _enabled_state(unit)
            active = _active_state(unit)
            if active:
                stopped = unit.stop()
                if stopped.returncode != 0:
                    raise ManagedIntegrationError("Ricky gateway could not be stopped for upgrade")
                stopped_gateway = True
                deadline = time.monotonic() + drain_timeout_seconds
                lock = GatewayLock(settings)
                while lock.is_active():
                    if time.monotonic() >= deadline:
                        raise ManagedIntegrationError(
                            "Ricky gateway did not release its runtime lock before the upgrade "
                            "timeout"
                        )
                    await asyncio.sleep(0.05)
        crontab = UserCrontabBackend(settings)
        cron_installed = await managed_schedules_installed(crontab)
        return UpgradeManagedBinding(
            update_jobs=update_jobs,
            gateway_unit_path=str(unit.unit_path.resolve()) if installed is not None else None,
            gateway_unit_sha256=unit_digest,
            gateway_was_enabled=enabled,
            gateway_was_active=active,
            managed_crontab_was_installed=cron_installed,
        )
    except BaseException:
        if stopped_gateway:
            with suppress(Exception):
                unit.start()
        raise


class ManagedUpgradeController:
    """Idempotently reconcile launch surfaces while the operation holds EX."""

    def __init__(
        self,
        *,
        user_data_dir: Path,
        executable: Path,
        run_async: Callable[[Coroutine[Any, Any, ManagedUpgradeResult]], ManagedUpgradeResult],
    ) -> None:
        self._root = user_data_dir.resolve()
        self._executable = executable.resolve()
        # The synchronous coordinator boundary owns no event loop. The interface
        # entry point supplies one so this library never drives a loop itself.
        self._run_async = run_async

    def reconcile_target(self, journal: UpgradeJournal) -> None:
        self._reconcile(journal, endpoint="target")

    def reconcile_source(self, journal: UpgradeJournal) -> None:
        self._reconcile(journal, endpoint="source")

    def _reconcile(self, journal: UpgradeJournal, *, endpoint: Literal["source", "target"]) -> None:
        managed = _managed(journal)
        settings = load_settings_at(self._root)
        gateway = self._reconcile_gateway(settings, managed, endpoint=endpoint)
        schedule = self._run_async(
            _reconcile_schedules(
                settings=settings,
                executable=self._executable,
                managed=managed,
            )
        )
        result = schedule.model_copy(update={"gateway": gateway})
        _write_result(self._root, journal.operation_id, result)

    def _reconcile_gateway(
        self,
        settings: RickySettings,
        managed: UpgradeManagedBinding,
        *,
        endpoint: Literal["source", "target"] = "target",
    ) -> Literal["not_installed", "reconciled", "inactive"]:
        if managed.gateway_unit_path is None:
            return "not_installed"
        unit = GatewayServiceUnit(settings, executable=str(self._executable))
        if unit.unit_path.resolve() != Path(managed.gateway_unit_path):
            raise ManagedIntegrationError("gateway unit path changed during upgrade")
        current = unit.installed()
        if current is None or not current.startswith(MARKER):
            raise ManagedIntegrationError("owned gateway unit disappeared during upgrade")
        current_digest = hashlib.sha256(current.encode("utf-8")).hexdigest()
        desired = unit.render()
        desired_digest = hashlib.sha256(desired.encode("utf-8")).hexdigest()
        if current_digest != managed.gateway_unit_sha256 and current != desired:
            source_restore = (
                endpoint == "source"
                and desired_digest == managed.gateway_unit_sha256
                and _exec_start(current) == _exec_start(desired)
            )
            if not source_restore:
                raise ManagedIntegrationError("gateway unit changed during upgrade")
        installed = unit.install()
        if not installed.verified:
            raise ManagedIntegrationError("target gateway unit verification failed")
        if unit.daemon_reload().returncode != 0:
            raise ManagedIntegrationError("user service manager did not reload the gateway unit")
        state = unit.enable() if managed.gateway_was_enabled else unit.disable()
        if state.returncode != 0:
            raise ManagedIntegrationError("gateway enabled state could not be preserved")
        return "reconciled" if managed.gateway_was_active else "inactive"


def restart_gateway_after_upgrade(
    *,
    user_data_dir: Path,
    executable: Path,
    operation_id: str,
) -> ManagedUpgradeResult:
    """Restart a previously active gateway after EX has been released and verify it."""

    journal_path = user_data_dir / "upgrades" / operation_id / "journal.json"
    journal = UpgradeJournal.model_validate_json(journal_path.read_bytes())
    managed = _managed(journal)
    result = load_managed_result(user_data_dir, operation_id)
    if not managed.gateway_was_active:
        return result
    settings = load_settings_at(user_data_dir)
    unit = GatewayServiceUnit(settings, executable=str(executable.resolve()))
    started = unit.start()
    if started.returncode != 0 or not _active_state(unit):
        failed = result.model_copy(
            update={
                "gateway": "reconciled",
                "detail": "upgrade completed, but the previously active gateway failed to restart",
            }
        )
        _write_result(user_data_dir, operation_id, failed)
        return failed
    restarted = result.model_copy(update={"gateway": "restarted"})
    _write_result(user_data_dir, operation_id, restarted)
    return restarted


def restore_gateway_after_aborted_prepare(
    *, settings: RickySettings, executable: Path, managed: UpgradeManagedBinding
) -> None:
    """Best-effort restore only a source gateway stopped before journal preparation."""

    if not managed.gateway_was_active:
        return
    unit = GatewayServiceUnit(settings, executable=str(executable.resolve()))
    current = unit.installed()
    if current is None or hashlib.sha256(current.encode("utf-8")).hexdigest() != (
        managed.gateway_unit_sha256
    ):
        raise ManagedIntegrationError("stopped gateway unit changed before it could be restored")
    if unit.start().returncode != 0:
        raise ManagedIntegrationError("stopped gateway could not be restored after upgrade abort")


def load_managed_result(user_data_dir: Path, operation_id: str) -> ManagedUpgradeResult:
    path = _result_path(user_data_dir, operation_id)
    try:
        return ManagedUpgradeResult.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise ManagedIntegrationError("managed upgrade result is missing or invalid") from exc


async def _reconcile_schedules(
    *,
    settings: RickySettings,
    executable: Path,
    managed: UpgradeManagedBinding,
) -> ManagedUpgradeResult:
    scope = settings.resolve_profile_scope(
        settings.profiles.default,
        access_profiles=settings.profiles.enabled,
    )
    profile_registry = JobRegistry(settings, profile_scope=scope)
    jobs, job_errors = profile_registry.discover()
    valid_jobs = tuple(item.resource.qualified for item in jobs)
    invalid_jobs = tuple(sorted(str(error.source_path) for error in job_errors))

    store = ScheduleStore(settings, scope=scope)
    schedules = await store.list()
    usable_project = _first_usable_project(schedules)
    backend = UserCrontabBackend(settings)
    if usable_project is None:
        changed = False
        detail = "managed integrations reconciled"
        if schedules:
            # The desired managed block is unknown while no schedule project root
            # resolves, so leave the installed one exactly as it is. Syncing an
            # empty fragment here would delete every managed entry, and the
            # crontab is not a rollback backup target.
            detail = (
                "managed schedules were left unchanged because no schedule project root resolved"
            )
        elif managed.managed_crontab_was_installed:
            # No schedules remain, so an empty block is the desired state and
            # removes stale managed entries.
            fragment = render_fragment(
                [],
                ricky_executable=executable,
                log_dir=backend.log_dir.resolve(),
            )
            changed = (await backend.sync(fragment)).changed
        return ManagedUpgradeResult(
            gateway="not_installed",
            cron_changed=changed,
            schedules_unavailable=tuple(schedule.id for schedule in schedules),
            profile_jobs_valid=valid_jobs,
            profile_jobs_invalid=invalid_jobs,
            detail=detail,
        )

    service = ScheduleService(
        settings,
        profile_scope=scope,
        project_root=usable_project,
        backend=backend,
        ricky_executable=executable,
    )
    inspections = await service.list()
    if managed.update_jobs:
        for inspection in inspections:
            if inspection.state == "validation_required":
                with suppress(ScheduleServiceError):
                    await service.refresh(inspection.schedule.id)
        inspections = await service.list()
    if managed.managed_crontab_was_installed:
        report = await service.sync()
        return ManagedUpgradeResult(
            gateway="not_installed",
            cron_changed=report.changed,
            schedules_installed=tuple(report.installed),
            schedules_disabled=tuple(report.disabled),
            schedules_validation_required=tuple(report.validation_required),
            schedules_lineage_required=tuple(report.lineage_required),
            schedules_approval_required=tuple(report.approval_required),
            schedules_unavailable=tuple(report.unavailable),
            profile_jobs_valid=valid_jobs,
            profile_jobs_invalid=invalid_jobs,
        )
    return ManagedUpgradeResult(
        gateway="not_installed",
        schedules_ready=tuple(item.schedule.id for item in inspections if item.state == "ready"),
        schedules_disabled=tuple(
            item.schedule.id for item in inspections if item.state == "disabled"
        ),
        schedules_validation_required=tuple(
            item.schedule.id for item in inspections if item.state == "validation_required"
        ),
        schedules_lineage_required=tuple(
            item.schedule.id for item in inspections if item.state == "lineage_required"
        ),
        schedules_approval_required=tuple(
            item.schedule.id for item in inspections if item.state == "approval_required"
        ),
        schedules_unavailable=tuple(
            item.schedule.id for item in inspections if item.state == "unavailable"
        ),
        profile_jobs_valid=valid_jobs,
        profile_jobs_invalid=invalid_jobs,
    )


def _first_usable_project(schedules: list[ScheduleSpec]) -> Path | None:
    for schedule in schedules:
        project_root = Path(schedule.project_root).expanduser().resolve()
        if not project_root.is_dir():
            continue
        try:
            discovered = find_project_root(project_root)
        except (OSError, ValueError):
            continue
        if discovered == project_root:
            return project_root
    return None


def _active_state(unit: GatewayServiceUnit) -> bool:
    result = unit.status()
    state = result.stdout.strip()
    if result.returncode == 0 and state in {"", "active"}:
        return True
    if state in {"inactive", "failed", "deactivating"} or result.returncode in {3, 4}:
        return False
    raise ManagedIntegrationError("gateway active state is unavailable")


def _enabled_state(unit: GatewayServiceUnit) -> bool:
    result = unit.enabled()
    state = result.stdout.strip()
    if result.returncode == 0 and state in {"", "enabled", "enabled-runtime"}:
        return True
    if state in {"disabled", "static", "indirect", "masked"} or result.returncode == 1:
        return False
    raise ManagedIntegrationError("gateway enabled state is unavailable")


def _exec_start(content: str) -> str | None:
    matches = [line for line in content.splitlines() if line.startswith("ExecStart=")]
    return matches[0] if len(matches) == 1 else None


def _managed(journal: UpgradeJournal) -> UpgradeManagedBinding:
    if journal.managed is None:
        raise ManagedIntegrationError("upgrade journal has no managed integration binding")
    return journal.managed


def _result_path(user_data_dir: Path, operation_id: str) -> Path:
    return user_data_dir.resolve() / "upgrades" / operation_id / "managed-result.json"


def _write_result(user_data_dir: Path, operation_id: str, result: ManagedUpgradeResult) -> None:
    write_private_file(
        _result_path(user_data_dir, operation_id),
        result.model_dump_json(indent=2) + "\n",
    )
