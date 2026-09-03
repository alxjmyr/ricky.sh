"""Managed gateway and schedule reconciliation during released upgrades."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import ricky.upgrades.integrations as integrations
from ricky.config import load_settings_at
from ricky.gateway.service_unit import MARKER
from ricky.installation import initialize_installation
from ricky.schedules.cron import BEGIN_MARKER, END_MARKER
from ricky.upgrades.integrations import (
    ManagedIntegrationError,
    ManagedUpgradeController,
    ManagedUpgradeResult,
    prepare_managed_upgrade,
)
from ricky.upgrades.journal import (
    UpgradeJournal,
    UpgradeManagedBinding,
    create_upgrade_journal,
)
from ricky.upgrades.models import MigrationPlan
from ricky.upgrades.versions import ReleaseVersion

OPERATION_ID = "b" * 32


def _settings(tmp_path: Path) -> tuple[Path, Any]:
    root = (tmp_path / "user-data").resolve()
    initialize_installation(root)
    return root, load_settings_at(root)


def _journal(root: Path) -> UpgradeJournal:
    return create_upgrade_journal(
        user_data_dir=root,
        installation_id="a" * 32,
        operation_id=OPERATION_ID,
        source_software_version=ReleaseVersion.parse("0.6.0"),
        target_software_version=ReleaseVersion.parse("0.7.0"),
        plan=MigrationPlan.create(source_data_generation=1, target_data_generation=2),
        backup_manifest_path=root / "upgrades" / OPERATION_ID / "backup" / "manifest.json",
        managed=UpgradeManagedBinding(),
    )


def test_prepare_records_and_stops_an_exact_owned_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, settings = _settings(tmp_path)
    executable = (tmp_path / "uv-bin" / "ricky").resolve()
    calls: list[str] = []

    class FakeUnit:
        unit_path = (tmp_path / "ricky-gateway.service").resolve()

        def __init__(self, _settings: object, *, executable: str) -> None:
            self.executable = executable

        def render(self) -> str:
            return f"{MARKER}\nExecStart={self.executable} gateway run\n"

        def installed(self) -> str:
            return self.render()

        def enabled(self) -> SimpleNamespace:
            return SimpleNamespace(returncode=0, stdout="enabled")

        def status(self) -> SimpleNamespace:
            return SimpleNamespace(returncode=0, stdout="active")

        def stop(self) -> SimpleNamespace:
            calls.append("stop")
            return SimpleNamespace(returncode=0)

        def start(self) -> SimpleNamespace:
            calls.append("start")
            return SimpleNamespace(returncode=0)

    class FakeLock:
        def __init__(self, _settings: object) -> None:
            pass

        def is_active(self) -> bool:
            return False

    async def cron_installed(_backend: object) -> bool:
        return True

    monkeypatch.setattr(integrations, "GatewayServiceUnit", FakeUnit)
    monkeypatch.setattr(integrations, "GatewayLock", FakeLock)
    monkeypatch.setattr(integrations, "managed_schedules_installed", cron_installed)

    binding = asyncio.run(
        prepare_managed_upgrade(
            settings=settings,
            source_executable=executable,
            update_jobs=True,
        )
    )

    assert calls == ["stop"]
    assert binding.gateway_was_active is True
    assert binding.gateway_was_enabled is True
    assert binding.managed_crontab_was_installed is True
    assert binding.update_jobs is True
    assert binding.gateway_unit_path == str(FakeUnit.unit_path)
    expected = f"{MARKER}\nExecStart={executable} gateway run\n"
    assert binding.gateway_unit_sha256 == hashlib.sha256(expected.encode()).hexdigest()


def test_prepare_restores_a_stopped_gateway_if_preparation_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, settings = _settings(tmp_path)
    calls: list[str] = []

    class FakeUnit:
        unit_path = (tmp_path / "ricky-gateway.service").resolve()

        def __init__(self, _settings: object, *, executable: str) -> None:
            self.executable = executable

        def render(self) -> str:
            return f"{MARKER}\nExecStart={self.executable} gateway run\n"

        def installed(self) -> str:
            return self.render()

        def enabled(self) -> SimpleNamespace:
            return SimpleNamespace(returncode=0, stdout="enabled")

        def status(self) -> SimpleNamespace:
            return SimpleNamespace(returncode=0, stdout="active")

        def stop(self) -> SimpleNamespace:
            calls.append("stop")
            return SimpleNamespace(returncode=0)

        def start(self) -> SimpleNamespace:
            calls.append("start")
            return SimpleNamespace(returncode=0)

    class FakeLock:
        def __init__(self, _settings: object) -> None:
            pass

        def is_active(self) -> bool:
            return False

    async def unreadable_cron(_backend: object) -> bool:
        raise RuntimeError("crontab became unreadable")

    monkeypatch.setattr(integrations, "GatewayServiceUnit", FakeUnit)
    monkeypatch.setattr(integrations, "GatewayLock", FakeLock)
    monkeypatch.setattr(integrations, "managed_schedules_installed", unreadable_cron)

    with pytest.raises(RuntimeError, match="crontab became unreadable"):
        asyncio.run(
            prepare_managed_upgrade(
                settings=settings,
                source_executable=tmp_path / "ricky",
                update_jobs=False,
            )
        )

    assert calls == ["stop", "start"]


def test_prepare_refuses_a_foreign_gateway_without_controlling_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, settings = _settings(tmp_path)

    class ForeignUnit:
        unit_path = (tmp_path / "ricky-gateway.service").resolve()

        def __init__(self, _settings: object, *, executable: str) -> None:
            del executable

        def installed(self) -> str:
            return "[Service]\nExecStart=/tmp/not-ricky\n"

    monkeypatch.setattr(integrations, "GatewayServiceUnit", ForeignUnit)

    with pytest.raises(ManagedIntegrationError, match="not owned"):
        asyncio.run(
            prepare_managed_upgrade(
                settings=settings,
                source_executable=tmp_path / "ricky",
                update_jobs=False,
            )
        )


def test_gateway_reconciliation_preserves_enabled_and_inactive_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, settings = _settings(tmp_path)
    source = (tmp_path / "old" / "ricky").resolve()
    target = (tmp_path / "new" / "ricky").resolve()
    unit_path = (tmp_path / "ricky-gateway.service").resolve()
    source_content = f"{MARKER}\nExecStart={source} gateway run\n"
    state = {"content": source_content}
    calls: list[str] = []

    class FakeUnit:
        def __init__(self, _settings: object, *, executable: str) -> None:
            self.executable = executable
            self.unit_path = unit_path

        def render(self) -> str:
            return f"{MARKER}\nExecStart={self.executable} gateway run\n"

        def installed(self) -> str:
            return state["content"]

        def install(self) -> SimpleNamespace:
            state["content"] = self.render()
            calls.append("install")
            return SimpleNamespace(verified=True)

        def daemon_reload(self) -> SimpleNamespace:
            calls.append("reload")
            return SimpleNamespace(returncode=0)

        def enable(self) -> SimpleNamespace:
            calls.append("enable")
            return SimpleNamespace(returncode=0)

        def disable(self) -> SimpleNamespace:
            calls.append("disable")
            return SimpleNamespace(returncode=0)

    monkeypatch.setattr(integrations, "GatewayServiceUnit", FakeUnit)
    binding = UpgradeManagedBinding(
        gateway_unit_path=str(unit_path),
        gateway_unit_sha256=hashlib.sha256(source_content.encode()).hexdigest(),
        gateway_was_enabled=True,
        gateway_was_active=False,
    )

    outcome = ManagedUpgradeController(
        user_data_dir=root,
        executable=target,
        run_async=asyncio.run,
    )._reconcile_gateway(settings, binding)

    assert outcome == "inactive"
    assert state["content"] == f"{MARKER}\nExecStart={target} gateway run\n"
    assert calls == ["install", "reload", "enable"]


def test_gateway_rollback_accepts_the_target_template_and_restores_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, settings = _settings(tmp_path)
    executable = (tmp_path / "tool" / "bin" / "ricky").resolve()
    unit_path = (tmp_path / "ricky-gateway.service").resolve()
    source_content = f"{MARKER}\n[Service]\nExecStart={executable} gateway run\n"
    state = {
        "content": (
            f"{MARKER}\n[Service]\nTargetFeature=true\nExecStart={executable} gateway run\n"
        )
    }

    class FakeUnit:
        def __init__(self, _settings: object, *, executable: str) -> None:
            self.executable = executable
            self.unit_path = unit_path

        def render(self) -> str:
            return source_content

        def installed(self) -> str:
            return state["content"]

        def install(self) -> SimpleNamespace:
            state["content"] = self.render()
            return SimpleNamespace(verified=True)

        def daemon_reload(self) -> SimpleNamespace:
            return SimpleNamespace(returncode=0)

        def enable(self) -> SimpleNamespace:
            return SimpleNamespace(returncode=0)

        def disable(self) -> SimpleNamespace:
            return SimpleNamespace(returncode=0)

    monkeypatch.setattr(integrations, "GatewayServiceUnit", FakeUnit)
    binding = UpgradeManagedBinding(
        gateway_unit_path=str(unit_path),
        gateway_unit_sha256=hashlib.sha256(source_content.encode()).hexdigest(),
        gateway_was_enabled=True,
        gateway_was_active=True,
    )

    outcome = ManagedUpgradeController(
        user_data_dir=root,
        executable=executable,
        run_async=asyncio.run,
    )._reconcile_gateway(settings, binding, endpoint="source")

    assert outcome == "reconciled"
    assert state["content"] == source_content


@pytest.mark.parametrize(("update_jobs", "refreshes"), [(False, 0), (True, 1)])
def test_schedule_reconciliation_refreshes_only_opted_in_safe_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    update_jobs: bool,
    refreshes: int,
) -> None:
    root, settings = _settings(tmp_path)
    project = (tmp_path / "project").resolve()
    project.mkdir()
    authored = project / "job.toml"
    authored.write_text("project-owned bytes\n", encoding="utf-8")
    schedule = SimpleNamespace(id="sched_safe", project_root=str(project))
    approval = SimpleNamespace(id="sched_approval", project_root=str(project))
    calls: list[str] = []

    class FakeRegistry:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def discover(self) -> tuple[list[SimpleNamespace], list[SimpleNamespace]]:
            valid = SimpleNamespace(resource=SimpleNamespace(qualified="shared/profile-job"))
            invalid = SimpleNamespace(source_path=root / "profiles/shared/jobs/bad/job.toml")
            return [valid], [invalid]

    class FakeStore:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def list(self) -> list[SimpleNamespace]:
            return [schedule, approval]

    class FakeBackend:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.log_dir = root / "logs"

    class FakeService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.list_count = 0

        async def list(self) -> list[SimpleNamespace]:
            self.list_count += 1
            safe_state = "ready" if self.list_count > 1 and update_jobs else "validation_required"
            return [
                SimpleNamespace(schedule=schedule, state=safe_state),
                SimpleNamespace(schedule=approval, state="approval_required"),
            ]

        async def refresh(self, schedule_id: str) -> None:
            calls.append(f"refresh:{schedule_id}")

        async def sync(self) -> SimpleNamespace:
            calls.append("sync")
            installed = [schedule.id] if update_jobs else []
            validation = [] if update_jobs else [schedule.id]
            return SimpleNamespace(
                changed=True,
                installed=installed,
                disabled=[],
                validation_required=validation,
                lineage_required=[],
                approval_required=[approval.id],
                unavailable=[],
            )

    monkeypatch.setattr(integrations, "JobRegistry", FakeRegistry)
    monkeypatch.setattr(integrations, "ScheduleStore", FakeStore)
    monkeypatch.setattr(integrations, "UserCrontabBackend", FakeBackend)
    monkeypatch.setattr(integrations, "ScheduleService", FakeService)
    monkeypatch.setattr(integrations, "find_project_root", lambda path: path)

    result = asyncio.run(
        integrations._reconcile_schedules(
            settings=settings,
            executable=(tmp_path / "bin" / "ricky").resolve(),
            managed=UpgradeManagedBinding(
                update_jobs=update_jobs,
                managed_crontab_was_installed=True,
            ),
        )
    )

    assert sum(call.startswith("refresh:") for call in calls) == refreshes
    assert calls[-1] == "sync"
    assert result.schedules_approval_required == (approval.id,)
    assert result.schedules_installed == ((schedule.id,) if update_jobs else ())
    assert result.schedules_validation_required == (() if update_jobs else (schedule.id,))
    assert result.profile_jobs_valid == ("shared/profile-job",)
    assert result.profile_jobs_invalid == (str(root / "profiles/shared/jobs/bad/job.toml"),)
    assert authored.read_bytes() == b"project-owned bytes\n"


def _job_registry(root: Path) -> type:
    class FakeRegistry:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def discover(self) -> tuple[list[SimpleNamespace], list[SimpleNamespace]]:
            valid = SimpleNamespace(resource=SimpleNamespace(qualified="shared/profile-job"))
            invalid = SimpleNamespace(source_path=root / "profiles/shared/jobs/bad/job.toml")
            return [valid], [invalid]

    return FakeRegistry


def test_unreachable_schedule_project_roots_leave_the_managed_crontab_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, settings = _settings(tmp_path)
    unmounted = (tmp_path / "unmounted" / "project").resolve()
    schedules = [
        SimpleNamespace(id="sched_offline", project_root=str(unmounted)),
        SimpleNamespace(id="sched_renamed", project_root=str(unmounted / "was-renamed")),
    ]
    installed = (
        f"{BEGIN_MARKER}\n"
        "0 * * * * umask 077; /bin/ricky schedule invoke sched_offline\n"
        f"{END_MARKER}\n"
    )
    crontab = {"block": installed}
    synced: list[str] = []

    class FakeStore:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def list(self) -> list[SimpleNamespace]:
            return schedules

    class FakeBackend:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.log_dir = root / "cron" / "logs"

        async def sync(self, fragment: str) -> SimpleNamespace:
            synced.append(fragment)
            crontab["block"] = fragment
            return SimpleNamespace(changed=True)

    class ForbiddenService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("no schedule service is usable without a project root")

    monkeypatch.setattr(integrations, "JobRegistry", _job_registry(root))
    monkeypatch.setattr(integrations, "ScheduleStore", FakeStore)
    monkeypatch.setattr(integrations, "UserCrontabBackend", FakeBackend)
    monkeypatch.setattr(integrations, "ScheduleService", ForbiddenService)

    result = asyncio.run(
        integrations._reconcile_schedules(
            settings=settings,
            executable=(tmp_path / "bin" / "ricky").resolve(),
            managed=UpgradeManagedBinding(managed_crontab_was_installed=True),
        )
    )

    assert not unmounted.exists()
    assert synced == []
    assert crontab["block"] == installed
    assert result.cron_changed is False
    assert result.schedules_unavailable == ("sched_offline", "sched_renamed")
    assert result.schedules_installed == ()
    assert "no schedule project root resolved" in result.detail
    assert result.profile_jobs_valid == ("shared/profile-job",)


@pytest.mark.parametrize("was_installed", [True, False])
def test_an_empty_schedule_set_still_clears_the_managed_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, was_installed: bool
) -> None:
    root, settings = _settings(tmp_path)
    executable = (tmp_path / "bin" / "ricky").resolve()
    synced: list[str] = []

    class EmptyStore:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def list(self) -> list[SimpleNamespace]:
            return []

    class FakeBackend:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.log_dir = root / "cron" / "logs"

        async def sync(self, fragment: str) -> SimpleNamespace:
            synced.append(fragment)
            return SimpleNamespace(changed=True)

    monkeypatch.setattr(integrations, "JobRegistry", _job_registry(root))
    monkeypatch.setattr(integrations, "ScheduleStore", EmptyStore)
    monkeypatch.setattr(integrations, "UserCrontabBackend", FakeBackend)

    result = asyncio.run(
        integrations._reconcile_schedules(
            settings=settings,
            executable=executable,
            managed=UpgradeManagedBinding(managed_crontab_was_installed=was_installed),
        )
    )

    assert synced == ([f"{BEGIN_MARKER}\n{END_MARKER}\n"] if was_installed else [])
    assert result.cron_changed is was_installed
    assert result.schedules_unavailable == ()
    assert result.detail == "managed integrations reconciled"


def test_managed_controller_reconciles_through_the_injected_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _unused_settings = _settings(tmp_path)
    executable = (tmp_path / "bin" / "ricky").resolve()
    reconciled = ManagedUpgradeResult(
        gateway="not_installed",
        cron_changed=True,
        schedules_ready=("sched_ready",),
    )
    observed: list[Path] = []

    async def fake_reconcile_schedules(
        *, settings: Any, executable: Path, managed: UpgradeManagedBinding
    ) -> ManagedUpgradeResult:
        del settings, managed
        observed.append(executable)
        return reconciled

    driven: list[object] = []

    def run_without_owning_a_loop(
        coroutine: Coroutine[Any, Any, ManagedUpgradeResult],
    ) -> ManagedUpgradeResult:
        """Drive the reconciliation coroutine without any asyncio event loop."""

        driven.append(coroutine)
        try:
            coroutine.send(None)
        except StopIteration as stop:
            outcome: ManagedUpgradeResult = stop.value
            return outcome
        raise AssertionError("managed schedule reconciliation suspended unexpectedly")

    monkeypatch.setattr(integrations, "_reconcile_schedules", fake_reconcile_schedules)
    journal = _journal(root)
    controller = ManagedUpgradeController(
        user_data_dir=root,
        executable=executable,
        run_async=run_without_owning_a_loop,
    )

    async def reconcile_while_a_loop_is_running() -> None:
        controller.reconcile_target(journal)

    asyncio.run(reconcile_while_a_loop_is_running())

    assert observed == [executable]
    assert len(driven) == 1
    written = ManagedUpgradeResult.model_validate_json(
        (root / "upgrades" / OPERATION_ID / "managed-result.json").read_bytes()
    )
    assert written == reconciled
