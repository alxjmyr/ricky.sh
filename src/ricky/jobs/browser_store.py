"""Browser-attempt evidence stored inside the existing job-run ledger."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from ricky.config import RickySettings, user_data_path
from ricky.executions.browser import (
    BrowserActionEvidence,
    BrowserAttempt,
    BrowserAttemptLease,
    BrowserAttemptStatus,
    BrowserBudgetOperation,
    BrowserBudgetReservation,
    BrowserBudgetUsage,
    BrowserCleanupDisposition,
    BrowserExecutionScope,
    BrowserNavigationCheckpoint,
    BrowserResourceKind,
)
from ricky.jobs.store import JobRunStore, JobStoreError
from ricky.profiles import ProfileResourceRef, ProfileScope

_TERMINAL = {"completed", "failed", "cancelled", "uncertain", "in_doubt"}
_LIVE_BUDGETS = {"controlled_pages", "parked_browsers"}


class BrowserBudgetExceededError(JobStoreError):
    """A browser operation would exceed its immutable durable ceiling."""


class BrowserAttemptFenceError(JobStoreError):
    """A stale browser owner attempted to mutate attempt evidence."""


class BrowserRunLedger:
    """Fenced browser lifecycle, budgets, and safe evidence beside one job run."""

    def __init__(self, settings: RickySettings) -> None:
        self.settings = settings
        self.root = user_data_path(settings) / settings.jobs.run_dir
        self.path = self.root / "runs.sqlite3"
        self._busy_timeout_ms = settings.jobs.sqlite_busy_timeout_ms
        self.runs = JobRunStore(settings)

    async def initialize(self) -> None:
        await self.runs.initialize()

    async def start_attempt(
        self,
        *,
        run_id: str,
        scope: ProfileScope,
        browser_scope: BrowserExecutionScope,
        claim_fence: int,
        worker_id: str,
        execution_request_id: str | None,
        resource: ProfileResourceRef | None,
        resource_kind: BrowserResourceKind,
        resource_configuration_digest: str | None,
        now: datetime | None = None,
    ) -> BrowserAttemptLease:
        run = await self.runs.get(run_id, scope=scope)
        if browser_scope.mode == "transaction" and (
            run.trigger != "execution" or execution_request_id is None
        ):
            raise JobStoreError("transaction browser attempts are limited to ad hoc execution runs")
        if execution_request_id is not None and run.trigger_id != execution_request_id:
            raise JobStoreError("browser attempt execution identity differs from its job run")
        if claim_fence < 1:
            raise ValueError("browser attempt claim fence must be positive")
        if not worker_id.strip() or len(worker_id) > 200:
            raise ValueError("browser attempt worker id must be 1-200 characters")
        if resource_kind == "persistent":
            if resource is None or resource_configuration_digest is None:
                raise JobStoreError("persistent browser attempt requires an exact resource pin")
            pin = next(
                (item for item in browser_scope.resources if item.resource == resource),
                None,
            )
            if pin is None or pin.configuration_digest != resource_configuration_digest:
                raise JobStoreError("browser resource is outside its immutable execution scope")
        elif resource is not None or resource_configuration_digest is not None:
            raise JobStoreError("ephemeral browser attempts cannot claim a configured resource")
        elif not browser_scope.allow_ephemeral:
            raise JobStoreError("ephemeral browser use is outside its immutable execution scope")
        return await self._call(
            self._start_attempt,
            run_id,
            run.profile_scope,
            browser_scope,
            claim_fence,
            worker_id,
            execution_request_id,
            resource,
            resource_kind,
            resource_configuration_digest,
            now or datetime.now(UTC),
        )

    async def get_attempt(self, attempt_id: str, *, scope: ProfileScope) -> BrowserAttempt:
        found = await self._call(self._get_attempt, attempt_id)
        if found is None or not scope.permits(found.profile_label):
            raise JobStoreError(f"browser attempt not found: {attempt_id}")
        return found

    async def attempts_for_run(self, run_id: str, *, scope: ProfileScope) -> list[BrowserAttempt]:
        await self.runs.get(run_id, scope=scope)
        return await self._call(self._attempts_for_run, run_id)

    async def active_attempts(self, *, scope: ProfileScope) -> list[BrowserAttempt]:
        """List live attempts visible to one recovery scope."""

        return await self._call(self._active_attempts, scope)

    async def has_ambiguous_effect_evidence(
        self,
        attempt_id: str,
        *,
        scope: ProfileScope,
    ) -> bool:
        """Return whether losing this owner requires an in-doubt disposition."""

        await self.get_attempt(attempt_id, scope=scope)
        return await self._call(self._has_ambiguous_effect_evidence, attempt_id)

    async def recover_lost_attempt(
        self,
        attempt_id: str,
        *,
        scope: ProfileScope,
        reason: str,
        now: datetime | None = None,
    ) -> BrowserAttempt:
        """Terminalize one disowned attempt without accepting its stale owner token."""

        await self.get_attempt(attempt_id, scope=scope)
        return await self._call(
            self._recover_lost_attempt,
            attempt_id,
            reason[:2_000],
            now or datetime.now(UTC),
        )

    async def transition(
        self,
        attempt_id: str,
        *,
        scope: ProfileScope,
        owner_token: str,
        claim_fence: int,
        status: BrowserAttemptStatus,
        cleanup: BrowserCleanupDisposition | None = None,
        error: str | None = None,
        now: datetime | None = None,
    ) -> BrowserAttempt:
        await self.get_attempt(attempt_id, scope=scope)
        return await self._call(
            self._transition,
            attempt_id,
            owner_token,
            claim_fence,
            status,
            cleanup,
            error[:2_000] if error else None,
            now or datetime.now(UTC),
        )

    async def reserve_budget(
        self,
        attempt_id: str,
        operation: BrowserBudgetOperation,
        *,
        amount: int = 1,
        scope: ProfileScope,
        owner_token: str,
        claim_fence: int,
        now: datetime | None = None,
    ) -> BrowserBudgetUsage:
        if amount < 1:
            raise ValueError("browser budget reservation amount must be positive")
        await self.get_attempt(attempt_id, scope=scope)
        return await self._call(
            self._reserve_budget,
            attempt_id,
            operation,
            amount,
            owner_token,
            claim_fence,
            now or datetime.now(UTC),
        )

    async def release_live_budget(
        self,
        attempt_id: str,
        operation: Literal["controlled_pages", "parked_browsers"],
        *,
        amount: int = 1,
        scope: ProfileScope,
        owner_token: str,
        claim_fence: int,
        now: datetime | None = None,
    ) -> BrowserBudgetUsage:
        """Release only confirmed live-resource ownership, never cumulative attempts."""

        if amount < 1:
            raise ValueError("browser live-budget release amount must be positive")
        await self.get_attempt(attempt_id, scope=scope)
        return await self._call(
            self._release_live_budget,
            attempt_id,
            operation,
            amount,
            owner_token,
            claim_fence,
            now or datetime.now(UTC),
        )

    async def reserve_possible_pages(
        self,
        attempt_id: str,
        *,
        maximum: int,
        reservation_key: str,
        scope: ProfileScope,
        owner_token: str,
        claim_fence: int,
        now: datetime | None = None,
    ) -> BrowserBudgetReservation:
        """Preauthorize a maximum page count before a popup-capable action."""

        if maximum < 1:
            raise ValueError("possible page reservation must be positive")
        if len(reservation_key) != 64:
            raise ValueError("possible page reservation key must be a SHA-256 digest")
        await self.get_attempt(attempt_id, scope=scope)
        return await self._call(
            self._reserve_possible_pages,
            attempt_id,
            maximum,
            reservation_key,
            owner_token,
            claim_fence,
            now or datetime.now(UTC),
        )

    async def settle_possible_pages(
        self,
        reservation_id: str,
        *,
        consumed: int,
        in_doubt: bool,
        scope: ProfileScope,
        owner_token: str,
        claim_fence: int,
        now: datetime | None = None,
    ) -> BrowserBudgetReservation:
        """Settle definitive actual use; ambiguous work keeps the reserved maximum."""

        if consumed < 0:
            raise ValueError("consumed page count cannot be negative")
        attempt_id = await self._call(self._reservation_attempt, reservation_id)
        await self.get_attempt(attempt_id, scope=scope)
        return await self._call(
            self._settle_possible_pages,
            reservation_id,
            consumed,
            in_doubt,
            owner_token,
            claim_fence,
            now or datetime.now(UTC),
        )

    async def budget_usage(
        self, attempt_id: str, *, scope: ProfileScope
    ) -> list[BrowserBudgetUsage]:
        await self.get_attempt(attempt_id, scope=scope)
        return await self._call(self._budget_usage, attempt_id)

    async def checkpoint_navigation(
        self,
        checkpoint: BrowserNavigationCheckpoint,
        *,
        scope: ProfileScope,
        owner_token: str,
        claim_fence: int,
    ) -> BrowserNavigationCheckpoint:
        if checkpoint.id is not None:
            raise ValueError("new browser navigation checkpoints cannot provide an id")
        await self.get_attempt(checkpoint.attempt_id, scope=scope)
        return await self._call(
            self._checkpoint_navigation,
            checkpoint,
            owner_token,
            claim_fence,
        )

    async def record_action_evidence(
        self,
        evidence: BrowserActionEvidence,
        *,
        scope: ProfileScope,
        owner_token: str,
        claim_fence: int,
    ) -> BrowserActionEvidence:
        if evidence.id is not None:
            raise ValueError("new browser action evidence cannot provide an id")
        await self.get_attempt(evidence.attempt_id, scope=scope)
        return await self._call(
            self._record_action_evidence,
            evidence,
            owner_token,
            claim_fence,
        )

    async def action_evidence(
        self, attempt_id: str, *, scope: ProfileScope
    ) -> list[BrowserActionEvidence]:
        await self.get_attempt(attempt_id, scope=scope)
        return await self._call(self._action_evidence, attempt_id)

    async def recover_orphaned(self, *, scope: ProfileScope) -> list[BrowserAttempt]:
        return await self._call(self._recover_orphaned, scope, datetime.now(UTC))

    async def _call(self, operation: Any, *args: object) -> Any:
        task = asyncio.create_task(asyncio.to_thread(operation, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.shield(task)
            raise
        except (OSError, sqlite3.Error) as exc:
            raise JobStoreError(f"browser run ledger failed: {exc}") from exc

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(
            self.path,
            timeout=self._busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _start_attempt(
        self,
        run_id: str,
        profile_scope: ProfileScope,
        browser_scope: BrowserExecutionScope,
        claim_fence: int,
        worker_id: str,
        execution_request_id: str | None,
        resource: ProfileResourceRef | None,
        resource_kind: BrowserResourceKind,
        resource_configuration_digest: str | None,
        now: datetime,
    ) -> BrowserAttemptLease:
        attempt_id = f"browser_attempt_{uuid4().hex}"
        owner_token = f"browser_owner_{uuid4().hex}"
        attempt = BrowserAttempt(
            id=attempt_id,
            run_id=run_id,
            execution_request_id=execution_request_id,
            profile_label=profile_scope.label(),
            claim_fence=claim_fence,
            worker_id=worker_id,
            mode=browser_scope.mode,
            resource=resource,
            resource_kind=resource_kind,
            resource_configuration_digest=resource_configuration_digest,
            scope_digest=browser_scope.digest(),
            status="starting",
            cleanup="not_started",
            started_at=now,
            updated_at=now,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT outcome, trigger, trigger_id, profile_scope_json FROM job_runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if run is None or run["outcome"] is not None:
                raise JobStoreError("browser attempt requires a live job run")
            if ProfileScope.model_validate_json(run["profile_scope_json"]) != profile_scope:
                raise JobStoreError("browser attempt profile scope differs from its run")
            try:
                connection.execute(
                    """INSERT INTO browser_attempts (
                        id, run_id, execution_request_id, owner_token, claim_fence,
                        worker_id, mode, resource_json, resource_kind,
                        resource_configuration_digest, scope_digest, status, cleanup,
                        started_at, updated_at, finished_at, error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'starting', 'not_started',
                              ?, ?, NULL, NULL)""",
                    (
                        attempt.id,
                        attempt.run_id,
                        attempt.execution_request_id,
                        owner_token,
                        attempt.claim_fence,
                        attempt.worker_id,
                        attempt.mode,
                        resource.model_dump_json() if resource is not None else None,
                        attempt.resource_kind,
                        attempt.resource_configuration_digest,
                        attempt.scope_digest,
                        _iso(now),
                        _iso(now),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise JobStoreError("job run already owns a live browser attempt") from exc
            connection.executemany(
                """INSERT INTO browser_attempt_budgets
                    (attempt_id, operation, used, ceiling, updated_at)
                    VALUES (?, ?, 0, ?, ?)""",
                [
                    (attempt.id, operation, browser_scope.budget.ceiling(operation), _iso(now))
                    for operation in browser_scope.allowed_operations
                ],
            )
            connection.commit()
        return BrowserAttemptLease(attempt=attempt, owner_token=owner_token)

    def _get_attempt(self, attempt_id: str) -> BrowserAttempt | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT a.*, r.profile_scope_json FROM browser_attempts AS a
                JOIN job_runs AS r ON r.id=a.run_id WHERE a.id=?""",
                (attempt_id,),
            ).fetchone()
        return _row_to_attempt(row) if row is not None else None

    def _attempts_for_run(self, run_id: str) -> list[BrowserAttempt]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT a.*, r.profile_scope_json FROM browser_attempts AS a
                JOIN job_runs AS r ON r.id=a.run_id
                WHERE a.run_id=? ORDER BY a.started_at, a.id""",
                (run_id,),
            ).fetchall()
        return [_row_to_attempt(row) for row in rows]

    def _active_attempts(self, scope: ProfileScope) -> list[BrowserAttempt]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT a.*, r.profile_scope_json FROM browser_attempts AS a
                JOIN job_runs AS r ON r.id=a.run_id
                WHERE a.status IN ('starting','running','parked')
                ORDER BY a.started_at, a.id"""
            ).fetchall()
        return [
            attempt
            for row in rows
            if scope.permits((attempt := _row_to_attempt(row)).profile_label)
        ]

    def _has_ambiguous_effect_evidence(self, attempt_id: str) -> bool:
        with self._connect() as connection:
            return self._effect_evidence(connection, attempt_id)

    def _recover_lost_attempt(
        self,
        attempt_id: str,
        reason: str,
        now: datetime,
    ) -> BrowserAttempt:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = self._joined_attempt(connection, attempt_id)
            if before["status"] in _TERMINAL:
                connection.commit()
                return _row_to_attempt(before)
            status: BrowserAttemptStatus = (
                "in_doubt" if self._effect_evidence(connection, attempt_id) else "failed"
            )
            connection.execute(
                """UPDATE browser_attempts
                SET status=?, cleanup='failed', updated_at=?, finished_at=?, error=?
                WHERE id=?""",
                (status, _iso(now), _iso(now), reason, attempt_id),
            )
            current = self._joined_attempt(connection, attempt_id)
            connection.commit()
        return _row_to_attempt(current)

    def _transition(
        self,
        attempt_id: str,
        owner_token: str,
        claim_fence: int,
        status: BrowserAttemptStatus,
        cleanup: BrowserCleanupDisposition | None,
        error: str | None,
        now: datetime,
    ) -> BrowserAttempt:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = self._fenced(connection, attempt_id, owner_token, claim_fence)
            if before["status"] in _TERMINAL:
                raise BrowserAttemptFenceError("browser attempt is already terminal")
            allowed = {
                "starting": {"running", "failed", "cancelled", "uncertain"},
                "running": {"parked", "completed", "failed", "cancelled", "uncertain", "in_doubt"},
                "parked": {"running", "failed", "cancelled", "uncertain", "in_doubt"},
            }
            if status not in allowed[str(before["status"])]:
                raise BrowserAttemptFenceError(
                    f"invalid browser attempt transition: {before['status']} -> {status}"
                )
            final_cleanup = cleanup or str(before["cleanup"])
            if status == "completed" and final_cleanup != "confirmed":
                raise JobStoreError("completed browser attempt requires confirmed cleanup")
            if status in _TERMINAL and final_cleanup == "pending":
                raise JobStoreError("terminal browser attempt requires cleanup evidence")
            finished_at = _iso(now) if status in _TERMINAL else None
            connection.execute(
                """UPDATE browser_attempts
                SET status=?, cleanup=?, updated_at=?, finished_at=?, error=? WHERE id=?""",
                (status, final_cleanup, _iso(now), finished_at, error, attempt_id),
            )
            current = self._joined_attempt(connection, attempt_id)
            connection.commit()
        return _row_to_attempt(current)

    def _reserve_budget(
        self,
        attempt_id: str,
        operation: BrowserBudgetOperation,
        amount: int,
        owner_token: str,
        claim_fence: int,
        now: datetime,
    ) -> BrowserBudgetUsage:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = self._fenced(connection, attempt_id, owner_token, claim_fence)
            if before["status"] not in {"starting", "running"}:
                raise BrowserAttemptFenceError("parked or terminal browser cannot reserve work")
            cursor = connection.execute(
                """UPDATE browser_attempt_budgets
                SET used=used+?, updated_at=?
                WHERE attempt_id=? AND operation=? AND used+?<=ceiling""",
                (amount, _iso(now), attempt_id, operation, amount),
            )
            if cursor.rowcount != 1:
                exists = connection.execute(
                    """SELECT 1 FROM browser_attempt_budgets
                    WHERE attempt_id=? AND operation=?""",
                    (attempt_id, operation),
                ).fetchone()
                if exists is None:
                    raise BrowserBudgetExceededError(
                        f"browser operation is outside its contract: {operation}"
                    )
                raise BrowserBudgetExceededError(f"browser budget exhausted: {operation}")
            row = connection.execute(
                """SELECT * FROM browser_attempt_budgets
                WHERE attempt_id=? AND operation=?""",
                (attempt_id, operation),
            ).fetchone()
            connection.commit()
        assert row is not None
        return BrowserBudgetUsage.model_validate(dict(row))

    def _release_live_budget(
        self,
        attempt_id: str,
        operation: str,
        amount: int,
        owner_token: str,
        claim_fence: int,
        now: datetime,
    ) -> BrowserBudgetUsage:
        if operation not in _LIVE_BUDGETS:
            raise JobStoreError("only live browser resource budgets can be released")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._fenced(connection, attempt_id, owner_token, claim_fence)
            cursor = connection.execute(
                """UPDATE browser_attempt_budgets
                SET used=used-?, updated_at=?
                WHERE attempt_id=? AND operation=? AND used>=?""",
                (amount, _iso(now), attempt_id, operation, amount),
            )
            if cursor.rowcount != 1:
                raise JobStoreError("browser live-budget release exceeds confirmed ownership")
            row = connection.execute(
                """SELECT * FROM browser_attempt_budgets
                WHERE attempt_id=? AND operation=?""",
                (attempt_id, operation),
            ).fetchone()
            connection.commit()
        assert row is not None
        return BrowserBudgetUsage.model_validate(dict(row))

    def _reserve_possible_pages(
        self,
        attempt_id: str,
        maximum: int,
        reservation_key: str,
        owner_token: str,
        claim_fence: int,
        now: datetime,
    ) -> BrowserBudgetReservation:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = self._fenced(connection, attempt_id, owner_token, claim_fence)
            if before["status"] != "running":
                raise BrowserAttemptFenceError("page reservation requires a running browser")
            existing = connection.execute(
                """SELECT * FROM browser_budget_reservations
                WHERE attempt_id=? AND operation='created_pages' AND reservation_key=?""",
                (attempt_id, reservation_key),
            ).fetchone()
            if existing is not None:
                if int(existing["reserved"]) != maximum:
                    raise JobStoreError("page reservation key is bound to another maximum")
                connection.commit()
                return _row_to_budget_reservation(existing)
            cursor = connection.execute(
                """UPDATE browser_attempt_budgets SET used=used+?, updated_at=?
                WHERE attempt_id=? AND operation='created_pages' AND used+?<=ceiling""",
                (maximum, _iso(now), attempt_id, maximum),
            )
            if cursor.rowcount != 1:
                raise BrowserBudgetExceededError("browser budget exhausted: created_pages")
            reservation = BrowserBudgetReservation(
                id=f"browser_budget_{uuid4().hex}",
                attempt_id=attempt_id,
                operation="created_pages",
                reservation_key=reservation_key,
                reserved=maximum,
                consumed=0,
                state="pending",
                created_at=now,
            )
            connection.execute(
                """INSERT INTO browser_budget_reservations (
                    id, attempt_id, operation, reservation_key, reserved, consumed,
                    state, created_at, settled_at
                ) VALUES (?, ?, 'created_pages', ?, ?, 0, 'pending', ?, NULL)""",
                (
                    reservation.id,
                    attempt_id,
                    reservation_key,
                    maximum,
                    _iso(now),
                ),
            )
            connection.commit()
        return reservation

    def _reservation_attempt(self, reservation_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempt_id FROM browser_budget_reservations WHERE id=?",
                (reservation_id,),
            ).fetchone()
        if row is None:
            raise JobStoreError(f"browser budget reservation not found: {reservation_id}")
        return str(row["attempt_id"])

    def _settle_possible_pages(
        self,
        reservation_id: str,
        consumed: int,
        in_doubt: bool,
        owner_token: str,
        claim_fence: int,
        now: datetime,
    ) -> BrowserBudgetReservation:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM browser_budget_reservations WHERE id=?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise JobStoreError(f"browser budget reservation not found: {reservation_id}")
            self._fenced(connection, row["attempt_id"], owner_token, claim_fence)
            reserved = int(row["reserved"])
            if consumed > reserved:
                raise JobStoreError("created page evidence exceeds its preauthorization")
            effective = reserved if in_doubt else consumed
            state = "in_doubt" if in_doubt else "settled"
            if row["state"] != "pending":
                if int(row["consumed"]) != effective or row["state"] != state:
                    raise JobStoreError(
                        "created-page reservation already has different settlement evidence"
                    )
                connection.commit()
                return _row_to_budget_reservation(row)
            released = reserved - effective
            if released:
                cursor = connection.execute(
                    """UPDATE browser_attempt_budgets SET used=used-?, updated_at=?
                    WHERE attempt_id=? AND operation='created_pages' AND used>=?""",
                    (released, _iso(now), row["attempt_id"], released),
                )
                if cursor.rowcount != 1:
                    raise JobStoreError("created-page budget settlement is inconsistent")
            connection.execute(
                """UPDATE browser_budget_reservations
                SET consumed=?, state=?, settled_at=? WHERE id=?""",
                (effective, state, _iso(now), reservation_id),
            )
            current = connection.execute(
                "SELECT * FROM browser_budget_reservations WHERE id=?",
                (reservation_id,),
            ).fetchone()
            connection.commit()
        assert current is not None
        return _row_to_budget_reservation(current)

    def _budget_usage(self, attempt_id: str) -> list[BrowserBudgetUsage]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM browser_attempt_budgets
                WHERE attempt_id=? ORDER BY operation""",
                (attempt_id,),
            ).fetchall()
        return [BrowserBudgetUsage.model_validate(dict(row)) for row in rows]

    def _checkpoint_navigation(
        self,
        checkpoint: BrowserNavigationCheckpoint,
        owner_token: str,
        claim_fence: int,
    ) -> BrowserNavigationCheckpoint:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = self._fenced(connection, checkpoint.attempt_id, owner_token, claim_fence)
            if before["status"] != "running":
                raise BrowserAttemptFenceError("navigation checkpoint requires running browser")
            cursor = connection.execute(
                """INSERT INTO browser_navigation_checkpoints (
                    attempt_id, page_generation, top_level_origin, url_projection, created_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    checkpoint.attempt_id,
                    checkpoint.page_generation,
                    checkpoint.top_level_origin,
                    checkpoint.url_projection,
                    _iso(checkpoint.created_at),
                ),
            )
            connection.commit()
        checkpoint_id = cursor.lastrowid
        if checkpoint_id is None:
            raise JobStoreError("browser navigation checkpoint was not recorded")
        return checkpoint.model_copy(update={"id": checkpoint_id})

    def _record_action_evidence(
        self,
        evidence: BrowserActionEvidence,
        owner_token: str,
        claim_fence: int,
    ) -> BrowserActionEvidence:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = self._fenced(connection, evidence.attempt_id, owner_token, claim_fence)
            if before["status"] not in {"running", "parked"}:
                raise BrowserAttemptFenceError("browser action evidence requires a live attempt")
            if evidence.action_id is not None:
                action = connection.execute(
                    "SELECT run_id, action_key, status FROM job_actions WHERE id=?",
                    (evidence.action_id,),
                ).fetchone()
                if (
                    action is None
                    or action["run_id"] != before["run_id"]
                    or action["action_key"] != evidence.logical_effect_key
                    or action["status"] != evidence.disposition
                ):
                    raise JobStoreError("browser evidence differs from shared effect ledger")
            cursor = connection.execute(
                """INSERT INTO browser_action_evidence (
                    attempt_id, action_id, operation, logical_effect_key,
                    live_occurrence_digest, disposition, attempt_reason,
                    binding_json, postcondition, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    evidence.attempt_id,
                    evidence.action_id,
                    evidence.operation,
                    evidence.logical_effect_key,
                    evidence.live_occurrence_digest,
                    evidence.disposition,
                    evidence.attempt_reason,
                    evidence.binding.model_dump_json() if evidence.binding is not None else None,
                    evidence.postcondition,
                    _iso(evidence.created_at),
                ),
            )
            connection.commit()
        evidence_id = cursor.lastrowid
        if evidence_id is None:
            raise JobStoreError("browser action evidence was not recorded")
        return evidence.model_copy(update={"id": evidence_id})

    def _action_evidence(self, attempt_id: str) -> list[BrowserActionEvidence]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM browser_action_evidence
                WHERE attempt_id=? ORDER BY id""",
                (attempt_id,),
            ).fetchall()
        return [_row_to_evidence(row) for row in rows]

    def _recover_orphaned(self, scope: ProfileScope, now: datetime) -> list[BrowserAttempt]:
        recovered: list[BrowserAttempt] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT a.*, r.profile_scope_json FROM browser_attempts AS a
                JOIN job_runs AS r ON r.id=a.run_id
                WHERE a.status IN ('starting','running','parked') AND r.outcome IS NOT NULL"""
            ).fetchall()
            for row in rows:
                before = _row_to_attempt(row)
                if not scope.permits(before.profile_label):
                    continue
                effect = self._effect_evidence(connection, before.id)
                status: BrowserAttemptStatus = "in_doubt" if effect else "failed"
                error = (
                    "browser owner ended with unresolved external-effect evidence"
                    if effect is not None
                    else "browser owner ended before confirmed cleanup"
                )
                connection.execute(
                    """UPDATE browser_attempts SET status=?, cleanup='failed', updated_at=?,
                    finished_at=?, error=? WHERE id=?""",
                    (status, _iso(now), _iso(now), error, before.id),
                )
                recovered.append(_row_to_attempt(self._joined_attempt(connection, before.id)))
            connection.commit()
        return recovered

    @staticmethod
    def _effect_evidence(connection: sqlite3.Connection, attempt_id: str) -> bool:
        row = connection.execute(
            """SELECT run_id FROM browser_attempts WHERE id=?""",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise JobStoreError(f"browser attempt not found: {attempt_id}")
        browser = connection.execute(
            """SELECT 1 FROM browser_action_evidence
            WHERE attempt_id=? AND disposition IN ('reserved','performed','in_doubt')
            LIMIT 1""",
            (attempt_id,),
        ).fetchone()
        if browser is not None:
            return True
        shared = connection.execute(
            """SELECT 1 FROM job_actions
            WHERE run_id=? AND status IN ('reserved','performed','in_doubt') LIMIT 1""",
            (row["run_id"],),
        ).fetchone()
        return shared is not None

    def _fenced(
        self,
        connection: sqlite3.Connection,
        attempt_id: str,
        owner_token: str,
        claim_fence: int,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM browser_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise JobStoreError(f"browser attempt not found: {attempt_id}")
        if row["owner_token"] != owner_token or int(row["claim_fence"]) != claim_fence:
            raise BrowserAttemptFenceError("stale browser attempt owner")
        return row

    @staticmethod
    def _joined_attempt(connection: sqlite3.Connection, attempt_id: str) -> sqlite3.Row:
        row = connection.execute(
            """SELECT a.*, r.profile_scope_json FROM browser_attempts AS a
            JOIN job_runs AS r ON r.id=a.run_id WHERE a.id=?""",
            (attempt_id,),
        ).fetchone()
        assert row is not None
        return row


def _row_to_attempt(row: sqlite3.Row) -> BrowserAttempt:
    resource = (
        ProfileResourceRef.model_validate_json(row["resource_json"])
        if row["resource_json"] is not None
        else None
    )
    return BrowserAttempt(
        id=row["id"],
        run_id=row["run_id"],
        execution_request_id=row["execution_request_id"],
        profile_label=ProfileScope.model_validate_json(row["profile_scope_json"]).label(),
        claim_fence=row["claim_fence"],
        worker_id=row["worker_id"],
        mode=row["mode"],
        resource=resource,
        resource_kind=row["resource_kind"],
        resource_configuration_digest=row["resource_configuration_digest"],
        scope_digest=row["scope_digest"],
        status=row["status"],
        cleanup=row["cleanup"],
        started_at=datetime.fromisoformat(row["started_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        finished_at=(
            datetime.fromisoformat(row["finished_at"]) if row["finished_at"] is not None else None
        ),
        error=row["error"],
    )


def _row_to_evidence(row: sqlite3.Row) -> BrowserActionEvidence:
    values = dict(row)
    binding_json = values.pop("binding_json")
    values["binding"] = json.loads(binding_json) if binding_json else None
    return BrowserActionEvidence.model_validate(values)


def _row_to_budget_reservation(row: sqlite3.Row) -> BrowserBudgetReservation:
    return BrowserBudgetReservation(
        id=row["id"],
        attempt_id=row["attempt_id"],
        operation=row["operation"],
        reservation_key=row["reservation_key"],
        reserved=row["reserved"],
        consumed=row["consumed"],
        state=row["state"],
        created_at=datetime.fromisoformat(row["created_at"]),
        settled_at=(
            datetime.fromisoformat(row["settled_at"]) if row["settled_at"] is not None else None
        ),
    )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()
