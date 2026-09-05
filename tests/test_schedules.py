"""Offline schedule store, cron backend, service, and CLI acceptance."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from ricky.config import RickySettings, load_settings_at
from ricky.interfaces.cli.app import app
from ricky.jobs.registry import JobRegistry, context_definition_digest
from ricky.jobs.runner import JobConfigurationError, JobRunner, runtime_policy_digest
from ricky.jobs.spec import TaskSourceSpec
from ricky.jobs.store import JobRunStore
from ricky.jobs.tools import RecordItemDispositionTool
from ricky.jobs.types import JobApprovalEnvelope, JobApprovalTool, JobRun
from ricky.profiles import ProfileScope
from ricky.schedules.cron import (
    BEGIN_MARKER,
    END_MARKER,
    CommandResult,
    CronError,
    UserCrontabBackend,
    merge_managed_block,
    parse_managed_crontab,
    render_fragment,
)
from ricky.schedules.render import render_approval
from ricky.schedules.service import ScheduleService, ScheduleServiceError
from ricky.schedules.store import ScheduleReference, ScheduleStore, ScheduleStoreError
from ricky.schedules.types import ScheduleSpec, validate_cron_expression


class FakeCrontabRunner:
    """In-memory crontab command; install input is read from the temp path."""

    def __init__(self, current: str | None = None) -> None:
        self.current = current
        self.commands: list[tuple[str, ...]] = []
        self.fail_install = False
        self.corrupt_readback = False

    async def run(self, args: Any) -> CommandResult:
        command = tuple(str(item) for item in args)
        self.commands.append(command)
        if command[-1] == "-l":
            if self.current is None:
                return CommandResult(returncode=1, stderr="no crontab for test-user")
            return CommandResult(returncode=0, stdout=self.current)
        if self.fail_install:
            return CommandResult(returncode=1, stderr="install refused")
        installed = Path(command[1]).read_text(encoding="utf-8")
        self.current = installed + ("# corrupt\n" if self.corrupt_readback else "")
        return CommandResult(returncode=0)


class BlockingInstallRunner(FakeCrontabRunner):
    def __init__(self, current: str) -> None:
        super().__init__(current)
        self.install_started = asyncio.Event()
        self.release_install = asyncio.Event()

    async def run(self, args: Any) -> CommandResult:
        command = tuple(str(item) for item in args)
        if command[-1] != "-l":
            self.install_started.set()
            await self.release_install.wait()
        return await super().run(args)


def _settings(tmp_path: Path, *, log_limit: int = 4_096) -> RickySettings:
    return RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user-data"),
            "project_data_dir": ".ricky",
            "default_provider": "openrouter",
            "providers": {
                "openrouter": {"default_model": "test-model"},
                "anthropic": {"default_model": "claude-test"},
            },
            "memory": {"enabled": False},
            "workflow": {"enabled": False},
            "google": {"accounts": {}},
            "google_oauth_clients": {},
            "schedules": {"launcher_log_byte_limit": log_limit},
        }
    )


def _project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='schedule-test'\nversion='0.1'\n",
        encoding="utf-8",
    )
    (tmp_path / "user-data").mkdir(exist_ok=True)
    (tmp_path / "user-data" / "ricky.toml").write_text(
        """default_provider = "openrouter"
[providers.openrouter]
default_model = "test-model"
[providers.anthropic]
default_model = "claude-test"
[memory]
enabled = false
[workflow]
enabled = false
""",
        encoding="utf-8",
    )
    return tmp_path


def _scope(settings: RickySettings, *, all_profiles: bool = False) -> ProfileScope:
    return settings.resolve_profile_scope(
        access_profiles=settings.profiles.enabled if all_profiles else (),
    )


def _bundle_text(*, goal: str = "Report safely.") -> str:
    return f"""version = 3
