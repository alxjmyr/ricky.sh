"""SQLite persistence for immutable grants and append-only authority activity.

The grant record itself is immutable except for its lifecycle ``status``: a
grant is never edited to become wider. Adding authority means issuing a new
grant.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from ricky.authority.types import (
    AuthorityScope,
    DelegationGrant,
    EffectDisposition,
    GrantActivity,
    GrantActivityKind,
    GrantSource,
    GrantStatus,
    validate_grant_id,
)
from ricky.config import RickySettings, user_data_path
from ricky.executions.contracts import ConfirmationRef
from ricky.profiles import ProfileScope

SCHEMA_VERSION = 4


class AuthorityStoreError(RuntimeError):
    """The authority store rejected an operation."""


class GrantNotFoundError(AuthorityStoreError):
    """The requested delegation grant does not exist."""


class GrantStateError(AuthorityStoreError):
    """A grant is not in the state the operation requires."""


class AuthorityStore:
    """Short-transaction store for delegation grants and their activity."""

    def __init__(self, settings: RickySettings) -> None:
        self.settings = settings.authority
        self.user_root = user_data_path(settings)
        self.db_path = self.user_root / self.settings.store_path

    async def initialize(self) -> None:
        await self._run(self._initialize)

    async def issue(
        self,
        grant: DelegationGrant,
        *,
        scope: ProfileScope,
    ) -> DelegationGrant:
        if grant.status != "active":
            raise ValueError("a newly issued grant must be active")
        _require_profile_access(scope, grant, grant.id)
        return await self._run(self._issue, grant)

    async def get(self, grant_id: str, *, scope: ProfileScope) -> DelegationGrant:
        found = await self._run(self._get, validate_grant_id(grant_id))
        if found is None or not scope.permits(found.profile_scope.label()):
            raise GrantNotFoundError(f"delegation grant not found: {grant_id}")
        return found

    async def list(
        self,
        *,
        scope: ProfileScope,
        task_id: str | None = None,
        status: GrantStatus | None = None,
        limit: int = 50,
    ) -> list[DelegationGrant]:
        if limit < 1 or limit > 1_000:
            raise ValueError("grant list limit must be between 1 and 1000")
        found = await self._run(self._list, task_id, status, 1_000)
        return [grant for grant in found if scope.permits(grant.profile_scope.label())][:limit]

    async def attach_execution(
        self,
        grant_id: str,
        execution_request_id: str,
        *,
        scope: ProfileScope,
    ) -> DelegationGrant:
        await self.get(grant_id, scope=scope)
        return await self._run(
            self._attach_execution, validate_grant_id(grant_id), execution_request_id
        )

    async def load_active(
        self,
        grant_id: str,
        *,
        scope: ProfileScope,
        now: datetime | None = None,
    ) -> DelegationGrant:
        """Return an active, unexpired grant or fail closed.

        Expiry is recorded durably the first time it is observed, so an expired
        grant is inspectable rather than silently unusable.
        """

        await self.get(grant_id, scope=scope)
        return await self._run(self._load_active, validate_grant_id(grant_id), now or _now())

    async def revoke(
        self,
        grant_id: str,
        *,
        scope: ProfileScope,
        actor: str,
        reason: str,
    ) -> DelegationGrant:
        if not actor.strip() or not reason.strip():
            raise ValueError("revocation actor and reason cannot be blank")
        await self.get(grant_id, scope=scope)
        return await self._run(
            self._terminate,
            validate_grant_id(grant_id),
            "revoked",
            "revoked",
            f"{reason.strip()} (by {actor.strip()})",
        )

    async def consume(
        self,
        grant_id: str,
        *,
        scope: ProfileScope,
        reason: str,
    ) -> DelegationGrant:
        await self.get(grant_id, scope=scope)
        return await self._run(
            self._terminate, validate_grant_id(grant_id), "consumed", "consumed", reason
        )

    async def record(
        self,
        grant_id: str,
        kind: GrantActivityKind,
        summary: str,
        *,
        scope: ProfileScope,
        capability: str | None = None,
        tool_name: str | None = None,
        action_id: str | None = None,
        disposition: EffectDisposition | None = None,
    ) -> GrantActivity:
        grant = await self.get(grant_id, scope=scope)
        return await self._run(
            self._record,
            validate_grant_id(grant_id),
            kind,
            summary,
            capability,
            tool_name,
            action_id,
            disposition,
            grant.profile_scope,
        )

    async def activities(
        self,
        grant_id: str,
        *,
        scope: ProfileScope,
        limit: int = 200,
    ) -> list[GrantActivity]:
        if limit < 1 or limit > 1_000:
            raise ValueError("activity limit must be between 1 and 1000")
        grant = await self.get(grant_id, scope=scope)
        return await self._run(
            self._activities,
            validate_grant_id(grant_id),
            grant.profile_scope,
            limit,
        )

    async def active_execution_request_ids(self, *, scope: ProfileScope) -> tuple[str, ...]:
        """Return every request referenced by active authority without a list cap."""

        return await self._run(self._active_execution_request_ids, scope)

    # -- internals ---------------------------------------------------------

    async def _run[T](self, operation: Callable[..., T], *args: object) -> T:
        task = asyncio.create_task(asyncio.to_thread(operation, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.shield(task)
            raise
        except sqlite3.Error as exc:
            raise AuthorityStoreError("authority store operation failed") from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=self.settings.sqlite_busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(f"PRAGMA busy_timeout = {self.settings.sqlite_busy_timeout_ms}")
        return connection

    def _initialize(self) -> None:
        from ricky.authority.upgrade import (
            create_current_authority_database,
            inspect_authority_database,
        )

        if not self.db_path.exists():
            create_current_authority_database(self.db_path)
            return
        inspection = inspect_authority_database(self.db_path)
        if inspection.state != "current":
            raise AuthorityStoreError(inspection.detail)
        os.chmod(self.db_path.parent, 0o700)
        os.chmod(self.db_path, 0o600)

    def _issue(self, grant: DelegationGrant) -> DelegationGrant:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"INSERT INTO delegation_grants ({','.join(_COLUMNS)}) "
                f"VALUES ({','.join('?' for _ in _COLUMNS)})",
                _values(grant),
            )
            self._append(connection, grant.id, "issued", grant.summary, None, None, None, None)
            connection.commit()
        return grant

    def _get(self, grant_id: str) -> DelegationGrant | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM delegation_grants WHERE id = ?", (grant_id,)
            ).fetchone()
        return _row(row) if row is not None else None

    def _list(
        self, task_id: str | None, status: GrantStatus | None, limit: int
    ) -> list[DelegationGrant]:
        sql = "SELECT * FROM delegation_grants"
        clauses: list[str] = []
        params: list[object] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY issued_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            return [_row(row) for row in connection.execute(sql, params).fetchall()]

    def _active_execution_request_ids(self, scope: ProfileScope) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM delegation_grants
                WHERE status = 'active' AND execution_request_id IS NOT NULL
                ORDER BY execution_request_id ASC
                """
            ).fetchall()
        return tuple(
            str(grant.execution_request_id)
            for row in rows
            if scope.permits((grant := _row(row)).profile_scope.label())
            and grant.execution_request_id is not None
        )

    def _attach_execution(self, grant_id: str, execution_request_id: str) -> DelegationGrant:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._required(connection, grant_id)
            if current.status != "active":
                raise GrantStateError(f"grant is {current.status}; it cannot accept work")
            if current.execution_request_id is not None:
                if current.execution_request_id == execution_request_id:
                    connection.commit()
                    return current
                raise GrantStateError("grant is already attached to another execution request")
            connection.execute(
                "UPDATE delegation_grants SET execution_request_id = ? WHERE id = ?"
                " AND execution_request_id IS NULL AND status = 'active'",
                (execution_request_id, grant_id),
            )
            updated = self._required(connection, grant_id)
            if updated.execution_request_id != execution_request_id:
                raise GrantStateError("grant execution attachment lost a race")
            self._append(
                connection,
                grant_id,
                "issued",
                f"Attached to execution request {execution_request_id}",
                None,
                None,
                None,
                None,
            )
            connection.commit()
        return updated

    def _load_active(self, grant_id: str, now: datetime) -> DelegationGrant:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._required(connection, grant_id)
            if current.status != "active":
                raise GrantStateError(f"delegation grant is {current.status}")
            if current.expires_at <= now:
                connection.execute(
                    "UPDATE delegation_grants SET status = 'expired' WHERE id = ? "
                    "AND status = 'active'",
                    (grant_id,),
                )
                self._append(
                    connection,
                    grant_id,
                    "expired",
                    "Grant expired before use",
                    None,
                    None,
                    None,
                    None,
                )
                connection.commit()
                raise GrantStateError("delegation grant is expired")
            connection.commit()
        return current

    def _terminate(
        self, grant_id: str, status: GrantStatus, kind: GrantActivityKind, summary: str
    ) -> DelegationGrant:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._required(connection, grant_id)
            if current.status != "active":
                raise GrantStateError(f"grant is already {current.status}")
            connection.execute(
                "UPDATE delegation_grants SET status = ? WHERE id = ? AND status = 'active'",
                (status, grant_id),
            )
            self._append(connection, grant_id, kind, summary, None, None, None, None)
            updated = self._required(connection, grant_id)
            connection.commit()
        return updated

    def _record(
        self,
        grant_id: str,
        kind: GrantActivityKind,
        summary: str,
        capability: str | None,
        tool_name: str | None,
        action_id: str | None,
        disposition: EffectDisposition | None,
        profile_scope: ProfileScope,
    ) -> GrantActivity:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._required(connection, grant_id)
            activity = self._append(
                connection,
                grant_id,
                kind,
                summary,
                capability,
                tool_name,
                action_id,
                disposition,
                profile_scope,
            )
            connection.commit()
        return activity

    def _activities(
        self,
        grant_id: str,
        profile_scope: ProfileScope,
        limit: int,
    ) -> list[GrantActivity]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM grant_activities WHERE grant_id = ? ORDER BY id LIMIT ?",
                (grant_id, limit),
            ).fetchall()
        return [_activity(row, profile_scope) for row in rows]

    def _required(self, connection: sqlite3.Connection, grant_id: str) -> DelegationGrant:
        row = connection.execute(
            "SELECT * FROM delegation_grants WHERE id = ?", (grant_id,)
        ).fetchone()
        if row is None:
            raise GrantNotFoundError(f"delegation grant not found: {grant_id}")
        return _row(row)

    def _append(
        self,
        connection: sqlite3.Connection,
        grant_id: str,
        kind: GrantActivityKind,
        summary: str,
        capability: str | None,
        tool_name: str | None,
        action_id: str | None,
        disposition: EffectDisposition | None,
        profile_scope: ProfileScope | None = None,
    ) -> GrantActivity:
        created_at = _now()
        cursor = connection.execute(
            """
            INSERT INTO grant_activities
                (grant_id, kind, capability, tool_name, action_id, disposition,
                 summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                grant_id,
                kind,
                capability,
                tool_name,
                action_id,
                disposition,
                summary[:2_000],
                _dt(created_at),
            ),
        )
        activity_id = cursor.lastrowid
        if activity_id is None:
            raise AuthorityStoreError("authority activity did not receive an audit id")
        if profile_scope is None:
            profile_scope = self._required(connection, grant_id).profile_scope
        return GrantActivity(
            id=activity_id,
            grant_id=grant_id,
            profile_label=profile_scope.label(),
            kind=kind,
            capability=capability,
            tool_name=tool_name,
            action_id=action_id,
            disposition=disposition,
            summary=summary[:2_000],
            created_at=created_at,
        )


_COLUMNS = (
    "id",
    "source_json",
    "task_id",
    "task_revision",
    "profile_scope_json",
    "execution_request_id",
    "contract_id",
    "contract_digest",
    "confirmations_json",
    "scopes_json",
    "summary",
    "effect_call_limit",
    "financial_limit_minor",
    "currency",
    "issued_at",
    "expires_at",
    "status",
    "policy_digest",
)


def _values(grant: DelegationGrant) -> tuple[Any, ...]:
    return (
        grant.id,
        grant.source.model_dump_json(),
        grant.task_id,
        grant.task_revision,
        grant.profile_scope.model_dump_json(),
        grant.execution_request_id,
        grant.contract_id,
        grant.contract_digest,
        json.dumps(
            [item.model_dump(mode="json") for item in grant.confirmations],
            sort_keys=True,
        ),
        json.dumps([scope.model_dump(mode="json") for scope in grant.scopes], sort_keys=True),
        grant.summary,
        grant.effect_call_limit,
        grant.financial_limit_minor,
        grant.currency,
        _dt(grant.issued_at),
        _dt(grant.expires_at),
        grant.status,
        grant.policy_digest,
    )


def _row(row: sqlite3.Row) -> DelegationGrant:
    values = dict(row)
    return DelegationGrant(
        id=values["id"],
        source=GrantSource.model_validate_json(values["source_json"]),
        task_id=values["task_id"],
        task_revision=values["task_revision"],
        profile_scope=ProfileScope.model_validate_json(values["profile_scope_json"]),
        execution_request_id=values["execution_request_id"],
        contract_id=values["contract_id"],
        contract_digest=values["contract_digest"],
        confirmations=tuple(
            ConfirmationRef.model_validate(item)
            for item in json.loads(values.get("confirmations_json") or "[]")
        ),
        scopes=tuple(
            AuthorityScope.model_validate(item) for item in json.loads(values["scopes_json"])
        ),
        summary=values["summary"],
        effect_call_limit=values["effect_call_limit"],
        financial_limit_minor=values["financial_limit_minor"],
        currency=values["currency"],
        issued_at=datetime.fromisoformat(values["issued_at"]),
        expires_at=datetime.fromisoformat(values["expires_at"]),
        status=values["status"],
        policy_digest=values["policy_digest"],
    )


def _activity(row: sqlite3.Row, profile_scope: ProfileScope) -> GrantActivity:
    values = dict(row)
    values["created_at"] = datetime.fromisoformat(values["created_at"])
    values["profile_label"] = profile_scope.label()
    return GrantActivity.model_validate(values)


def _require_profile_access(
    scope: ProfileScope,
    grant: DelegationGrant,
    grant_id: str,
) -> None:
    if not scope.permits(grant.profile_scope.label()):
        raise GrantNotFoundError(f"delegation grant not found: {grant_id}")


def _now() -> datetime:
    return datetime.now(UTC)


def _dt(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


_SCHEMA = """
CREATE TABLE delegation_grants (
    id TEXT PRIMARY KEY,
    source_json TEXT NOT NULL,
    task_id TEXT NOT NULL,
    task_revision INTEGER NOT NULL,
    profile_scope_json TEXT NOT NULL,
    execution_request_id TEXT UNIQUE,
    contract_id TEXT NOT NULL,
    contract_digest TEXT NOT NULL,
    confirmations_json TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    effect_call_limit INTEGER NOT NULL,
    financial_limit_minor INTEGER,
    currency TEXT,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','revoked','expired','consumed')),
    policy_digest TEXT NOT NULL
);
CREATE INDEX delegation_grants_task_idx ON delegation_grants(task_id, issued_at DESC);
CREATE INDEX delegation_grants_status_idx ON delegation_grants(status, issued_at DESC);
CREATE TABLE grant_activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grant_id TEXT NOT NULL REFERENCES delegation_grants(id),
    kind TEXT NOT NULL,
    capability TEXT,
    tool_name TEXT,
    action_id TEXT,
    disposition TEXT,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX grant_activities_grant_idx ON grant_activities(grant_id, id);
"""
