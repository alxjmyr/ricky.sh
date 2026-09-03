"""Domain-neutral SQLite run ledger for ephemeral agent jobs."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar, cast
from uuid import uuid4

from pydantic import JsonValue, ValidationError

from ricky.config import RickySettings, user_data_path
from ricky.jobs.sources import Disposition, PersistedBatch
from ricky.jobs.types import ActionResolution, JobAction, JobContextEvidence, JobRun
from ricky.profiles import ProfileScope
from ricky.tools.base import EffectDisposition

_T = TypeVar("_T")

SCHEMA_VERSION = 12
"""Current job-ledger schema version (12 adds browser budget preauthorizations)."""

_INITIALIZATION_LOCK = threading.Lock()
_CREATE_RACE_ATTEMPTS = 25
_CREATE_RACE_DELAY_SECONDS = 0.02


class ActionIdentity(Protocol):
    action_key: str
    operation: str
    target: str
    occurrence: str
    summary: str


class JobStoreError(RuntimeError):
    """A bounded run-ledger failure safe to show at an interface."""


class JobActionConflictError(JobStoreError):
    """An action identity is already reserved, performed, or ambiguous."""


class JobEffectBudgetError(JobStoreError):
    """A run exhausted its atomically reserved external-effect calls."""


class GrantAuthorityError(JobStoreError):
    """A delegated effect exceeded, or fell outside, its grant's ledger ceiling."""