name = "brief"
description = "A scheduled report."
provider = "openrouter"
model = "test-model"
goal = {json.dumps(goal)}
[budget]
wall_clock_seconds = 10
iterations = 2
max_completion_tokens_per_request = 123
effect_calls = 0
[tools]
allow = []
"""


def _bundle(project: Path, *, goal: str = "Report safely.") -> Path:
    """Author the scheduled job in the primary profile's user job root.

    ``project`` remains the schedule's project binding, which grants filesystem
    authority only. Job discovery never reads it.
    """

    bundle = project / "user-data" / "profiles" / "personal" / "jobs" / "brief"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "job.toml").write_text(_bundle_text(goal=goal), encoding="utf-8")
    return bundle


def _schedule(project: Path, suffix: str = "0") -> ScheduleSpec:
    now = datetime.now(UTC)
    return ScheduleSpec(
        id=f"sched_{suffix * 24}",
        job_name="personal/brief",
        cron="*/15 * * * *",
        project_root=str(project.resolve()),
        profile_scope=ProfileScope.create("personal"),
        approved_spec_digest="a" * 64,
        approved_runtime_policy_digest="b" * 64,
        created_at=now,
        updated_at=now,
    )


def _source_scope(**changes: object) -> str:
    payload: dict[str, object] = {
        "tags_any": ["kind:triage"],
        "tags_all": [],
        "tags_none": [],
        "execution_modes": [],
        "statuses": ["open", "waiting"],
        "waiting_on": [],
        "due_before": "2026-08-25T17:00:00+00:00",
        "text": "invoice",
    }
    payload.update(changes)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _approval_envelope(**changes: object) -> JobApprovalEnvelope:
    values: dict[str, object] = {
        "provider": "openrouter",
        "tools": {
            "existing": JobApprovalTool(
                contract_digest="1" * 64,
                risk="read_only",
                effect_kind="none",
                unattended="allowed",
            )
        },
        "mutating_tools": (),
        "source_scopes": {"task:queue": _source_scope()},
        "google_accounts": {"personal/main": "main@example.com"},
        "workflow_args": {"query": "is:unread"},
        "effect_calls": 1,
    }
    values.update(changes)
    return JobApprovalEnvelope.model_validate(values)


@pytest.mark.parametrize(
    "expression",
    [
        "@hourly",
        "* * * *",
        "* * * * * command",
        "* * * * * # comment",
        "PATH=/tmp * * * * *",
        "*/0 * * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * * JAN *",
        "10-2 * * * *",
        "* * * * *\n",
    ],
)
def test_strict_five_field_cron_rejects_commands_and_invalid_tokens(
    expression: str,
) -> None:
    with pytest.raises(ValueError):
        validate_cron_expression(expression)


def test_schedule_contract_is_strict_and_json_round_trip_safe(tmp_path: Path) -> None:
    schedule = _schedule(tmp_path)
    assert ScheduleSpec.model_validate_json(schedule.model_dump_json()) == schedule
    with pytest.raises(ValidationError):
        ScheduleSpec.model_validate(schedule.model_dump() | {"command": "echo no"})


@pytest.mark.parametrize(
    ("previous", "current", "schedule_changes", "revision_changed", "expected"),
    [
        pytest.param(
            _approval_envelope(),
            _approval_envelope(),
            {},
            False,
            (),
            id="unchanged",
        ),
        pytest.param(
            _approval_envelope(),
            _approval_envelope(),
            {"cron": "0 9 * * 1"},
            False,
            ("schedule timing changed: */15 * * * * -> 0 9 * * 1",),
            id="timing-change",
        ),
        pytest.param(
            None,
            _approval_envelope(),
            {},
            True,
            ("prior approval envelope is unavailable",),
            id="legacy-envelope-unavailable",
        ),
        pytest.param(
            _approval_envelope(),
            _approval_envelope(provider="anthropic"),
            {},
            True,
            ("provider changed: openrouter -> anthropic",),
            id="provider-change",
        ),
        pytest.param(
            _approval_envelope(),
            _approval_envelope(
                tools={
                    "existing": _approval_envelope().tools["existing"],
                    "new_tool": JobApprovalTool(
                        contract_digest="2" * 64,
                        risk="read_only",
                        effect_kind="none",
                        unattended="allowed",
                    ),
                }
            ),
            {},
            True,
            ("tool added: new_tool",),
            id="tool-added",
        ),
        pytest.param(
            _approval_envelope(),
            _approval_envelope(
                tools={
                    "existing": JobApprovalTool(
                        contract_digest="2" * 64,
                        risk="read_only",
                        effect_kind="none",
                        unattended="allowed",
                    )
                }
            ),
            {},
            True,
            ("tool contract changed: existing",),
            id="tool-contract-change",
        ),
        pytest.param(
            _approval_envelope(),
            _approval_envelope(tools={}),
            {},
            True,
            (),
            id="tool-removal",
        ),
        pytest.param(
            _approval_envelope(),
            _approval_envelope(mutating_tools=("existing",)),
            {},
            True,
            ("standing mutation added: existing",),
            id="standing-mutation-added",
        ),
        pytest.param(
            _approval_envelope(mutating_tools=("existing",)),
            _approval_envelope(),
            {},
            True,
            (),
            id="standing-mutation-removed",
        ),
        pytest.param(
            _approval_envelope(),
            _approval_envelope(
                source_scopes={
                    "task:queue": _source_scope(),
                    "task:new": _source_scope(text=None),
                }
            ),
            {},
            True,
            ("data source added: task:new",),
            id="source-added",
        ),
        pytest.param(
            _approval_envelope(),
            _approval_envelope(source_scopes={"task:queue": _source_scope(statuses=["open"])}),
            {},
            True,
            (),
            id="task-source-narrowed",
        ),
        pytest.param(
            _approval_envelope(),
            _approval_envelope(
                source_scopes={
                    "task:queue": _source_scope(statuses=["open", "waiting", "completed"])
                }
            ),
            {},
            True,
            ("data source scope changed: task:queue",),
            id="task-source-widened",
        ),
        pytest.param(
            _approval_envelope(
                source_scopes={"task:queue": _source_scope(due_before="2026-08-25T17:00:00")}
            ),
            _approval_envelope(
                source_scopes={"task:queue": _source_scope(due_before="2026-08-25T18:00:00+00:00")}
            ),
            {},
            True,
            ("data source scope changed: task:queue",),
            id="legacy-naive-due-boundary-widened",
        ),
        pytest.param(
            _approval_envelope(),
            _approval_envelope(source_scopes={}),
            {},
            True,
            (),
            id="source-removed",
        ),
        pytest.param(
            _approval_envelope(),
            _approval_envelope(
                google_accounts={
                    "personal/main": "main@example.com",
                    "personal/secondary": "secondary@example.com",
                }
            ),
            {},
            True,
            ("Google account became available: personal/secondary",),
            id="google-account-added",
        ),
        pytest.param(
            _approval_envelope(),
            _approval_envelope(google_accounts={"personal/main": "replacement@example.com"}),
            {},
            True,
            ("Google account identity changed: personal/main",),
            id="google-identity-change",
        ),
        pytest.param(
            _approval_envelope(),
            _approval_envelope(google_accounts={}),
            {},
            True,
            (),
            id="google-account-removed",
        ),
        pytest.param(
            _approval_envelope(),
            _approval_envelope(workflow_args={"query": "is:starred"}),
            {},
            True,
            ("locked workflow arguments changed",),
            id="workflow-args-change",
        ),
        pytest.param(
            _approval_envelope(),
            _approval_envelope(effect_calls=2),
            {},
            True,
            ("effect budget increased: 1 -> 2",),
            id="effect-budget-increased",
        ),
        pytest.param(
            _approval_envelope(),
            _approval_envelope(effect_calls=0),
            {},
            True,
            (),
            id="effect-budget-reduced",
        ),
    ],
)
def test_schedule_reapproval_classification_matrix_is_exact(
    tmp_path: Path,
    previous: JobApprovalEnvelope | None,
    current: JobApprovalEnvelope,
    schedule_changes: dict[str, object],
    revision_changed: bool,
    expected: tuple[str, ...],
) -> None:
    schedule = _schedule(tmp_path).model_copy(
        update={
            "approved_authority": previous,
            "approved_cron": "*/15 * * * *",
            **schedule_changes,
        }
    )

    reasons = ScheduleService._reapproval_reasons(
        schedule,
        current,
        revision_changed=revision_changed,
    )

    assert reasons == list(expected)


@pytest.mark.asyncio
async def test_schedule_store_atomic_crud_modes_and_concurrency(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = ScheduleStore(settings, scope=_scope(settings))
    first, second = _schedule(tmp_path, "1"), _schedule(tmp_path, "2")
    await asyncio.gather(store.create(first), store.create(second))
    assert [item.id for item in await store.list()] == [first.id, second.id]
    assert oct(store.path.stat().st_mode & 0o777) == "0o600"
    assert oct(store.root.stat().st_mode & 0o777) == "0o700"
    changed = first.model_copy(update={"cron": "0 9 * * 1"})
    await store.replace(changed)
    assert (await store.get(first.id)).cron == "0 9 * * 1"
    assert (await store.remove(second.id)).id == second.id
    assert "[[schedules]]" in store.path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_schedule_store_reference_scan_reads_the_whole_registry(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    wide = _scope(settings, all_profiles=True)
    narrow = _scope(settings)
    now = datetime.now(UTC)
    schedule = ScheduleSpec(
        id="sched_" + "3" * 24,
        job_name="personal/brief",
        cron="*/15 * * * *",
        project_root=str(tmp_path.resolve()),
        profile_scope=wide,
        approved_spec_digest="a" * 64,
        approved_runtime_policy_digest="b" * 64,
        created_at=now,
        updated_at=now,
    )
    await ScheduleStore(settings, scope=wide).create(schedule)
    scoped = ScheduleStore(settings, scope=narrow)

    # Every scoped path hides a schedule pinned outside the issued scope, so a
    # lifecycle refusal cannot be built on one.
    assert await scoped.list() == []
    with pytest.raises(ScheduleStoreError, match="not found"):
        await scoped.get(schedule.id)

    references = await scoped.list_references()

    assert references == (ScheduleReference(id=schedule.id, profile_scope=wide),)
    # Identity and pinned profiles only: the scan never returns content.
    assert tuple(field.name for field in dataclasses.fields(ScheduleReference)) == (
        "id",
        "profile_scope",
    )


def test_renderer_uses_only_fixed_argv_and_quotes_hostile_paths(tmp_path: Path) -> None:
    project = tmp_path / "project with spaces % value"
    project.mkdir()
    schedule = _schedule(project)
    fragment = render_fragment(
        [schedule],
        ricky_executable=Path("/opt/ricky tools/ricky"),
        log_dir=(tmp_path / "log dir").resolve(),
    )
    assert fragment.startswith(BEGIN_MARKER + "\n")
    assert fragment.endswith(END_MARKER + "\n")
    assert "--project" in fragment and "schedule invoke" in fragment
    assert schedule.id in fragment
    assert "Report safely" not in fragment
    assert r"\%" in fragment
    assert ">>" in fragment and "2>&1" in fragment
    assert "umask 077; : >" in fragment


def test_marker_parser_preserves_unrelated_content_and_rejects_ambiguity() -> None:
    unrelated = "MAILTO=me@example.com\n5 4 * * * /usr/bin/backup\n"
    fragment = f"{BEGIN_MARKER}\n0 * * * * /fixed\n{END_MARKER}\n"
    merged = merge_managed_block(unrelated, fragment)
    parsed = parse_managed_crontab(merged)
    assert parsed.prefix == unrelated
    assert parsed.separator == "\n"
    assert parsed.block == fragment
    assert parsed.without_block == unrelated
    replacement = fragment.replace("0 *", "15 *")
    assert merge_managed_block(merged, replacement) == unrelated + "\n" + replacement
    without_final_newline = "MAILTO=me@example.com"
    installed = merge_managed_block(without_final_newline, fragment)
    assert parse_managed_crontab(installed).without_block == without_final_newline
    malformed = [
        f"{BEGIN_MARKER}\n",
        f"{END_MARKER}\n",
        f"{BEGIN_MARKER}\n{BEGIN_MARKER}\n{END_MARKER}\n",
        f"prefix {BEGIN_MARKER}\n{END_MARKER}\n",
    ]
    for value in malformed:
        with pytest.raises(CronError):
            parse_managed_crontab(value)


@pytest.mark.asyncio
async def test_backend_sync_backup_verify_and_uninstall_preserve_other_lines(
    tmp_path: Path,
) -> None:
    unrelated = "MAILTO=me@example.com\n5 4 * * * /usr/bin/backup\n"
    runner = FakeCrontabRunner(unrelated)
    backend = UserCrontabBackend(_settings(tmp_path), runner=runner)
    fragment = f"{BEGIN_MARKER}\n0 * * * * /fixed\n{END_MARKER}\n"
    applied = await backend.sync(fragment)
    assert applied.changed and applied.backup_path is not None
    assert applied.backup_path.read_text(encoding="utf-8") == unrelated
    assert oct(applied.backup_path.stat().st_mode & 0o777) == "0o600"
    assert backend.fragment_path.read_text(encoding="utf-8") == fragment
    assert runner.current == unrelated + "\n" + fragment
    command_count = len(runner.commands)
    unchanged = await backend.sync(fragment)
    assert not unchanged.changed and len(runner.commands) == command_count + 1
    removed = await backend.uninstall()
    assert removed.changed and runner.current == unrelated
    assert backend.fragment_path.exists(), "uninstall retains the inspectable fragment"


@pytest.mark.asyncio
async def test_store_mutation_waits_for_complete_reconciliation_lock(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = ScheduleStore(settings, scope=_scope(settings))
    backend = UserCrontabBackend(settings, runner=FakeCrontabRunner())
    mutation: asyncio.Task[ScheduleSpec]
    async with backend.reconciliation_lock():
        mutation = asyncio.create_task(store.create(_schedule(tmp_path, "3")))
        await asyncio.sleep(0.01)
        assert not mutation.done()
    assert (await mutation).id == f"sched_{'3' * 24}"


@pytest.mark.asyncio
async def test_backend_failures_never_hide_install_or_verification_state(
    tmp_path: Path,
) -> None:
    original = "1 2 * * * /existing\n"
    fragment = f"{BEGIN_MARKER}\n{END_MARKER}\n"
    install_failure = FakeCrontabRunner(original)
    install_failure.fail_install = True
    backend = UserCrontabBackend(_settings(tmp_path), runner=install_failure)
    with pytest.raises(CronError, match="install refused"):
        await backend.sync(fragment)
    assert install_failure.current == original
    assert list(backend.backup_dir.iterdir())

    mismatch = FakeCrontabRunner(original)
    mismatch.corrupt_readback = True
    mismatch_backend = UserCrontabBackend(_settings(tmp_path / "other"), runner=mismatch)
    with pytest.raises(CronError, match="did not match"):
        await mismatch_backend.sync(fragment)
    assert list(mismatch_backend.backup_dir.iterdir())


@pytest.mark.asyncio
async def test_install_cancellation_waits_for_readback_and_releases_lock(
    tmp_path: Path,
) -> None:
    original = "MAILTO=x\n"
    fragment = f"{BEGIN_MARKER}\n{END_MARKER}\n"
    runner = BlockingInstallRunner(original)
    backend = UserCrontabBackend(_settings(tmp_path), runner=runner)
    task = asyncio.create_task(backend.sync(fragment))
    await runner.install_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    runner.release_install.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert parse_managed_crontab(runner.current or "").block == fragment
    async with backend.reconciliation_lock():
        pass


@pytest.mark.asyncio
async def test_service_crud_drift_approval_sync_and_doctor_are_provider_free(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    bundle = _bundle(project)
    settings = _settings(tmp_path)
    fake_crontab = FakeCrontabRunner("MAILTO=x\n")
    backend = UserCrontabBackend(settings, runner=fake_crontab)
    service = ScheduleService(
        settings,
        profile_scope=_scope(settings, all_profiles=True),
        project_root=project,
        backend=backend,
        ricky_executable=Path("/usr/bin/ricky"),
    )
    schedule = await service.create(
        "brief",
        "*/15 * * * *",
        profile_scope=_scope(settings),
    )
    loaded = JobRegistry(
        settings,
        profile_scope=schedule.profile_scope,
    ).load(schedule.job_name)
    approval_text = render_approval(schedule, loaded)
    assert "resolved tools: (none)" in approval_text
    assert "source scopes: (none)" in approval_text
    assert "locked workflow arguments: (none)" in approval_text
    assert "approved cron: */15 * * * *" in approval_text
    assert "runtime revision digest:" in approval_text
    assert (await service.show(schedule.id)).state == "ready"

    original_approval = schedule.approved_spec_digest
    changed_cron = await service.update_cron(schedule.id, "0 9 * * 1")
    assert changed_cron.approved_spec_digest == original_approval
    (bundle / "job.toml").write_text(
        (bundle / "job.toml")
        .read_text(encoding="utf-8")
        .replace("Report safely.", "Report changed behavior."),
        encoding="utf-8",
    )
    assert (await service.show(schedule.id)).state == "approval_required"
    drift_sync = await service.sync()
    assert drift_sync.installed == []
    assert drift_sync.approval_required == [schedule.id]
    assert schedule.id not in (fake_crontab.current or "")

    before, approved, changes = await service.approve(schedule.id)
    assert before.state == "approval_required"
    assert approved.approved_spec_digest != original_approval
    assert any(change.startswith("goal:") for change in changes)
    synced = await service.sync()
    assert synced.installed == [schedule.id]
    assert schedule.id in (fake_crontab.current or "")
    assert (await service.doctor()).healthy

    fake_crontab.current = (fake_crontab.current or "").replace("0 9", "1 9")
    report = await service.doctor()
    assert not report.healthy
    assert any(issue.code == "installed_drift" for issue in report.issues)
    disabled = await service.set_enabled(schedule.id, False)
    assert not disabled.enabled
    assert (await service.show(schedule.id)).state == "disabled"
    assert (await service.remove(schedule.id)).id == schedule.id


@pytest.mark.asyncio
async def test_model_and_goal_edits_need_validation_refresh_not_reapproval(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    bundle = _bundle(project)
    settings = _settings(tmp_path)
    service = ScheduleService(settings, profile_scope=_scope(settings), project_root=project)
    schedule = await service.create("brief", "0 * * * *", profile_scope=_scope(settings))
    source = bundle / "job.toml"
    source.write_text(
        source.read_text(encoding="utf-8").replace('model = "test-model"', 'model = "other"'),
        encoding="utf-8",
    )

    model_change = await service.show(schedule.id)
    assert model_change.state == "validation_required"
    _, refreshed_model, _ = await service.refresh(schedule.id)
    assert refreshed_model.approved_authority == schedule.approved_authority
    source.write_text(
        source.read_text(encoding="utf-8").replace("Report safely.", "Report more clearly."),
        encoding="utf-8",
    )

    goal_change = await service.show(schedule.id)
    assert goal_change.state == "validation_required"
    _, refreshed_goal, changes = await service.refresh(schedule.id)
    assert refreshed_goal.approved_authority == schedule.approved_authority
    assert any(change.startswith("goal:") for change in changes)
    assert (await service.show(schedule.id)).state == "ready"


@pytest.mark.asyncio
async def test_schedule_blocks_goal_change_until_context_choice_is_authored(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    bundle = _bundle(project)
    settings = _settings(tmp_path)
    scope = _scope(settings)
    service = ScheduleService(settings, profile_scope=scope, project_root=project)
    schedule = await service.create("brief", "0 * * * *", profile_scope=scope)
    loaded = JobRegistry(settings, profile_scope=scope).load("brief")
    now = datetime.now(UTC)
    prior = JobRun(
        id="jobrun_prior_context",
        job_name=loaded.resource.qualified,
        spec_digest=loaded.digest,
        provider="openrouter",
        model="test-model",
        profile_scope=scope,
        session_id="session_prior_context",
        outcome="succeeded",
        started_at=now,
        finished_at=now,
        transcript_path=str(tmp_path / "prior.jsonl"),
        runtime_policy_digest=runtime_policy_digest(loaded.spec, settings, scope),
        context_lineage=1,
        context_revision=1,
        context_definition_digest=context_definition_digest(loaded),
    )
    store = JobRunStore(settings)
    await store.initialize()
    await store.insert(prior, scope=scope)
    source = bundle / "job.toml"
    source.write_text(
        source.read_text(encoding="utf-8").replace("Report safely.", "Changed responsibility."),
        encoding="utf-8",
    )

    inspection = await service.show(schedule.id)
    assert inspection.state == "lineage_required"
    with pytest.raises(JobConfigurationError, match="context decision"):
        await service.refresh(schedule.id)

    source.write_text(
        source.read_text(encoding="utf-8") + "[context]\nlineage = 1\nrevision = 2\n",
        encoding="utf-8",
    )
    assert (await service.show(schedule.id)).state == "validation_required"
    await service.refresh(schedule.id)
    assert (await service.show(schedule.id)).state == "ready"


@pytest.mark.asyncio
async def test_authority_expansion_requires_approval_but_reduction_only_refreshes(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    bundle = _bundle(project)
    settings = _settings(tmp_path)
    scope = _scope(settings)
    service = ScheduleService(settings, profile_scope=scope, project_root=project)
    schedule = await service.create("brief", "0 * * * *", profile_scope=scope)
    source = bundle / "job.toml"
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "effect_calls = 0\n[tools]\nallow = []",
            'effect_calls = 1\n[tools]\nallow = ["run_shell"]\n'
            '[permissions]\nallow_mutating = ["run_shell"]',
        ),
        encoding="utf-8",
    )

    expanded = await service.show(schedule.id)
    assert expanded.state == "approval_required"
    assert expanded.detail is not None and "tool added: run_shell" in expanded.detail
    with pytest.raises(ScheduleServiceError, match="reapproval is required"):
        await service.refresh(schedule.id)
    _, approved, _ = await service.approve(schedule.id)
    assert approved.approved_authority is not None

    source.write_text(_bundle_text(), encoding="utf-8")
    reduced = await service.show(schedule.id)
    assert reduced.state == "validation_required"
    await service.refresh(schedule.id)
    assert (await service.show(schedule.id)).state == "ready"


@pytest.mark.asyncio
async def test_injected_disposition_contract_change_requires_validation_not_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    bundle = _bundle(project)
    source = bundle / "job.toml"
    source.write_text(
        source.read_text(encoding="utf-8")
        + """
