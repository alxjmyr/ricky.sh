"""Job bundle, snapshot, lock, and run-ledger contracts."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ricky.config import RickySettings
from ricky.jobs.lock import browser_worker_is_alive, browser_worker_lease, job_lock
from ricky.jobs.registry import JobRegistry
from ricky.jobs.spec import TaskSourceSpec
from ricky.jobs.store import SCHEMA_VERSION, JobRunStore, JobStoreError
from ricky.jobs.types import JobRun
from ricky.jobs.upgrade import JobsUpgradeAdapter
from ricky.profiles import ProfileScope

SCOPE = ProfileScope.create("personal")


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": ".ricky",
            "workflow": {"enabled": False},
            "memory": {"enabled": False},
            "google": {"accounts": {}},
            "google_oauth_clients": {},
        }
    )


async def test_phase7_browser_ledger_schema_migrates_additively(tmp_path: Path) -> None:
    store = JobRunStore(_settings(tmp_path))
    await store.initialize()
    with sqlite3.connect(store.path) as database:
        for table in (
            "browser_budget_reservations",
            "browser_action_evidence",
            "browser_navigation_checkpoints",
            "browser_attempt_budgets",
            "browser_attempts",
        ):
            database.execute(f"DROP TABLE {table}")
        database.execute("PRAGMA user_version = 10")

    before = store.path.read_bytes()
    with pytest.raises(JobStoreError, match="unsupported.*schema"):
        await store.initialize()
    assert store.path.read_bytes() == before

    adapter = JobsUpgradeAdapter((store.path,))
    target = adapter.discover(user_data_dir=Path(store.settings.user_data_dir))[0]
    inspection = adapter.inspect(target)
    assert inspection.state == "migration_required"
    assert adapter.preflight(inspection).backup_paths == (str(store.path),)
    (step,) = adapter.plan_steps(source_data_generation=1, target_data_generation=1)
    adapter.apply(step)
    assert adapter.verify(target).state == "current"

    with sqlite3.connect(store.path) as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        names = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {
        "browser_attempts",
        "browser_attempt_budgets",
        "browser_navigation_checkpoints",
        "browser_action_evidence",
        "browser_budget_reservations",
    } <= names


def _bundle(settings: RickySettings, name: str, body: str) -> Path:
    """Author one job bundle in the primary profile's user job root."""

    bundle = Path(settings.user_data_dir) / "profiles" / "personal" / "jobs" / name
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "job.toml").write_text(body, encoding="utf-8")
    return bundle


def _bundled_job(bundled_root: Path, name: str, body: str) -> Path:
    """Write one job bundle into the resources distributed with Ricky."""

    bundle = bundled_root / "jobs" / name
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "job.toml").write_text(body, encoding="utf-8")
    return bundle


def _toml(name: str, *, goal: str = 'goal = "Report."', tools: str = "") -> str:
    return f'''version = 3
name = "{name}"
description = "A test job."
provider = "openrouter"
model = "test-model"
{goal}
[budget]
wall_clock_seconds = 10
iterations = 2
max_completion_tokens_per_request = 123
effect_calls = 0
[tools]
allow = [{tools}]
'''


def test_registry_user_precedence_and_exact_snapshot(tmp_path: Path, bundled_root: Path) -> None:
    settings = _settings(tmp_path)
    user_bundle = _bundle(settings, "brief", _toml("brief"))
    _bundled_job(bundled_root, "brief", _toml("brief", goal='goal = "Wrong precedence."'))

    registry = JobRegistry(settings, profile_scope=SCOPE)
    loaded = registry.load("brief")
    snapshot = registry.snapshot(loaded)

    assert loaded.bundle_path == user_bundle.resolve()
    assert loaded.goal == "Report."
    assert (snapshot / "job.toml").read_bytes() == (user_bundle / "job.toml").read_bytes()
    resolved = json.loads((snapshot / "resolved.json").read_text(encoding="utf-8"))
    assert resolved["spec"]["provider"] == "openrouter"
    assert resolved["spec"]["model"] == "test-model"
    assert resolved["goal"] == "Report."
    assert oct((snapshot / "job.toml").stat().st_mode & 0o777) == "0o600"
    assert oct(snapshot.stat().st_mode & 0o777) == "0o700"


