"""Provider-free gateway status and doctor checks."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from authority_support import install_sandbox_runtime
from gateway_ops_support import (
    PROFILE_SCOPE,
    FakeRunner,
    enqueue_notification,
    job_run,
    make_conversation,
    reserve_effect,
    settings,
    store_inbound,
)
from gateway_ops_support import inbound as build_inbound
from ricky.config import RickySettings
from ricky.executions.store import ExecutionStore
from ricky.gateway.health import GatewayHealth
from ricky.gateway.lock import GatewayLock
from ricky.gateway.service_unit import GatewayServiceUnit
from ricky.gateway.store import GatewayStore
from ricky.jobs.store import JobRunStore
from ricky.messaging.store import MessagingStore
from ricky.notifications.store import NotificationStore
from ricky.sessions.store import SessionStore
from sandbox_support import SandboxGuardrailEvaluator

pytestmark = pytest.mark.asyncio

SECRET = "test-token"


def _unit(config, tmp_path: Path) -> GatewayServiceUnit:  # type: ignore[no-untyped-def]
    return GatewayServiceUnit(
        config,
        unit_dir=tmp_path / "units",
        runner=FakeRunner(),
        executable="/usr/bin/uv",
    )


async def test_status_needs_no_provider_and_counts_every_subsystem(tmp_path: Path) -> None:
    config = settings(tmp_path)
    messaging = MessagingStore(config)
    notifications = NotificationStore(config)
    gateway = GatewayStore(config)
    sessions = SessionStore(config)
    jobs = JobRunStore(config)
    await messaging.initialize()
    await notifications.initialize()
    await gateway.initialize()
    await sessions.initialize()
    await jobs.initialize()
    await store_inbound(messaging, build_inbound())
    await enqueue_notification(notifications)
    await make_conversation(gateway, sessions)
    run = job_run()
    await jobs.insert(run, scope=PROFILE_SCOPE)
    await reserve_effect(jobs, run)

    status = await GatewayHealth(config, unit=_unit(config, tmp_path)).status()

    assert status.gateway_enabled is True
    assert status.inbox["pending"] == 1
    assert status.outbox["pending"] == 1
    assert status.conversations["active"] == 1
    assert status.sessions["active"] == 1
    assert status.effects["reserved"] == 1
    assert status.transports[0].account == "personal/bot"
    assert status.transports[0].credential_configured is True


async def test_status_never_renders_a_secret(tmp_path: Path) -> None:
    config = settings(tmp_path)

    status = await GatewayHealth(config, unit=_unit(config, tmp_path)).status()
    rendered = json.dumps(status.model_dump(mode="json"), default=str)

    assert SECRET not in rendered
    assert "bot_token" not in rendered


async def test_status_reports_uncertain_and_in_doubt_counts(tmp_path: Path) -> None:
    config = settings(tmp_path)
    messaging = MessagingStore(config)
    jobs = JobRunStore(config)
    await messaging.initialize()
    await jobs.initialize()
    message = await store_inbound(messaging, build_inbound())
    await messaging.claim_inbox(message.id, owner="dead", lease_seconds=1)
    later = datetime.now(UTC) + timedelta(seconds=10)
    await messaging.recover_inbox_claim(message.id, status="uncertain", now=later)
    run = job_run()
    await jobs.insert(run, scope=PROFILE_SCOPE)
    action_id = await reserve_effect(jobs, run)
    await jobs.strand_reserved_action(action_id, scope=PROFILE_SCOPE, error="interrupted")

    status = await GatewayHealth(config, unit=_unit(config, tmp_path)).status()

    assert status.uncertain_count == 1
    assert status.in_doubt_count == 1


async def test_status_reports_the_live_process_lock(tmp_path: Path) -> None:
    config = settings(tmp_path)
    health = GatewayHealth(config, unit=_unit(config, tmp_path))
    lock = GatewayLock(config)

    before = await health.status()
    assert before.lock_owner is None and before.lock_active is False

    with lock:
        during = await health.status()
    after = await health.status()

    assert during.lock_active is True
    assert during.lock_owner is not None and during.lock_owner.pid == os.getpid()
    assert during.lock_age_seconds is not None and during.lock_age_seconds >= 0
    # The record survives release so an operator can see who ran last.
    assert after.lock_active is False and after.lock_owner is not None


async def test_doctor_passes_complete_configuration_and_reports_private_store_files(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)

    report = await GatewayHealth(config, unit=_unit(config, tmp_path)).doctor()

    assert report.ok, [check.detail for check in report.failures]
    names = {check.name for check in report.checks}
    assert "gateway.enabled" in names
    assert "process lock" in names
    assert "service unit" in names
    assert "telegram credential personal/bot" in names
    for name in ("gateway", "messaging", "sessions", "executions"):
        check = next(item for item in report.checks if item.name == f"{name} store file")
        assert check.status == "ok"


async def test_doctor_fails_when_a_gateway_route_has_no_messaging_target(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    stripped = config.model_copy(
        update={"messaging": config.messaging.model_copy(update={"routes": {}})}
    )

    report = await GatewayHealth(stripped, unit=_unit(stripped, tmp_path)).doctor()

    assert not report.ok
    assert any("messaging.routes.owner" in check.detail for check in report.failures)


async def test_doctor_reports_gateway_profiles_missing_from_route_clearance(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    gateway_route = config.gateway.routes["owner"].model_copy(update={"access_profiles": ["work"]})
    work_definition = config.profiles.definitions["work"].model_copy(
        update={"allowed_providers": ["openrouter", "anthropic", "claude_code"]}
    )
    mismatched = config.model_copy(
        update={
            "gateway": config.gateway.model_copy(update={"routes": {"owner": gateway_route}}),
            "profiles": config.profiles.model_copy(
                update={
                    "definitions": {
                        **config.profiles.definitions,
                        "work": work_definition,
                    }
                }
            ),
        }
    )

    report = await GatewayHealth(mismatched, unit=_unit(mismatched, tmp_path)).doctor()

    assert any(
        "does not accept gateway profile(s): work" in check.detail for check in report.failures
    )


async def test_doctor_reports_invalid_capability_inventory_before_runtime_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_sandbox_runtime(monkeypatch)
    config_raw = settings(tmp_path).model_dump(mode="python")
    config_raw["authority"] = {
        "enabled": True,
        "capabilities": {"sandbox_reservation": {"enabled": True}},
    }
    config = RickySettings.model_validate(config_raw)

    class MismatchedEvaluator(SandboxGuardrailEvaluator):
        tools = frozenset({"wrong_tool"})

    monkeypatch.setattr(
        "ricky.runtime.composition.built_in_guardrail_evaluators",
        lambda: (MismatchedEvaluator(),),
    )

    report = await GatewayHealth(config, unit=_unit(config, tmp_path)).doctor()

    failure = next(
        check for check in report.failures if check.name.startswith("capability inventory")
    )
    assert "tools differ" in failure.detail


async def test_doctor_fails_on_a_group_readable_store_directory(tmp_path: Path) -> None:
    config = settings(tmp_path)
    executions = ExecutionStore(config)
    await executions.initialize()
    # The execution store creates its directory privately but does not re-tighten
    # an existing one, so a later widening must be reported rather than hidden.
    executions.db_path.parent.chmod(0o755)

    report = await GatewayHealth(config, unit=_unit(config, tmp_path)).doctor()

    assert not report.ok
    assert any("group or world accessible" in check.detail for check in report.failures)
    executions.db_path.parent.chmod(0o700)


async def test_doctor_never_renders_a_secret(tmp_path: Path) -> None:
    config = settings(tmp_path)

    report = await GatewayHealth(config, unit=_unit(config, tmp_path)).doctor()
    rendered = json.dumps(report.model_dump(mode="json"))

    assert SECRET not in rendered
    credential = next(
        check for check in report.checks if check.name == "telegram credential personal/bot"
    )
    assert credential.status == "ok"
    assert credential.detail == "bot token is configured"


async def test_doctor_warns_when_the_installed_unit_drifts(tmp_path: Path) -> None:
    config = settings(tmp_path)
    unit = _unit(config, tmp_path)
    unit.install()
    unit.unit_path.write_text(unit.render() + "\n# hand edited\n", encoding="utf-8")

    report = await GatewayHealth(config, unit=unit).doctor()

    drift = next(check for check in report.checks if check.name == "service unit")
    assert drift.status == "warn"
    assert "does not match the current configuration" in drift.detail


async def test_doctor_warns_rather_than_failing_when_the_gateway_is_disabled(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path, enabled=False)

    report = await GatewayHealth(config, unit=_unit(config, tmp_path)).doctor()

    enabled = next(check for check in report.checks if check.name == "gateway.enabled")
    assert enabled.status == "warn"
    assert report.ok


async def test_status_initialization_creates_every_database_private(tmp_path: Path) -> None:
    config = settings(tmp_path)
    health = GatewayHealth(config, unit=_unit(config, tmp_path))
    await health.status()

    paths = (
        health.messaging.db_path,
        health.notifications.db_path,
        health.gateway.db_path,
        health.sessions.db_path,
        health.executions.db_path,
        health.jobs.path,
        health.authority.db_path,
    )
    for path in paths:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
        assert stat.S_IMODE(path.parent.stat().st_mode) & 0o077 == 0, path.parent