[[task_sources]]
name = "queue"
statuses = ["open"]
""",
        encoding="utf-8",
    )
    settings = _settings(tmp_path)
    scope = _scope(settings)
    service = ScheduleService(settings, profile_scope=scope, project_root=project)
    schedule = await service.create("brief", "0 * * * *", profile_scope=scope)
    assert schedule.approved_authority is not None
    assert "record_item_disposition" not in schedule.approved_authority.tools
    monkeypatch.setattr(RecordItemDispositionTool, "contract_version", 2, raising=False)

    inspection = await service.show(schedule.id)
    assert inspection.state == "validation_required"
    _, refreshed, _ = await service.refresh(schedule.id)
    assert refreshed.approved_authority == schedule.approved_authority
    assert refreshed.approved_runtime_policy_digest != schedule.approved_runtime_policy_digest


@pytest.mark.asyncio
async def test_injected_disposition_tool_must_remain_confined_ricky_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    bundle = _bundle(project)
    source = bundle / "job.toml"
    source.write_text(
        source.read_text(encoding="utf-8")
        + """
[[task_sources]]
name = "queue"
statuses = ["open"]
""",
        encoding="utf-8",
    )
    settings = _settings(tmp_path)
    scope = _scope(settings)
    monkeypatch.setattr(RecordItemDispositionTool, "effect_kind", "external")

    with pytest.raises(JobConfigurationError, match="internal disposition tool"):
        await JobRunner(settings, project_root=project).validate("brief", profile_scope=scope)


@pytest.mark.asyncio
async def test_schedule_snapshot_captures_google_accounts_used_by_runtime(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    profile_root = tmp_path / "user-data" / "profiles" / "personal"
    profile_root.mkdir(parents=True)
    profile_config = profile_root / "ricky.toml"
    profile_secrets = profile_root / ".secrets.toml"
    profile_config.write_text(
        """[google.accounts.main]