def test_instruction_file_is_digest_input_and_goal_source(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    bundle = _bundle(settings, "brief", _toml("brief", goal=""))
    (bundle / "INSTRUCTIONS.md").write_text("First instructions.\n", encoding="utf-8")
    registry = JobRegistry(settings, profile_scope=SCOPE)
    first = registry.load("brief")
    (bundle / "INSTRUCTIONS.md").write_text("Second instructions.\n", encoding="utf-8")
    second = registry.load("brief")

    assert first.goal == "First instructions."
    assert first.digest != second.digest
    assert dict(first.source_files)["INSTRUCTIONS.md"] == b"First instructions.\n"


def test_job_context_defaults_and_explicit_revision_round_trip(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    bundle = _bundle(settings, "brief", _toml("brief"))
    registry = JobRegistry(settings, profile_scope=SCOPE)
    assert registry.load("brief").spec.context.model_dump() == {"lineage": 1, "revision": 1}
    source = bundle / "job.toml"
    source.write_text(
        source.read_text(encoding="utf-8") + "[context]\nlineage = 3\nrevision = 7\n",
        encoding="utf-8",
    )

    loaded = registry.load("brief")

    assert loaded.spec.context.model_dump() == {"lineage": 3, "revision": 7}
    assert loaded.spec.model_validate_json(loaded.spec.model_dump_json()) == loaded.spec


def test_result_notification_defaults_to_always_and_never_is_snapshotted(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    bundle = _bundle(settings, "brief", _toml("brief"))
    registry = JobRegistry(settings, profile_scope=SCOPE)

    defaulted = registry.load("brief")

    assert defaulted.spec.result_notification == "always"
    default_manifest = json.loads(dict(defaulted.source_files)["resolved.json"])
    assert "result_notification" not in default_manifest["spec"]

    source = bundle / "job.toml"
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            'goal = "Report."\n',
            'goal = "Report."\nresult_notification = "never"\n',
        ),
        encoding="utf-8",
    )
    silent = registry.load("brief")

    assert silent.spec.result_notification == "never"
    silent_manifest = json.loads(dict(silent.source_files)["resolved.json"])
    assert silent_manifest["spec"]["result_notification"] == "never"
    assert silent.digest != defaulted.digest


def test_task_source_due_before_requires_offset_and_normalizes_to_utc() -> None:
    with pytest.raises(ValueError, match="must include a timezone offset"):
        TaskSourceSpec(name="queue", due_before=datetime(2026, 8, 25, 12))

    source = TaskSourceSpec(
        name="queue",
        due_before=datetime.fromisoformat("2026-08-25T12:00:00-05:00"),
    )

    assert source.due_before == datetime(2026, 8, 25, 17, tzinfo=UTC)


@pytest.mark.parametrize(
    "body, message",
    [
        (_toml("wrong"), "does not match bundle"),
        (_toml("brief") + "unknown_setting = 'value'\n", "invalid job definition"),
        (
            _toml("brief").replace(
                'goal = "Report."\n',
                'goal = "Report."\nresult_notification = "sometimes"\n',
            ),
            "invalid job definition",
        ),
        (_toml("brief", goal=""), "exactly one"),
    ],
)
def test_registry_rejects_invalid_bundle_shapes(tmp_path: Path, body: str, message: str) -> None:
    settings = _settings(tmp_path)
    _bundle(settings, "brief", body)
    with pytest.raises(ValueError, match=message):
        JobRegistry(settings, profile_scope=SCOPE).load("brief")


def test_registry_rejects_symlink_escape(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "job.toml").write_text(_toml("brief"), encoding="utf-8")
    jobs = Path(settings.user_data_dir) / "profiles" / "personal" / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "brief").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes jobs directory"):
        JobRegistry(settings, profile_scope=SCOPE).load("brief")