class JobRunStore:
    """Purpose-specific asynchronous operations over one global run database."""

    def __init__(self, settings: RickySettings) -> None:
        self.settings = settings
        self.root = user_data_path(settings) / settings.jobs.run_dir
        self.path = self.root / "runs.sqlite3"
        self._busy_timeout_ms = settings.jobs.sqlite_busy_timeout_ms

    async def initialize(self) -> None:
        """Create or migrate the job ledger to the current schema."""

        await self._call(self._initialize_sync)

    async def insert(self, run: JobRun, *, scope: ProfileScope) -> JobRun:
        """Insert one launch attempt exactly once."""

        _require_run_access(scope, run, run.id)
        await self._call(self._insert_sync, run)
        return run

    async def finish(self, run: JobRun, *, scope: ProfileScope) -> JobRun:
        """Finish one live run with all bounded audit fields."""

        if run.outcome is None or run.finished_at is None:
            raise JobStoreError("finished run requires outcome and finished_at")
        _require_run_access(scope, run, run.id)
        stored = await self.get(run.id, scope=scope)
        if stored.profile_scope != run.profile_scope:
            raise JobStoreError("job run profile scope is immutable")
        await self._call(self._finish_sync, run)
        return run

    async def get(self, run_id: str, *, scope: ProfileScope) -> JobRun:
        """Load one run by exact id."""

        result = await self._call(self._get_sync, run_id)
        if result is None or not scope.permits(result.profile_scope.label()):
            raise JobStoreError(f"job run not found: {run_id}")
        return result

    async def list(
        self,
        *,
        scope: ProfileScope,
        job_name: str | None = None,
        limit: int = 50,
    ) -> list[JobRun]:
        """List newest attempts, optionally filtered by job identity."""

        if not 1 <= limit <= 500:
            raise JobStoreError("history limit must be between 1 and 500")
        found = await self._call(self._list_sync, job_name, 500)
        return [run for run in found if scope.permits(run.profile_scope.label())][:limit]

    async def list_context_runs(
        self,
        *,
        scope: ProfileScope,
        job_name: str,
        dry_run: bool,
        context_lineage: int,
        limit: int = 50,
    ) -> list[JobRun]:
        """List one job's newest attempts in an exact live or dry context lane."""

        if not 1 <= limit <= 500:
            raise JobStoreError("history limit must be between 1 and 500")
        found = await self._call(
            self._list_context_runs_sync,
            job_name,
            dry_run,
            context_lineage,
            500,
        )
        return [run for run in found if scope.permits(run.profile_scope.label())][:limit]

    async def context_revision_evidence(
        self,
        *,
        scope: ProfileScope,
        job_name: str,
        dry_run: bool,
        context_lineage: int,
    ) -> list[JobContextEvidence]:
        """Return every accessible revision-to-meaning assignment for one context lane."""

        found = await self._call(
            self._context_revision_evidence_sync,
            job_name,
            dry_run,
            context_lineage,
        )
        return [
            JobContextEvidence(revision=revision, definition_digest=digest)
            for revision, digest, stored_scope in found
            if scope.permits(stored_scope.label())
        ]

    async def completed_transcripts(self, *, scope: ProfileScope) -> list[tuple[str, str]]:
        """Return completed transcript paths newest first for safe retention."""

        return await self._call(self._completed_transcripts_sync, scope)

    async def completed_transcript_records(
        self,
        *,
        scope: ProfileScope,
    ) -> list[tuple[str, str, str]]:
        """Return run, path, and session ids for retention cleanup."""

        return await self._call(self._completed_transcript_records_sync, scope)

    async def has_transcript_reference(
        self,
        session_id: str,
        *,
        scope: ProfileScope,
    ) -> bool:
        """Return whether a durable transcript still references this session."""

        return await self._call(self._has_transcript_reference_sync, session_id, scope)

    async def has_other_transcript_reference(
        self,
        session_id: str,
        run_id: str,
        *,
        scope: ProfileScope,
    ) -> bool:
        """Return whether another durable transcript references this session."""

        return await self._call(
            self._has_other_transcript_reference_sync,
            session_id,
            run_id,
            scope,
        )

    async def clear_transcript_path(
        self,
        run_id: str,
        path: str,
        *,
        scope: ProfileScope,
    ) -> None:
        """Clear a pruned path only when it still matches the selected record."""

        await self.get(run_id, scope=scope)
        await self._call(self._clear_transcript_path_sync, run_id, path)

    async def update_workflow_link(
        self,
        run_id: str,
        *,
        scope: ProfileScope,
        workflow_name: str,
        workflow_args: dict[str, JsonValue],
        workflow_run_id: str | None = None,
        workflow_status: str | None = None,
    ) -> JobRun:
        """Persist resolved workflow identity before or during execution."""

        await self.get(run_id, scope=scope)
        await self._call(
            self._update_workflow_link_sync,
            run_id,
            workflow_name,
            workflow_args,
            workflow_run_id,
            workflow_status,
        )
        return await self.get(run_id, scope=scope)

    async def completed_batch_payloads(self, *, scope: ProfileScope) -> list[tuple[str, str]]:
        """Return completed-run batch payload paths newest first."""

        return await self._call(self._completed_batch_payloads_sync, scope)

    async def clear_batch_payload_path(
        self,
        batch_id: str,
        path: str,
        *,
        scope: ProfileScope,
    ) -> None:
        """Mark a pruned batch payload absent while retaining ledger metadata."""

        await self._call(self._clear_batch_payload_path_sync, batch_id, path, scope)

    async def insert_batch(
        self,
        batch: PersistedBatch,
        item_ids: list[str],
        *,
        scope: ProfileScope,
    ) -> PersistedBatch:
        if len(item_ids) != len(set(item_ids)):
            raise JobStoreError("recurring batch item identities must be unique")
        run = await self.get(batch.run_id, scope=scope)
        if batch.profile_label != run.profile_scope.label():
            raise JobStoreError("job batch profile label differs from its run")
        await self._call(self._insert_batch_sync, batch, item_ids)
        return batch

    async def batches_for_run(
        self,
        run_id: str,
        *,
        scope: ProfileScope,
    ) -> list[PersistedBatch]:
        run = await self.get(run_id, scope=scope)
        return await self._call(self._batches_for_run_sync, run_id, run.profile_scope)

    async def record_disposition(
        self,
        disposition: Disposition,
        *,
        scope: ProfileScope,
    ) -> Disposition:
        batch_scope = await self._call(self._batch_scope_sync, disposition.batch_id)
        _require_scope_access(scope, batch_scope, "job batch", disposition.batch_id)
        if disposition.profile_label != batch_scope.label():
            raise JobStoreError("job disposition profile label differs from its batch")
        await self._call(self._record_disposition_sync, disposition)
        return disposition

    async def dispositions(
        self,
        batch_id: str,
        *,
        scope: ProfileScope,
    ) -> list[Disposition]:
        batch_scope = await self._call(self._batch_scope_sync, batch_id)
        _require_scope_access(scope, batch_scope, "job batch", batch_id)
        return await self._call(self._dispositions_sync, batch_id, batch_scope)

    async def cursor(
        self,
        job_name: str,
        source_name: str,
        *,
        scope: ProfileScope,
    ) -> JsonValue:
        return await self._call(self._cursor_sync, _scope_key(scope, job_name), source_name)

    async def commit_stream_cursors(self, run_id: str, *, scope: ProfileScope) -> None:
        run = await self.get(run_id, scope=scope)
        await self._call(self._commit_stream_cursors_sync, run_id, run.profile_scope)

    async def verify_run_accounting(self, run_id: str, *, scope: ProfileScope) -> None:
        await self.get(run_id, scope=scope)
        await self._call(self._verify_run_accounting_sync, run_id)

    async def consideration(
        self,
        job_name: str,
        task_id: str,
        revision: int,
        *,
        scope: ProfileScope,
    ) -> tuple[str, datetime] | None:
        return await self._call(
            self._consideration_sync,
            _scope_key(scope, job_name),
            task_id,
            revision,
        )

    async def record_consideration(
        self,
        *,
        job_name: str,
        task_id: str,
        revision: int,
        disposition: str,
        considered_at: datetime,
        scope: ProfileScope,
    ) -> None:
        await self._call(
            self._record_consideration_sync,
            _scope_key(scope, job_name),
            task_id,
            revision,
            disposition,
            considered_at,
        )

    async def reserve_action(
        self,
        *,
        job_name: str,
        run_id: str,
        identity: ActionIdentity,
        effect_budget: int,
        scope: ProfileScope,
    ) -> JobAction:
        run = await self.get(run_id, scope=scope)
        return await self._call(
            self._reserve_action_sync,
            job_name,
            run_id,
            identity,
            effect_budget,
            run.profile_scope,
        )

    async def seed_grant_budget(
        self,
        *,
        grant_id: str,
        task_id: str,
        effect_limit: int,
        financial_limit_minor: int | None,
        currency: str | None,
        expires_at: datetime,
        scope: ProfileScope,
    ) -> None:
        """Publish one delegation grant's ceiling into the single effect ledger."""

        await self._call(
            self._seed_grant_budget_sync,
            grant_id,
            task_id,
            effect_limit,
            financial_limit_minor,
            currency,
            expires_at,
            scope,
        )

    async def set_grant_budget_status(
        self,
        grant_id: str,
        status: str,
        *,
        scope: ProfileScope,
    ) -> None:
        """Mirror a grant lifecycle change so reservation fails closed at once."""

        if status not in {"active", "revoked", "expired", "consumed"}:
            raise JobStoreError(f"invalid grant budget status: {status}")
        await self._call(self._set_grant_budget_status_sync, grant_id, status, scope)

    async def get_grant_budget(
        self,
        grant_id: str,
        *,
        scope: ProfileScope,
    ) -> dict[str, Any] | None:
        return await self._call(self._get_grant_budget_sync, grant_id, scope)

    async def reserve_grant_action(
        self,
        *,
        grant_id: str,
        namespace: str,
        task_id: str,
        run_id: str,
        identity: ActionIdentity,
        effect_budget: int,
        amount_minor: int = 0,
        currency: str | None = None,
        now: datetime | None = None,
        scope: ProfileScope,
    ) -> JobAction:
        """Atomically reserve one delegated effect against every applicable limit.

        One transaction proves the grant is still active and unexpired, that the
        action identity has not already been reserved, performed, or left in
        doubt for this durable task, and that both the run's profile budget and
        the grant's effect and money limits still have room.
        """

        run = await self.get(run_id, scope=scope)
        return await self._call(
            self._reserve_grant_action_sync,
            grant_id,
            namespace,
            task_id,
            run_id,
            identity,
            effect_budget,
            amount_minor,
            currency,
            now or datetime.now(UTC),
            run.profile_scope,
        )

    async def resolve_action(
        self,
        action_id: str,
        disposition: EffectDisposition,
        *,
        scope: ProfileScope,
        provider_reference: str | None,
    ) -> JobAction:
        await self.get_action(action_id, scope=scope)
        return await self._call(
            self._resolve_action_sync, action_id, disposition, provider_reference
        )

    async def get_action(self, action_id: str, *, scope: ProfileScope) -> JobAction:
        result = await self._call(self._get_action_sync, action_id)
        if result is None or not scope.permits(result.profile_label):
            raise JobStoreError(f"job action not found: {action_id}")
        return result

    async def list_actions(
        self,
        *,
        scope: ProfileScope,
        job_name: str | None = None,
        limit: int = 50,
    ) -> list[JobAction]:
        if not 1 <= limit <= 500:
            raise JobStoreError("action limit must be between 1 and 500")
        found = await self._call(self._list_actions_sync, job_name, 500)
        return [action for action in found if scope.permits(action.profile_label)][:limit]

    async def actions_for_run(
        self,
        run_id: str,
        *,
        scope: ProfileScope,
    ) -> list[JobAction]:
        """Return every external-effect receipt record for one exact run."""

        await self.get(run_id, scope=scope)
        return await self._call(self._actions_for_run_sync, run_id)

    async def reconcile_action(
        self,
        action_id: str,
        disposition: EffectDisposition,
        *,
        scope: ProfileScope,
        actor: str = "ricky_job_cli",
    ) -> tuple[JobAction, ActionResolution]:
        if disposition == "in_doubt":
            raise JobStoreError("user reconciliation must choose performed or not_performed")
        await self.get_action(action_id, scope=scope)
        return await self._call(self._reconcile_action_sync, action_id, disposition, actor)

    async def reserved_actions(self, *, scope: ProfileScope) -> list[JobAction]:
        """List external effects that were reserved but never resolved."""

        found = await self._call(self._reserved_actions_sync)
        return [action for action in found if scope.permits(action.profile_label)]

    async def strand_reserved_action(
        self,
        action_id: str,
        *,
        scope: ProfileScope,
        error: str,
    ) -> JobAction:
        """Move one abandoned reservation to ``in_doubt`` for operator reconciliation.

        A reservation exists only because the dispatch call was about to happen,
        so an unresolved one after a restart is exactly the ambiguous case. Code
        never guesses ``performed`` or ``not_performed`` here.
        """

        if not error.strip():
            raise JobStoreError("recovery error cannot be empty")
        await self.get_action(action_id, scope=scope)
        return await self._call(self._strand_reserved_action_sync, action_id, error[:500])

    async def action_counts(self, *, scope: ProfileScope) -> dict[str, int]:
        """Count external effect actions by disposition."""

        return await self._call(self._action_counts_sync, scope)

    async def escalation_task(
        self,
        job_name: str,
        blocked_key: str,
        *,
        scope: ProfileScope,
    ) -> str | None:
        return await self._call(
            self._escalation_task_sync,
            _scope_key(scope, job_name),
            blocked_key,
        )

    async def correlate_escalation(
        self,
        job_name: str,
        blocked_key: str,
        task_id: str,
        run_id: str,
        *,
        scope: ProfileScope,
    ) -> str:
        await self.get(run_id, scope=scope)
        return await self._call(
            self._correlate_escalation_sync,
            _scope_key(scope, job_name),
            blocked_key,
            task_id,
            run_id,
        )

    async def _call(self, operation: Callable[..., _T], *args: Any) -> _T:
        try:
            return await asyncio.to_thread(operation, *args)
        except JobStoreError:
            raise
        except (OSError, sqlite3.Error, ValidationError) as exc:
            raise JobStoreError(f"job run store failed: {exc}") from exc

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(
            self.path,
            timeout=self._busy_timeout_ms / 1_000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize_sync(self) -> None:
        from ricky.jobs.upgrade import inspect_jobs_store

        # This ledger is published in two steps: connecting creates the file and
        # the schema script then sets its user version.  A concurrent opener can
        # therefore find an existing file whose schema is not published yet, so
        # serialize creation in-process and re-inspect a losing race within a
        # bounded window rather than rejecting a ledger that is merely young.
        with _INITIALIZATION_LOCK:
            if not self.path.exists():
                try:
                    self._migrate_or_create_sync()
                except (JobStoreError, sqlite3.Error):
                    # Another process published first.  The inspection below is
                    # the authority on whether the result is actually usable.
                    pass
                else:
                    self.path.chmod(0o600)
                    return
        for attempt in range(_CREATE_RACE_ATTEMPTS):
            try:
                inspect_jobs_store(self.path, allow_supported_old=False)
                break
            except (JobStoreError, sqlite3.Error):
                if attempt + 1 == _CREATE_RACE_ATTEMPTS:
                    raise
                time.sleep(_CREATE_RACE_DELAY_SECONDS)
        self.path.chmod(0o600)

    def _migrate_or_create_sync(self) -> None:
        """Owner-local implementation used only by create-current and the adapter."""

        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in set(range(SCHEMA_VERSION + 1)):
                raise JobStoreError(f"unsupported job run schema version: {version}")
            if version == 0:
                connection.executescript(
                    """
                    CREATE TABLE job_runs (
                        id TEXT PRIMARY KEY,
                        job_name TEXT,
                        spec_digest TEXT,
                        provider TEXT NOT NULL,
                        model TEXT NOT NULL,
                        profile_scope_json TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        outcome TEXT,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        iterations INTEGER NOT NULL DEFAULT 0,
                        prompt_tokens INTEGER NOT NULL DEFAULT 0,
                        completion_tokens INTEGER NOT NULL DEFAULT 0,
                        final_message TEXT,
                        error TEXT,
                        transcript_path TEXT
                    );
                    CREATE INDEX job_runs_started_idx
                        ON job_runs(started_at DESC, id DESC);
                    CREATE INDEX job_runs_name_started_idx
                        ON job_runs(job_name, started_at DESC, id DESC);
                    PRAGMA user_version = 1;
                    """
                )
                version = 1
            if version == 1:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    ALTER TABLE job_runs ADD COLUMN dry_run INTEGER NOT NULL DEFAULT 0;
                    ALTER TABLE job_runs ADD COLUMN effect_calls INTEGER NOT NULL DEFAULT 0;
                    ALTER TABLE job_runs ADD COLUMN runtime_policy_digest TEXT;
                    CREATE TABLE job_batches (
                        id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES job_runs(id),
                        job_name TEXT NOT NULL,
                        source_name TEXT NOT NULL,
                        kind TEXT NOT NULL CHECK(kind IN ('stream','task_pool')),
                        payload_path TEXT NOT NULL,
                        upper_bound TEXT,
                        input_cursor_json TEXT,
                        next_cursor_json TEXT,
                        complete INTEGER NOT NULL,
                        dry_run INTEGER NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX job_batches_run_idx ON job_batches(run_id, id);
                    CREATE TABLE job_batch_items (
                        batch_id TEXT NOT NULL REFERENCES job_batches(id) ON DELETE CASCADE,
                        item_id TEXT NOT NULL,
                        PRIMARY KEY (batch_id, item_id)
                    );
                    CREATE TABLE job_dispositions (
                        batch_id TEXT NOT NULL REFERENCES job_batches(id) ON DELETE CASCADE,
                        item_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        linked_id TEXT,
                        summary TEXT,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (batch_id, item_id),
                        FOREIGN KEY (batch_id, item_id)
                            REFERENCES job_batch_items(batch_id, item_id)
                    );
                    CREATE TABLE job_stream_cursors (
                        job_name TEXT NOT NULL,
                        source_name TEXT NOT NULL,
                        cursor_json TEXT,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (job_name, source_name)
                    );
                    CREATE TABLE job_task_considerations (
                        job_name TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        task_revision INTEGER NOT NULL,
                        disposition TEXT NOT NULL,
                        considered_at TEXT NOT NULL,
                        PRIMARY KEY (job_name, task_id, task_revision)
                    );
                    CREATE TABLE job_actions (
                        id TEXT PRIMARY KEY,
                        job_name TEXT NOT NULL,
                        run_id TEXT NOT NULL REFERENCES job_runs(id),
                        action_key TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        target TEXT NOT NULL,
                        occurrence TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN
                            ('reserved','performed','not_performed','in_doubt')),
                        provider_reference TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX job_actions_key_idx
                        ON job_actions(job_name, action_key, created_at DESC);
                    CREATE UNIQUE INDEX job_actions_active_key_idx
                        ON job_actions(job_name, action_key)
                        WHERE status IN ('reserved','performed','in_doubt');
                    CREATE TABLE job_action_resolutions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        action_id TEXT NOT NULL REFERENCES job_actions(id),
                        disposition TEXT NOT NULL CHECK(disposition IN
                            ('performed','not_performed')),
                        actor TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE job_escalations (
                        job_name TEXT NOT NULL,
                        blocked_key TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (job_name, blocked_key)
                    );
                    PRAGMA user_version = 2;
                    COMMIT;
                    """
                )
                version = 2
            if version == 2:
                columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(job_runs)")}
                connection.execute("BEGIN IMMEDIATE")
                if "trigger" not in columns:
                    connection.execute(
                        "ALTER TABLE job_runs ADD COLUMN trigger TEXT NOT NULL DEFAULT 'manual'"
                    )
                if "trigger_id" not in columns:
                    connection.execute("ALTER TABLE job_runs ADD COLUMN trigger_id TEXT")
                connection.commit()
                version = 2
            if version in (2, 3):
                # Delegated effects reuse this single effect ledger
                # instead of creating a second one. `grant_id` records which
                # delegated authority reserved the action, and `grant_budgets`
                # makes the grant's effect and money limits atomic with the
                # existing per-run budget and duplicate-identity guard.
                action_columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(job_actions)")
                }
                connection.execute("BEGIN IMMEDIATE")
                if action_columns:
                    if "grant_id" not in action_columns:
                        connection.execute("ALTER TABLE job_actions ADD COLUMN grant_id TEXT")
                    if "task_id" not in action_columns:
                        connection.execute("ALTER TABLE job_actions ADD COLUMN task_id TEXT")
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS grant_budgets (
                        grant_id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL,
                        effect_limit INTEGER NOT NULL,
                        effects_used INTEGER NOT NULL DEFAULT 0,
                        financial_limit_minor INTEGER,
                        financial_used_minor INTEGER NOT NULL DEFAULT 0,
                        currency TEXT,
                        status TEXT NOT NULL CHECK(status IN
                            ('active','revoked','expired','consumed')),
                        expires_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )"""
                )
                if action_columns:
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS job_actions_grant_idx"
                        " ON job_actions(grant_id, created_at DESC)"
                    )
                connection.execute("PRAGMA user_version = 4")
                connection.commit()
                version = 4
            if version == 4:
                # A lifecycle termination must survive even when it races ahead
                # of budget seeding. Seeding consults this tombstone in the same
                # transaction and can therefore never publish an active budget
                # after revocation/expiry/consumption became durable.
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE grant_budget_terminations (
                        grant_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL CHECK(status IN
                            ('revoked','expired','consumed')),
                        updated_at TEXT NOT NULL
                    );
                    PRAGMA user_version = 5;
                    COMMIT;
                    """
                )
                version = 5
            if version == 5:
                columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(job_runs)")}
                if "profile_scope_json" not in columns:
                    raise JobStoreError(
                        "job run schema requires the explicit first-class profiles migration"
                    )
                connection.execute("PRAGMA user_version = 6")
                connection.commit()
                version = 6
            if version == 6:
                connection.execute("BEGIN IMMEDIATE")
                budget_columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(grant_budgets)")
                }
                if "profile_scope_json" not in budget_columns:
                    connection.execute(
                        "ALTER TABLE grant_budgets ADD COLUMN profile_scope_json TEXT"
                    )
                termination_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(grant_budget_terminations)")
                }
                if "profile_scope_json" not in termination_columns:
                    connection.execute(
                        "ALTER TABLE grant_budget_terminations ADD COLUMN profile_scope_json TEXT"
                    )
                missing = connection.execute(
                    "SELECT COUNT(*) FROM grant_budgets WHERE profile_scope_json IS NULL"
                ).fetchone()[0]
                missing += connection.execute(
                    "SELECT COUNT(*) FROM grant_budget_terminations "
                    "WHERE profile_scope_json IS NULL"
                ).fetchone()[0]
                if missing:
                    connection.rollback()
                    raise JobStoreError(
                        "grant budget rows require the explicit first-class profiles migration"
                    )
                connection.execute("PRAGMA user_version = 7")
                connection.commit()
                version = 7
            if version == 7:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    ALTER TABLE job_runs ADD COLUMN workflow_name TEXT;
                    ALTER TABLE job_runs ADD COLUMN workflow_args_json TEXT;
                    ALTER TABLE job_runs ADD COLUMN workflow_run_id TEXT;
                    ALTER TABLE job_runs ADD COLUMN workflow_status TEXT;
                    CREATE INDEX job_runs_workflow_idx
                        ON job_runs(workflow_run_id) WHERE workflow_run_id IS NOT NULL;
                    PRAGMA user_version = 8;
                    COMMIT;
                    """
                )
                version = 8
            if version == 8:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    ALTER TABLE job_runs ADD COLUMN context_lineage INTEGER;
                    ALTER TABLE job_runs ADD COLUMN context_revision INTEGER;
                    ALTER TABLE job_runs ADD COLUMN context_definition_digest TEXT;
                    UPDATE job_runs
                    SET context_lineage = 1, context_revision = 1
                    WHERE job_name IS NOT NULL;
                    CREATE INDEX job_runs_context_idx ON job_runs(
                        job_name, dry_run, context_lineage, started_at DESC, id DESC
                    );
                    PRAGMA user_version = 9;
                    COMMIT;
                    """
                )
                version = 9
            if version == 9:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    ALTER TABLE job_runs ADD COLUMN result_notification TEXT NOT NULL
                        DEFAULT 'always'
                        CHECK(result_notification IN ('always','never'));
                    PRAGMA user_version = 10;
                    COMMIT;
                    """
                )
                version = 10
            if version == 10:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE browser_attempts (
                        id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES job_runs(id),
                        execution_request_id TEXT,
                        owner_token TEXT NOT NULL,
                        claim_fence INTEGER NOT NULL,
                        worker_id TEXT NOT NULL,
                        mode TEXT NOT NULL CHECK(mode IN ('read_only','transaction')),
                        resource_json TEXT,
                        resource_kind TEXT NOT NULL CHECK(resource_kind IN
                            ('ephemeral','persistent')),
                        resource_configuration_digest TEXT,
                        scope_digest TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN
                            ('starting','running','parked','completed','failed','cancelled',
                             'uncertain','in_doubt')),
                        cleanup TEXT NOT NULL CHECK(cleanup IN
                            ('pending','not_started','confirmed','failed')),
                        started_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        finished_at TEXT,
                        error TEXT
                    );
                    CREATE UNIQUE INDEX browser_attempts_active_run_idx
                    ON browser_attempts(run_id)
                    WHERE status IN ('starting','running','parked');
                    CREATE INDEX browser_attempts_request_idx
                    ON browser_attempts(execution_request_id, started_at DESC);
                    CREATE TABLE browser_attempt_budgets (
                        attempt_id TEXT NOT NULL REFERENCES browser_attempts(id),
                        operation TEXT NOT NULL,
                        used INTEGER NOT NULL DEFAULT 0,
                        ceiling INTEGER NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (attempt_id, operation)
                    );
                    CREATE TABLE browser_navigation_checkpoints (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        attempt_id TEXT NOT NULL REFERENCES browser_attempts(id),
                        page_generation INTEGER NOT NULL,
                        top_level_origin TEXT NOT NULL,
                        url_projection TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE browser_action_evidence (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        attempt_id TEXT NOT NULL REFERENCES browser_attempts(id),
                        action_id TEXT REFERENCES job_actions(id),
                        operation TEXT NOT NULL,
                        logical_effect_key TEXT,
                        live_occurrence_digest TEXT NOT NULL,
                        disposition TEXT NOT NULL CHECK(disposition IN
                            ('observed','reserved','performed','not_performed','in_doubt')),
                        attempt_reason TEXT,
                        binding_json TEXT,
                        postcondition TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE UNIQUE INDEX browser_action_evidence_action_idx
                    ON browser_action_evidence(action_id) WHERE action_id IS NOT NULL;
                    CREATE INDEX browser_action_evidence_attempt_idx
                    ON browser_action_evidence(attempt_id, id);
                    PRAGMA user_version = 11;
                    COMMIT;
                    """
                )
                version = 11
            if version == 11:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE browser_budget_reservations (
                        id TEXT PRIMARY KEY,
                        attempt_id TEXT NOT NULL REFERENCES browser_attempts(id),
                        operation TEXT NOT NULL CHECK(operation='created_pages'),
                        reservation_key TEXT NOT NULL,
                        reserved INTEGER NOT NULL,
                        consumed INTEGER NOT NULL DEFAULT 0,
                        state TEXT NOT NULL CHECK(state IN ('pending','settled','in_doubt')),
                        created_at TEXT NOT NULL,
                        settled_at TEXT,
                        UNIQUE(attempt_id, operation, reservation_key)
                    );
                    CREATE INDEX browser_budget_reservations_pending_idx
                    ON browser_budget_reservations(attempt_id, state);
                    PRAGMA user_version = 12;
                    COMMIT;
                    """
                )
                version = 12
        # The ledger records external effect targets and receipts as private evidence.
        self.path.chmod(0o600)

    def _insert_sync(self, run: JobRun) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO job_runs (
                    id, job_name, spec_digest, provider, model, profile_scope_json,
                    session_id, outcome,
                    started_at, finished_at, iterations, prompt_tokens,
                    completion_tokens, final_message, error, transcript_path,
                    dry_run, effect_calls, runtime_policy_digest, result_notification,
                    trigger, trigger_id,
                    context_lineage, context_revision, context_definition_digest,
                    workflow_name, workflow_args_json, workflow_run_id, workflow_status
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?
                )""",
                _run_values(run),
            )

    def _finish_sync(self, run: JobRun) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE job_runs SET
                    outcome = ?, finished_at = ?, iterations = ?, prompt_tokens = ?,
                    completion_tokens = ?, final_message = ?, error = ?, transcript_path = ?,
                    dry_run = ?, effect_calls = ?, runtime_policy_digest = ?,
                    trigger = ?, trigger_id = ?, workflow_name = ?,
                    context_lineage = ?, context_revision = ?, context_definition_digest = ?,
                    workflow_args_json = ?, workflow_run_id = ?, workflow_status = ?
                    WHERE id = ? AND outcome IS NULL""",
                (
                    run.outcome,
                    _iso(run.finished_at),
                    run.iterations,
                    run.prompt_tokens,
                    run.completion_tokens,
                    run.final_message,
                    run.error,
                    run.transcript_path,
                    int(run.dry_run),
                    run.effect_calls,
                    run.runtime_policy_digest,
                    run.trigger,
                    run.trigger_id,
                    run.workflow_name,
                    run.context_lineage,
                    run.context_revision,
                    run.context_definition_digest,
                    _optional_json(run.workflow_args),
                    run.workflow_run_id,
                    run.workflow_status,
                    run.id,
                ),
            )
            if cursor.rowcount != 1:
                raise JobStoreError(f"job run is missing or already finished: {run.id}")

    def _update_workflow_link_sync(
        self,
        run_id: str,
        workflow_name: str,
        workflow_args: dict[str, JsonValue],
        workflow_run_id: str | None,
        workflow_status: str | None,
    ) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE job_runs SET workflow_name = ?, workflow_args_json = ?,
                    workflow_run_id = COALESCE(?, workflow_run_id),
                    workflow_status = COALESCE(?, workflow_status)
                    WHERE id = ? AND outcome IS NULL""",
                (
                    workflow_name,
                    _json(workflow_args),
                    workflow_run_id,
                    workflow_status,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise JobStoreError(f"job run is missing or already finished: {run_id}")

    def _get_sync(self, run_id: str) -> JobRun | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,)).fetchone()
        return _row_to_run(row) if row is not None else None

    def _list_sync(self, job_name: str | None, limit: int) -> list[JobRun]:
        with self._connect() as connection:
            if job_name is None:
                rows = connection.execute(
                    "SELECT * FROM job_runs ORDER BY started_at DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM job_runs WHERE job_name = ?
                    ORDER BY started_at DESC, id DESC LIMIT ?""",
                    (job_name, limit),
                ).fetchall()
        return [_row_to_run(row) for row in rows]

    def _list_context_runs_sync(
        self,
        job_name: str,
        dry_run: bool,
        context_lineage: int,
        limit: int,
    ) -> list[JobRun]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM job_runs
                WHERE job_name = ? AND dry_run = ? AND context_lineage = ?
                ORDER BY started_at DESC, id DESC LIMIT ?""",
                (job_name, int(dry_run), context_lineage, limit),
            ).fetchall()
        return [_row_to_run(row) for row in rows]

    def _context_revision_evidence_sync(
        self,
        job_name: str,
        dry_run: bool,
        context_lineage: int,
    ) -> list[tuple[int, str | None, ProfileScope]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT DISTINCT context_revision, context_definition_digest,
                    profile_scope_json
                FROM job_runs
                WHERE job_name = ? AND dry_run = ? AND context_lineage = ?
                    AND context_revision IS NOT NULL AND transcript_path IS NOT NULL""",
                (job_name, int(dry_run), context_lineage),
            ).fetchall()
        return [
            (
                int(row["context_revision"]),
                row["context_definition_digest"],
                ProfileScope.model_validate_json(row["profile_scope_json"]),
            )
            for row in rows
        ]

    def _completed_transcripts_sync(self, scope: ProfileScope) -> list[tuple[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, transcript_path, profile_scope_json FROM job_runs
                WHERE outcome IS NOT NULL AND transcript_path IS NOT NULL
                ORDER BY finished_at DESC, id DESC"""
            ).fetchall()
        return [
            (str(row["id"]), str(row["transcript_path"]))
            for row in rows
            if _row_scope_permitted(scope, row)
        ]

    def _completed_transcript_records_sync(
        self,
        scope: ProfileScope,
    ) -> list[tuple[str, str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, transcript_path, session_id, profile_scope_json FROM job_runs
                WHERE outcome IS NOT NULL AND transcript_path IS NOT NULL
                ORDER BY finished_at DESC, id DESC"""
            ).fetchall()
        return [
            (str(row["id"]), str(row["transcript_path"]), str(row["session_id"]))
            for row in rows
            if _row_scope_permitted(scope, row)
        ]

    def _has_transcript_reference_sync(
        self,
        session_id: str,
        scope: ProfileScope,
    ) -> bool:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT profile_scope_json FROM job_runs
                WHERE session_id = ? AND transcript_path IS NOT NULL""",
                (session_id,),
            ).fetchall()
        return any(_row_scope_permitted(scope, row) for row in rows)

    def _has_other_transcript_reference_sync(
        self,
        session_id: str,
        run_id: str,
        scope: ProfileScope,
    ) -> bool:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT profile_scope_json FROM job_runs
                WHERE session_id = ? AND id != ? AND transcript_path IS NOT NULL""",
                (session_id, run_id),
            ).fetchall()
        return any(_row_scope_permitted(scope, row) for row in rows)

    def _clear_transcript_path_sync(self, run_id: str, path: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE job_runs SET transcript_path = NULL WHERE id = ? AND transcript_path = ?",
                (run_id, path),
            )

    def _completed_batch_payloads_sync(self, scope: ProfileScope) -> list[tuple[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT b.id, b.payload_path, r.profile_scope_json FROM job_batches b
                JOIN job_runs r ON r.id = b.run_id
                WHERE r.outcome IS NOT NULL AND b.payload_path != ''
                ORDER BY b.created_at DESC, b.id DESC"""
            ).fetchall()
        return [
            (str(row["id"]), str(row["payload_path"]))
            for row in rows
            if _row_scope_permitted(scope, row)
        ]

    def _clear_batch_payload_path_sync(
        self,
        batch_id: str,
        path: str,
        scope: ProfileScope,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            batch_scope = self._batch_scope(connection, batch_id)
            _require_scope_access(scope, batch_scope, "job batch", batch_id)
            connection.execute(
                "UPDATE job_batches SET payload_path = '' WHERE id = ? AND payload_path = ?",
                (batch_id, path),
            )

    def _insert_batch_sync(self, batch: PersistedBatch, item_ids: list[str]) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO job_batches (
                    id, run_id, job_name, source_name, kind, payload_path, upper_bound,
                    input_cursor_json, next_cursor_json, complete, dry_run, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    batch.id,
                    batch.run_id,
                    batch.job_name,
                    batch.source_name,
                    batch.kind,
                    batch.payload_path,
                    _iso(batch.upper_bound),
                    _json(batch.input_cursor),
                    _json(batch.next_cursor),
                    int(batch.complete),
                    int(batch.dry_run),
                    _iso(batch.created_at),
                ),
            )
            connection.executemany(
                "INSERT INTO job_batch_items (batch_id, item_id) VALUES (?, ?)",
                [(batch.id, item_id) for item_id in item_ids],
            )

    def _batches_for_run_sync(
        self,
        run_id: str,
        profile_scope: ProfileScope,
    ) -> list[PersistedBatch]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM job_batches WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [_row_to_batch(row, profile_scope) for row in rows]

    def _batch_scope_sync(self, batch_id: str) -> ProfileScope:
        with self._connect() as connection:
            return self._batch_scope(connection, batch_id)

    def _batch_scope(self, connection: sqlite3.Connection, batch_id: str) -> ProfileScope:
        row = connection.execute(
            """SELECT r.profile_scope_json FROM job_batches AS b
            JOIN job_runs AS r ON r.id = b.run_id WHERE b.id = ?""",
            (batch_id,),
        ).fetchone()
        if row is None:
            raise JobStoreError(f"job batch not found: {batch_id}")
        return ProfileScope.model_validate_json(row["profile_scope_json"])

    def _record_disposition_sync(self, disposition: Disposition) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO job_dispositions (
                        batch_id, item_id, kind, linked_id, summary, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        disposition.batch_id,
                        disposition.item_id,
                        disposition.kind,
                        disposition.linked_id,
                        disposition.summary,
                        _iso(disposition.created_at),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise JobStoreError(
                    "unknown batch identity or duplicate candidate disposition"
                ) from exc

    def _dispositions_sync(
        self,
        batch_id: str,
        profile_scope: ProfileScope,
    ) -> list[Disposition]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM job_dispositions WHERE batch_id = ? ORDER BY item_id",
                (batch_id,),
            ).fetchall()
        label = profile_scope.label()
        return [Disposition.model_validate({**dict(row), "profile_label": label}) for row in rows]

    def _cursor_sync(self, job_name: str, source_name: str) -> JsonValue:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cursor_json FROM job_stream_cursors WHERE job_name = ? AND source_name = ?",
                (job_name, source_name),
            ).fetchone()
        return _load_json(row[0]) if row is not None else None

    def _commit_stream_cursors_sync(self, run_id: str, scope: ProfileScope) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT outcome, dry_run FROM job_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None or run["outcome"] != "succeeded" or bool(run["dry_run"]):
                raise JobStoreError("stream cursors require a successful non-dry run")
            batches = connection.execute(
                "SELECT * FROM job_batches WHERE run_id = ? AND kind = 'stream'", (run_id,)
            ).fetchall()
            now = datetime.now(UTC).isoformat()
            for batch in batches:
                if not bool(batch["complete"]):
                    raise JobStoreError("incomplete stream batch cannot commit")
                missing = connection.execute(
                    """SELECT COUNT(*) FROM job_batch_items i
                    LEFT JOIN job_dispositions d
                      ON d.batch_id = i.batch_id AND d.item_id = i.item_id
                    WHERE i.batch_id = ? AND d.item_id IS NULL""",
                    (batch["id"],),
                ).fetchone()[0]
                if missing:
                    raise JobStoreError("unaccounted stream item prevents cursor commit")
                connection.execute(
                    """INSERT INTO job_stream_cursors (
                        job_name, source_name, cursor_json, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(job_name, source_name) DO UPDATE SET
                        cursor_json = excluded.cursor_json, updated_at = excluded.updated_at""",
                    (
                        _scope_key(scope, batch["job_name"]),
                        batch["source_name"],
                        batch["next_cursor_json"],
                        now,
                    ),
                )

    def _verify_run_accounting_sync(self, run_id: str) -> None:
        with self._connect() as connection:
            batches = connection.execute(
                "SELECT id, kind, complete FROM job_batches WHERE run_id = ?", (run_id,)
            ).fetchall()
            for batch in batches:
                if batch["kind"] == "stream" and not bool(batch["complete"]):
                    raise JobStoreError(
                        f"stream batch {batch['id']} is incomplete and cannot finish"
                    )
                missing = connection.execute(
                    """SELECT COUNT(*) FROM job_batch_items i
                    LEFT JOIN job_dispositions d
                      ON d.batch_id = i.batch_id AND d.item_id = i.item_id
                    WHERE i.batch_id = ? AND d.item_id IS NULL""",
                    (batch["id"],),
                ).fetchone()[0]
                if missing:
                    raise JobStoreError(
                        f"batch {batch['id']} has {missing} unaccounted candidate(s)"
                    )
                unsafe_effects = connection.execute(
                    """SELECT COUNT(*) FROM job_dispositions d
                    LEFT JOIN job_actions a ON a.id = d.linked_id
                    WHERE d.batch_id = ? AND d.kind = 'effect_reserved'
                      AND (a.id IS NULL OR a.status <> 'performed')""",
                    (batch["id"],),
                ).fetchone()[0]
                if unsafe_effects:
                    raise JobStoreError(f"batch {batch['id']} has unresolved linked effect(s)")

    def _consideration_sync(
        self, job_name: str, task_id: str, revision: int
    ) -> tuple[str, datetime] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT disposition, considered_at FROM job_task_considerations
                WHERE job_name = ? AND task_id = ? AND task_revision = ?""",
                (job_name, task_id, revision),
            ).fetchone()
        return (str(row[0]), datetime.fromisoformat(row[1])) if row is not None else None

    def _record_consideration_sync(
        self,
        job_name: str,
        task_id: str,
        revision: int,
        disposition: str,
        considered_at: datetime,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO job_task_considerations (
                    job_name, task_id, task_revision, disposition, considered_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_name, task_id, task_revision) DO UPDATE SET
                    disposition = excluded.disposition,
                    considered_at = excluded.considered_at""",
                (job_name, task_id, revision, disposition, _iso(considered_at)),
            )

    def _reserve_action_sync(
        self,
        job_name: str,
        run_id: str,
        identity: ActionIdentity,
        effect_budget: int,
        profile_scope: ProfileScope,
    ) -> JobAction:
        now = datetime.now(UTC)
        action = JobAction(
            id=f"action_{uuid4().hex}",
            job_name=job_name,
            run_id=run_id,
            profile_label=profile_scope.label(),
            action_key=str(identity.action_key),
            operation=str(identity.operation),
            target=str(identity.target),
            occurrence=str(identity.occurrence),
            summary=str(identity.summary),
            status="reserved",
            created_at=now,
            updated_at=now,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT status FROM job_actions WHERE job_name = ? AND action_key = ?
                ORDER BY created_at DESC, id DESC LIMIT 1""",
                (job_name, action.action_key),
            ).fetchone()
            if existing is not None and existing[0] in {"reserved", "performed", "in_doubt"}:
                raise JobActionConflictError(
                    f"job action is already {existing[0]} for this occurrence"
                )
            cursor = connection.execute(
                """UPDATE job_runs SET effect_calls = effect_calls + 1
                WHERE id = ? AND outcome IS NULL AND effect_calls < ?""",
                (run_id, effect_budget),
            )
            if cursor.rowcount != 1:
                raise JobEffectBudgetError("job effect-call budget exhausted")
            connection.execute(
                """INSERT INTO job_actions (
                    id, job_name, run_id, action_key, operation, target, occurrence,
                    summary, status, provider_reference, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
                (
                    action.id,
                    action.job_name,
                    action.run_id,
                    action.action_key,
                    action.operation,
                    action.target,
                    action.occurrence,
                    action.summary,
                    action.status,
                    _iso(action.created_at),
                    _iso(action.updated_at),
                ),
            )
        return action

    def _seed_grant_budget_sync(
        self,
        grant_id: str,
        task_id: str,
        effect_limit: int,
        financial_limit_minor: int | None,
        currency: str | None,
        expires_at: datetime,
        profile_scope: ProfileScope,
    ) -> None:
        now = datetime.now(UTC)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            termination = connection.execute(
                "SELECT status, profile_scope_json FROM grant_budget_terminations "
                "WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
            existing = connection.execute(
                "SELECT effect_limit, financial_limit_minor, currency, status, "
                "profile_scope_json FROM grant_budgets WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
            if existing is not None:
                _require_stored_scope(existing["profile_scope_json"], profile_scope, "grant budget")
                if (
                    int(existing["effect_limit"]) != effect_limit
                    or existing["financial_limit_minor"] != financial_limit_minor
                    or existing["currency"] != currency
                ):
                    raise JobStoreError("a grant budget can never be widened after issue")
                if termination is not None and existing["status"] == "active":
                    connection.execute(
                        "UPDATE grant_budgets SET status = ?, updated_at = ? WHERE grant_id = ?",
                        (str(termination["status"]), _iso(now), grant_id),
                    )
                return
            status = str(termination["status"]) if termination is not None else "active"
            if termination is not None:
                _require_stored_scope(
                    termination["profile_scope_json"],
                    profile_scope,
                    "grant budget termination",
                )
            connection.execute(
                """INSERT INTO grant_budgets (
                    grant_id, task_id, effect_limit, effects_used, financial_limit_minor,
                    financial_used_minor, currency, status, expires_at, updated_at,
                    profile_scope_json
                ) VALUES (?, ?, ?, 0, ?, 0, ?, ?, ?, ?, ?)""",
                (
                    grant_id,
                    task_id,
                    effect_limit,
                    financial_limit_minor,
                    currency,
                    status,
                    _iso(expires_at),
                    _iso(now),
                    profile_scope.model_dump_json(),
                ),
            )

    def _set_grant_budget_status_sync(
        self,
        grant_id: str,
        status: str,
        profile_scope: ProfileScope,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = _iso(datetime.now(UTC))
            budget = connection.execute(
                "SELECT profile_scope_json FROM grant_budgets WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
            termination = connection.execute(
                "SELECT profile_scope_json FROM grant_budget_terminations WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
            stored_scope = (
                ProfileScope.model_validate_json(budget["profile_scope_json"])
                if budget is not None
                else (
                    ProfileScope.model_validate_json(termination["profile_scope_json"])
                    if termination is not None
                    else profile_scope
                )
            )
            _require_scope_access(profile_scope, stored_scope, "grant budget", grant_id)
            if status != "active":
                connection.execute(
                    """INSERT INTO grant_budget_terminations (
                        grant_id, status, updated_at, profile_scope_json
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(grant_id) DO NOTHING""",
                    (grant_id, status, now, stored_scope.model_dump_json()),
                )
                terminal = connection.execute(
                    "SELECT status, profile_scope_json FROM grant_budget_terminations "
                    "WHERE grant_id = ?",
                    (grant_id,),
                ).fetchone()
                assert terminal is not None
                _require_stored_scope(
                    terminal["profile_scope_json"],
                    stored_scope,
                    "grant budget termination",
                )
                status = str(terminal["status"])
            elif (
                connection.execute(
                    "SELECT 1 FROM grant_budget_terminations WHERE grant_id = ?", (grant_id,)
                ).fetchone()
                is not None
            ):
                raise JobStoreError("a terminated grant budget cannot be reactivated")
            connection.execute(
                """UPDATE grant_budgets SET status = ?, updated_at = ?
                WHERE grant_id = ? AND profile_scope_json = ?""",
                (status, now, grant_id, stored_scope.model_dump_json()),
            )

    def _get_grant_budget_sync(
        self,
        grant_id: str,
        scope: ProfileScope,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM grant_budgets WHERE grant_id = ?", (grant_id,)
            ).fetchone()
        if row is None:
            return None
        stored_scope = ProfileScope.model_validate_json(row["profile_scope_json"])
        if not scope.permits(stored_scope.label()):
            return None
        return dict(row)

    def _reserve_grant_action_sync(
        self,
        grant_id: str,
        namespace: str,
        task_id: str,
        run_id: str,
        identity: ActionIdentity,
        effect_budget: int,
        amount_minor: int,
        currency: str | None,
        now: datetime,
        profile_scope: ProfileScope,
    ) -> JobAction:
        action = JobAction(
            id=f"action_{uuid4().hex}",
            job_name=namespace,
            run_id=run_id,
            profile_label=profile_scope.label(),
            action_key=str(identity.action_key),
            operation=str(identity.operation),
            target=str(identity.target),
            occurrence=str(identity.occurrence),
            summary=str(identity.summary),
            status="reserved",
            created_at=now,
            updated_at=now,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            budget = connection.execute(
                "SELECT * FROM grant_budgets WHERE grant_id = ?", (grant_id,)
            ).fetchone()
            if budget is None:
                raise GrantAuthorityError(f"no delegated budget for grant: {grant_id}")
            _require_stored_scope(budget["profile_scope_json"], profile_scope, "grant budget")
            if str(budget["status"]) != "active":
                raise GrantAuthorityError(f"delegation grant is {budget['status']}")
            if datetime.fromisoformat(str(budget["expires_at"])) <= now:
                connection.execute(
                    "UPDATE grant_budgets SET status = 'expired', updated_at = ?"
                    " WHERE grant_id = ?",
                    (_iso(now), grant_id),
                )
                connection.commit()
                raise GrantAuthorityError("delegation grant is expired")
            if str(budget["task_id"]) != task_id:
                raise GrantAuthorityError("delegated action targets a different durable task")
            existing = connection.execute(
                """SELECT status FROM job_actions WHERE job_name = ? AND action_key = ?
                ORDER BY created_at DESC, id DESC LIMIT 1""",
                (namespace, action.action_key),
            ).fetchone()
            if existing is not None and existing[0] in {"reserved", "performed", "in_doubt"}:
                raise JobActionConflictError(
                    f"job action is already {existing[0]} for this occurrence"
                )
            if amount_minor > 0:
                if budget["financial_limit_minor"] is None or budget["currency"] != currency:
                    raise GrantAuthorityError(
                        "delegated effect declares money outside the grant's currency limit"
                    )
                spent = connection.execute(
                    """UPDATE grant_budgets
                    SET financial_used_minor = financial_used_minor + ?, updated_at = ?
                    WHERE grant_id = ?
                      AND financial_used_minor + ? <= financial_limit_minor""",
                    (amount_minor, _iso(now), grant_id, amount_minor),
                )
                if spent.rowcount != 1:
                    raise GrantAuthorityError("delegated financial limit exhausted")
            reserved = connection.execute(
                """UPDATE grant_budgets SET effects_used = effects_used + 1, updated_at = ?
                WHERE grant_id = ? AND effects_used < effect_limit""",
                (_iso(now), grant_id),
            )
            if reserved.rowcount != 1:
                raise GrantAuthorityError("delegated effect-call limit exhausted")
            cursor = connection.execute(
                """UPDATE job_runs SET effect_calls = effect_calls + 1
                WHERE id = ? AND outcome IS NULL AND effect_calls < ?""",
                (run_id, effect_budget),
            )
            if cursor.rowcount != 1:
                raise JobEffectBudgetError("job effect-call budget exhausted")
            connection.execute(
                """INSERT INTO job_actions (
                    id, job_name, run_id, action_key, operation, target, occurrence,
                    summary, status, provider_reference, created_at, updated_at,
                    grant_id, task_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)""",
                (
                    action.id,
                    action.job_name,
                    action.run_id,
                    action.action_key,
                    action.operation,
                    action.target,
                    action.occurrence,
                    action.summary,
                    action.status,
                    _iso(action.created_at),
                    _iso(action.updated_at),
                    grant_id,
                    task_id,
                ),
            )
        return action

    def _resolve_action_sync(
        self, action_id: str, disposition: EffectDisposition, provider_reference: str | None
    ) -> JobAction:
        now = datetime.now(UTC)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE job_actions SET status = ?, provider_reference = ?, updated_at = ?
                WHERE id = ? AND status = 'reserved'""",
                (disposition, provider_reference, _iso(now), action_id),
            )
            if cursor.rowcount != 1:
                raise JobStoreError(f"job action is missing or already resolved: {action_id}")
            row = connection.execute(
                """SELECT a.*, r.profile_scope_json FROM job_actions AS a
                JOIN job_runs AS r ON r.id = a.run_id WHERE a.id = ?""",
                (action_id,),
            ).fetchone()
        assert row is not None
        return _row_to_action(row)

    def _get_action_sync(self, action_id: str) -> JobAction | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT a.*, r.profile_scope_json FROM job_actions AS a
                JOIN job_runs AS r ON r.id = a.run_id WHERE a.id = ?""",
                (action_id,),
            ).fetchone()
        return _row_to_action(row) if row is not None else None

    def _list_actions_sync(self, job_name: str | None, limit: int) -> list[JobAction]:
        with self._connect() as connection:
            if job_name is None:
                rows = connection.execute(
                    """SELECT a.*, r.profile_scope_json FROM job_actions AS a
                    JOIN job_runs AS r ON r.id = a.run_id
                    ORDER BY a.created_at DESC, a.id DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT a.*, r.profile_scope_json FROM job_actions AS a
                    JOIN job_runs AS r ON r.id = a.run_id WHERE a.job_name = ?
                    ORDER BY a.created_at DESC, a.id DESC LIMIT ?""",
                    (job_name, limit),
                ).fetchall()
        return [_row_to_action(row) for row in rows]

    def _actions_for_run_sync(self, run_id: str) -> list[JobAction]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT a.*, r.profile_scope_json FROM job_actions AS a
                JOIN job_runs AS r ON r.id = a.run_id
                WHERE a.run_id = ? ORDER BY a.created_at, a.id""",
                (run_id,),
            ).fetchall()
        return [_row_to_action(row) for row in rows]

    def _reserved_actions_sync(self) -> list[JobAction]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT a.*, r.profile_scope_json FROM job_actions AS a
                JOIN job_runs AS r ON r.id = a.run_id
                WHERE a.status = 'reserved' ORDER BY a.created_at, a.id"""
            ).fetchall()
        return [_row_to_action(row) for row in rows]

    def _strand_reserved_action_sync(self, action_id: str, error: str) -> JobAction:
        now = datetime.now(UTC)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE job_actions SET status = 'in_doubt', provider_reference = ?,
                updated_at = ? WHERE id = ? AND status = 'reserved'""",
                (error, _iso(now), action_id),
            )
            if cursor.rowcount != 1:
                raise JobStoreError(f"job action is not reserved: {action_id}")
            row = connection.execute(
                """SELECT a.*, r.profile_scope_json FROM job_actions AS a
                JOIN job_runs AS r ON r.id = a.run_id WHERE a.id = ?""",
                (action_id,),
            ).fetchone()
        assert row is not None
        return _row_to_action(row)

    def _action_counts_sync(self, scope: ProfileScope) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT a.status, r.profile_scope_json FROM job_actions AS a
                JOIN job_runs AS r ON r.id = a.run_id"""
            ).fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            if _row_scope_permitted(scope, row):
                status = str(row["status"])
                counts[status] = counts.get(status, 0) + 1
        return counts

    def _reconcile_action_sync(
        self, action_id: str, disposition: EffectDisposition, actor: str
    ) -> tuple[JobAction, ActionResolution]:
        now = datetime.now(UTC)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE job_actions SET status = ?, updated_at = ?
                WHERE id = ? AND status = 'in_doubt'""",
                (disposition, _iso(now), action_id),
            )
            if cursor.rowcount != 1:
                raise JobStoreError("only an in_doubt action may be reconciled")
            resolution_cursor = connection.execute(
                """INSERT INTO job_action_resolutions (
                    action_id, disposition, actor, created_at
                ) VALUES (?, ?, ?, ?)""",
                (action_id, disposition, actor, _iso(now)),
            )
            action_row = connection.execute(
                """SELECT a.*, r.profile_scope_json FROM job_actions AS a
                JOIN job_runs AS r ON r.id = a.run_id WHERE a.id = ?""",
                (action_id,),
            ).fetchone()
        assert action_row is not None
        resolution_id = resolution_cursor.lastrowid
        if resolution_id is None:
            raise JobStoreError("action resolution did not receive an audit id")
        resolution = ActionResolution(
            id=int(resolution_id),
            action_id=action_id,
            profile_label=_row_to_action(action_row).profile_label,
            disposition=disposition,
            actor=actor,
            created_at=now,
        )
        return _row_to_action(action_row), resolution

    def _escalation_task_sync(self, job_name: str, blocked_key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT task_id FROM job_escalations WHERE job_name = ? AND blocked_key = ?",
                (job_name, blocked_key),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def _correlate_escalation_sync(
        self, job_name: str, blocked_key: str, task_id: str, run_id: str
    ) -> str:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO job_escalations (
                    job_name, blocked_key, task_id, run_id, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_name, blocked_key) DO UPDATE SET
                    run_id = excluded.run_id, updated_at = excluded.updated_at""",
                (job_name, blocked_key, task_id, run_id, _iso(datetime.now(UTC))),
            )
            row = connection.execute(
                "SELECT task_id FROM job_escalations WHERE job_name = ? AND blocked_key = ?",
                (job_name, blocked_key),
            ).fetchone()
        assert row is not None
        return str(row[0])


def _run_values(run: JobRun) -> tuple[object, ...]:
    return (
        run.id,
        run.job_name,
        run.spec_digest,
        run.provider,
        run.model,
        run.profile_scope.model_dump_json(),
        run.session_id,
        run.outcome,
        _iso(run.started_at),
        _iso(run.finished_at),
        run.iterations,
        run.prompt_tokens,
        run.completion_tokens,
        run.final_message,
        run.error,
        run.transcript_path,
        int(run.dry_run),
        run.effect_calls,
        run.runtime_policy_digest,
        run.result_notification,
        run.trigger,
        run.trigger_id,
        run.context_lineage,
        run.context_revision,
        run.context_definition_digest,
        run.workflow_name,
        _optional_json(run.workflow_args),
        run.workflow_run_id,
        run.workflow_status,
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _row_to_run(row: sqlite3.Row) -> JobRun:
    values = dict(row)
    values["dry_run"] = bool(values.get("dry_run", 0))
    values["profile_scope"] = json.loads(values.pop("profile_scope_json"))
    values["workflow_args"] = _load_json(values.pop("workflow_args_json", None))
    return JobRun.model_validate(values)


def _row_to_batch(row: sqlite3.Row, profile_scope: ProfileScope) -> PersistedBatch:
    return PersistedBatch(
        id=row["id"],
        run_id=row["run_id"],
        profile_label=profile_scope.label(),
        job_name=row["job_name"],
        source_name=row["source_name"],
        kind=row["kind"],
        payload_path=row["payload_path"],
        upper_bound=datetime.fromisoformat(row["upper_bound"]) if row["upper_bound"] else None,
        input_cursor=_load_json(row["input_cursor_json"]),
        next_cursor=_load_json(row["next_cursor_json"]),
        complete=bool(row["complete"]),
        dry_run=bool(row["dry_run"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_action(row: sqlite3.Row) -> JobAction:
    values = dict(row)
    profile_scope = ProfileScope.model_validate_json(values.pop("profile_scope_json"))
    values["profile_label"] = profile_scope.label()
    return JobAction.model_validate(values)


def _row_scope_permitted(scope: ProfileScope, row: sqlite3.Row) -> bool:
    stored = ProfileScope.model_validate_json(row["profile_scope_json"])
    return scope.permits(stored.label())


def _require_scope_access(
    caller: ProfileScope,
    stored: ProfileScope,
    kind: str,
    identifier: str,
) -> None:
    if not caller.permits(stored.label()):
        raise JobStoreError(f"{kind} not found: {identifier}")


def _require_run_access(scope: ProfileScope, run: JobRun, run_id: str) -> None:
    _require_scope_access(scope, run.profile_scope, "job run", run_id)


def _require_stored_scope(payload: str | None, expected: ProfileScope, kind: str) -> None:
    if payload is None or ProfileScope.model_validate_json(payload) != expected:
        raise JobStoreError(f"{kind} profile scope mismatch")


def _scope_key(scope: ProfileScope, name: str) -> str:
    return f"{scope.digest()}:{name}"


def _json(value: object) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _optional_json(value: object | None) -> str | None:
    return _json(value) if value is not None else None


def _load_json(value: str | None) -> JsonValue:
    return cast(JsonValue, json.loads(value)) if value is not None else None