email = "main@example.com"
""",
        encoding="utf-8",
    )
    profile_secrets.write_text(
        """[google_oauth_clients.main]
client_id = "main-client"
client_secret = "main-secret"
""",
        encoding="utf-8",
    )
    bundle = _bundle(project)
    source = bundle / "job.toml"
    source.write_text(
        source.read_text(encoding="utf-8").replace("allow = []", 'allow = ["gmail_search"]'),
        encoding="utf-8",
    )
    settings = _settings(tmp_path)
    scope = _scope(settings)
    service = ScheduleService(settings, profile_scope=scope, project_root=project)
    schedule = await service.create("brief", "0 * * * *", profile_scope=scope)

    assert schedule.approved_authority is not None
    assert schedule.approved_authority.google_accounts == {"personal/main": "main@example.com"}
    loaded = JobRegistry(
        settings,
        profile_scope=schedule.profile_scope,
    ).load(schedule.job_name)
    assert "personal/main (main@example.com)" in render_approval(schedule, loaded)


def test_task_source_due_before_requires_timezone() -> None:
    with pytest.raises(ValidationError, match="must include a timezone offset"):
        TaskSourceSpec.model_validate({"name": "queue", "due_before": "2026-08-25T18:00:00"})


@pytest.mark.asyncio
async def test_schedule_create_allows_explicit_unattended_destructive_authority(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    bundle = _bundle(project)
    source = bundle / "job.toml"
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "allow = []",
            'allow = ["run_shell"]\n[permissions]\nallow_mutating = ["run_shell"]',
        ),
        encoding="utf-8",
    )
    settings = _settings(tmp_path)
    service = ScheduleService(settings, profile_scope=_scope(settings), project_root=project)
    schedule = await service.create(
        "brief",
        "0 * * * *",
        profile_scope=_scope(settings),
    )
    assert schedule.job_name == "personal/brief"
    assert [item.id for item in await service.store.list()] == [schedule.id]


@pytest.mark.asyncio
async def test_invoke_fails_closed_on_drift_records_provenance_and_bounds_log(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    bundle = _bundle(project)
    settings = _settings(tmp_path)
    service = ScheduleService(settings, profile_scope=_scope(settings), project_root=project)
    schedule = await service.create(
        "brief",
        "0 * * * *",
        profile_scope=_scope(settings),
    )
    log = service.backend.log_dir / f"{schedule.id}.log"
    log.parent.mkdir(parents=True)
    log.write_bytes(b"x" * 5_000)
    (bundle / "job.toml").write_text(
        (bundle / "job.toml")
        .read_text(encoding="utf-8")
        .replace("Report safely.", "Materially changed."),
        encoding="utf-8",
    )
    run = await service.invoke(schedule.id)
    assert run.outcome == "approval_required"
    assert run.trigger == "schedule" and run.trigger_id == schedule.id
    assert run.transcript_path is None and run.iterations == 0
    assert log.stat().st_size == 0
    assert await JobRunStore(settings).get(run.id, scope=_scope(settings)) == run
    with pytest.raises(ValueError, match="invalid opaque schedule id"):
        await service.invoke("../../escaped")
    assert not (tmp_path / "escaped.log").exists()


@pytest.mark.asyncio
async def test_invoke_passes_exact_approval_to_runner_and_records_schedule_trigger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    _bundle(project)
    settings = _settings(tmp_path)
    service = ScheduleService(settings, profile_scope=_scope(settings), project_root=project)
    schedule = await service.create(
        "brief",
        "0 * * * *",
        profile_scope=_scope(settings),
    )
    captured: dict[str, object] = {}

    async def fake_run(_self: Any, name: str, **kwargs: object) -> JobRun:
        captured.update(name=name, **kwargs)
        now = datetime.now(UTC)
        return JobRun(
            id="jobrun_scheduled",
            job_name=name,
            profile_scope=_scope(settings),
            spec_digest=schedule.approved_spec_digest,
            provider="openrouter",
            model="test-model",
            session_id="session_scheduled",
            outcome="succeeded",
            started_at=now,
            finished_at=now,
            trigger="schedule",
            trigger_id=schedule.id,
        )

    monkeypatch.setattr("ricky.schedules.service.JobRunner.run", fake_run)
    run = await service.invoke(schedule.id)
    assert run.outcome == "succeeded"
    assert captured == {
        "name": "personal/brief",
        "profile_scope": _scope(settings),
        "trigger": "schedule",
        "trigger_id": schedule.id,
        "expected_spec_digest": schedule.approved_spec_digest,
        "expected_runtime_policy_digest": schedule.approved_runtime_policy_digest,
    }


def test_cli_desired_state_lifecycle_never_calls_crontab(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    bundle = _bundle(project)
    monkeypatch.chdir(project)
    runner = CliRunner()
    added = runner.invoke(app, ["schedule", "add", "brief", "--cron", "*/15 * * * *"])
    assert added.exit_code == 0, added.output
    assert "not installed" in added.output
    assert "standing mutations" in added.output
    settings = load_settings_at(project / "user-data")
    schedules = asyncio.run(ScheduleStore(settings, scope=_scope(settings)).list())
    schedule_id = schedules[0].id
    listed = runner.invoke(app, ["schedule", "list"])
    assert listed.exit_code == 0 and schedule_id in listed.output
    source = bundle / "job.toml"
    source.write_text(
        source.read_text(encoding="utf-8").replace("Report safely.", "Report refreshed."),
        encoding="utf-8",
    )
    refreshed = runner.invoke(app, ["schedule", "refresh", schedule_id])
    assert refreshed.exit_code == 0, refreshed.output
    assert "Execution revision refreshed" in refreshed.output
    changed = runner.invoke(
        app,
        ["schedule", "set", schedule_id, "--cron", "0 9 * * 1"],
    )
    assert changed.exit_code == 0 and "approve the new timing" in changed.output
    assert not (Path(os.environ["RICKY_USER_DATA_DIR"]) / "cron" / "ricky.crontab").exists()


def test_jobs_runtime_schema_has_schedule_provenance_and_profile_scope() -> None:
    schema = JobRun.model_json_schema()["properties"]
    assert {
        "trigger",
        "trigger_id",
        "profile_scope",
        "context_lineage",
        "context_revision",
        "context_definition_digest",
    } <= set(schema)