@pytest.mark.asyncio
async def test_run_store_schema_round_trip_and_history(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = JobRunStore(settings)
    await store.initialize()
    started = datetime.now(UTC)
    run = JobRun(
        id="jobrun_1",
        job_name="brief",
        spec_digest="a" * 64,
        provider="openrouter",
        model="test",
        profile_scope=SCOPE,
        session_id="session_1",
        started_at=started,
        result_notification="never",
    )
    await store.insert(run, scope=SCOPE)
    finished = run.model_copy(update={"outcome": "succeeded", "finished_at": datetime.now(UTC)})
    await store.finish(finished, scope=SCOPE)

    loaded = await store.get(run.id, scope=SCOPE)
    assert loaded == finished
    assert loaded.result_notification == "never"
    assert JobRun.model_validate_json(loaded.model_dump_json()) == loaded
    assert (await store.list(scope=SCOPE, job_name="brief")) == [finished]
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_run_store_persists_workflow_linkage(tmp_path: Path) -> None:
    store = JobRunStore(_settings(tmp_path))
    await store.initialize()
    run = JobRun(
        id="jobrun_workflow",
        job_name="personal/triage",
        spec_digest="b" * 64,
        provider="openrouter",
        model="test",
        profile_scope=SCOPE,
        session_id="session_workflow",
        started_at=datetime.now(UTC),
        workflow_name="personal/email-triage",
    )
    await store.insert(run, scope=SCOPE)

    linked = await store.update_workflow_link(
        run.id,
        scope=SCOPE,
        workflow_name="personal/email-triage",
        workflow_args={"account": "personal/personal", "limit": 3},
        workflow_run_id="workflow_run_1",
        workflow_status="running",
    )

    assert linked.workflow_name == "personal/email-triage"
    assert linked.workflow_args == {"account": "personal/personal", "limit": 3}
    assert linked.workflow_run_id == "workflow_run_1"
    assert linked.workflow_status == "running"
    assert JobRun.model_validate_json(linked.model_dump_json()) == linked


@pytest.mark.asyncio
async def test_job_store_v8_migration_preserves_history_in_default_lineage(
    tmp_path: Path,
) -> None:
    store = JobRunStore(_settings(tmp_path))
    store.root.mkdir(parents=True)
    with sqlite3.connect(store.path) as connection:
        connection.executescript(
            """
            CREATE TABLE job_runs (
                id TEXT PRIMARY KEY, job_name TEXT, spec_digest TEXT,
                provider TEXT NOT NULL, model TEXT NOT NULL,
                profile_scope_json TEXT NOT NULL, session_id TEXT NOT NULL,
                outcome TEXT, started_at TEXT NOT NULL, finished_at TEXT,
                iterations INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                final_message TEXT, error TEXT, transcript_path TEXT,
                dry_run INTEGER NOT NULL DEFAULT 0,
                effect_calls INTEGER NOT NULL DEFAULT 0,
                runtime_policy_digest TEXT, trigger TEXT NOT NULL DEFAULT 'manual',
                trigger_id TEXT, workflow_name TEXT, workflow_args_json TEXT,
                workflow_run_id TEXT, workflow_status TEXT
            );
            PRAGMA user_version = 8;
            """
        )
        connection.execute(
            """INSERT INTO job_runs (
                id, job_name, provider, model, profile_scope_json, session_id, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "jobrun_legacy",
                "personal/brief",
                "openrouter",
                "test-model",
                SCOPE.model_dump_json(),
                "session_legacy",
                datetime.now(UTC).isoformat(),
            ),
        )
    before = store.path.read_bytes()
    with pytest.raises(JobStoreError, match="unsupported.*schema"):
        await store.initialize()
    assert store.path.read_bytes() == before

    adapter = JobsUpgradeAdapter((store.path,))
    target = adapter.discover(user_data_dir=Path(store.settings.user_data_dir))[0]
    (step,) = adapter.plan_steps(source_data_generation=1, target_data_generation=1)
    adapter.apply(step)
    assert adapter.verify(target).state == "current"

    migrated = await store.get("jobrun_legacy", scope=SCOPE)

    assert migrated.context_lineage == 1
    assert migrated.context_revision == 1
    assert migrated.context_definition_digest is None
    assert migrated.result_notification == "always"
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_job_lock_uses_qualified_identity_and_distinct_descriptors(tmp_path: Path) -> None:
    first = job_lock(tmp_path, "personal/brief")
    second = job_lock(tmp_path, "personal/brief")
    other = job_lock(tmp_path, "work/brief")
    assert first.acquire()
    try:
        assert not second.acquire()
        assert other.acquire()
        other.release()
    finally:
        first.release()
    assert second.acquire()
    second.release()
    assert os.path.basename(first.path) == "personal--brief.lock"


def test_browser_worker_identity_is_a_process_liveness_proof(tmp_path: Path) -> None:
    lease = browser_worker_lease(tmp_path)
    assert browser_worker_is_alive(tmp_path, lease.identity) is True
    assert browser_worker_is_alive(tmp_path, "named-job:legacy") is None

    lease.release()

    assert browser_worker_is_alive(tmp_path, lease.identity) is False


def test_job_run_contract_requires_profile_scope() -> None:
    schema = JobRun.model_json_schema()
    assert "profile_scope" in schema["properties"]
    assert "profile_scope" in schema["required"]


@pytest.mark.asyncio
async def test_job_run_queries_enforce_profile_scope(tmp_path: Path) -> None:
    store = JobRunStore(_settings(tmp_path))
    await store.initialize()
    run = JobRun(
        id="jobrun_scoped",
        job_name="personal/brief",
        spec_digest="a" * 64,
        provider="openrouter",
        model="test",
        profile_scope=SCOPE,
        session_id="session_scoped",
        started_at=datetime.now(UTC),
    )
    await store.insert(run, scope=SCOPE)
    work = ProfileScope.create("work")
    cross_profile = ProfileScope.create("work", access_profiles=["personal"])

    with pytest.raises(JobStoreError, match="job run not found"):
        await store.get(run.id, scope=work)
    with pytest.raises(JobStoreError, match="job run not found"):
        await store.finish(
            run.model_copy(update={"outcome": "failed", "finished_at": datetime.now(UTC)}),
            scope=work,
        )
    assert await store.list(scope=work) == []
    assert await store.list(scope=cross_profile) == [run]


@pytest.mark.asyncio
async def test_concurrent_first_open_creates_one_usable_run_ledger(tmp_path: Path) -> None:
    """Concurrent first opens must not reject a ledger that is merely young.

    Connecting creates the database file before the schema script sets its user
    version, so a racing opener used to find the file at version 0 and fail with
    ``unsupported job run schema version: 0``.
    """

    settings = _settings(tmp_path)
    stores = [JobRunStore(settings) for _ in range(4)]

    await asyncio.gather(*(store.initialize() for store in stores))

    with sqlite3.connect(stores[0].path) as database:
        version = int(database.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            str(row[0])
            for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert version == SCHEMA_VERSION
    assert "job_runs" in tables
