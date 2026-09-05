"""Fenced SQLite persistence for agent conversations."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from pydantic import ValidationError

from ricky.agent.session import AgentSession
from ricky.config import RickySettings, user_data_subpath
from ricky.profiles import ProfileLabel, ProfileScope
from ricky.sessions.types import (
    SessionLease,
    SessionStatus,
    StaleSessionLease,
    StoredSession,
    StoredTurn,
)

SCHEMA_VERSION = 1


class SessionStoreError(RuntimeError):
    """Base error safe to surface without raw SQLite details."""


class SessionNotFoundError(SessionStoreError):
    """The requested session does not exist."""


class SessionConflictError(SessionStoreError):
    """A session revision changed concurrently."""


class SessionLeaseError(SessionStoreError):
    """A lease is busy, expired, foreign, or stale."""


class SessionStateError(SessionStoreError):
    """The requested session or turn transition is invalid."""


class SessionSchemaError(SessionStoreError):
    """Stored schema or canonical JSON is unsupported or malformed."""


class SessionStore:
    """Async-first application API over a short-transaction SQLite store."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings.sessions
        self.db_path = user_data_subpath(settings, self.settings.store_path)
        self.root = self.db_path.parent
        self._clock = clock or (lambda: datetime.now(UTC))

    async def initialize(self) -> None:
        await self._run(self._initialize)

    async def create(self, session: AgentSession, *, scope: ProfileScope) -> StoredSession:
        _require_profile_access(scope, session.profile_scope.label(), "session", session.id)
        return await self._run(self._create, session)

    async def get(self, session_id: str, *, scope: ProfileScope) -> StoredSession:
        return await self._run(self._get, _session_id(session_id), scope)

    async def list(
        self,
        *,
        scope: ProfileScope,
        status: SessionStatus | None = None,
        limit: int = 50,
    ) -> list[StoredSession]:
        if limit < 1 or limit > 1_000:
            raise ValueError("session list limit must be between 1 and 1000")
        return await self._run(self._list, scope, status, limit)

    async def acquire(
        self,
        session_id: str,
        owner: str,
        *,
        scope: ProfileScope,
        lease_seconds: int | None = None,
    ) -> SessionLease:
        duration = lease_seconds or self.settings.lease_seconds
        if duration < 1 or duration > 3_600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        if not owner or len(owner) > 200:
            raise ValueError("lease owner must contain 1 to 200 characters")
        return await self._run(self._acquire, _session_id(session_id), owner, scope, duration)

    async def renew(self, lease: SessionLease) -> SessionLease:
        return await self._run(self._renew, lease)

    async def begin_turn(self, lease: SessionLease, turn: StoredTurn) -> StoredTurn:
        """Persist a running turn before constructing runtime resources."""

        return await self._run(self._begin_turn, lease, turn)

    async def commit(
        self,
        lease: SessionLease,
        expected_revision: int,
        session: AgentSession,
        turn: StoredTurn,
    ) -> StoredSession:
        return await self._run(self._commit, lease, expected_revision, session, turn)

    async def fail_turn(
        self,
        lease: SessionLease,
        turn_id: str,
        error: str,
        uncertain: bool,
    ) -> StoredTurn:
        if not error:
            raise ValueError("turn error cannot be empty")
        return await self._run(
            self._fail_turn,
            lease,
            _turn_id(turn_id),
            error[:16_000],
            uncertain,
        )

    async def release(self, lease: SessionLease) -> None:
        await self._run(self._release, lease)

    async def stale_leases(
        self,
        *,
        scope: ProfileScope,
        now: datetime | None = None,
    ) -> list[StaleSessionLease]:
        """List session leases whose holder is gone, with any running turn."""

        return await self._run(self._stale_leases, scope, now)

    async def recover_expired_lease(
        self,
        session_id: str,
        *,
        scope: ProfileScope,
        error: str,
        now: datetime | None = None,
    ) -> StaleSessionLease:
        """Release one expired lease and mark its interrupted turns uncertain.

        The fence advances so a resumed holder of the old token can never commit,
        and a turn that had begun becomes ``uncertain`` rather than ``failed``
        because this process cannot observe what that turn already emitted.
        """

        if not error:
            raise ValueError("recovery error cannot be empty")
        return await self._run(
            self._recover_expired_lease,
            _session_id(session_id),
            scope,
            error,
            now,
        )

    async def session_counts(self, *, scope: ProfileScope) -> dict[str, int]:
        """Count stored sessions by status."""

        return await self._run(self._session_counts, scope)

    async def archive(
        self,
        session_id: str,
        expected_revision: int,
        *,
        scope: ProfileScope,
    ) -> StoredSession:
        return await self._run(self._archive, _session_id(session_id), scope, expected_revision)

    async def turns(
        self,
        session_id: str,
        *,
        scope: ProfileScope,
        limit: int = 50,
    ) -> list[StoredTurn]:
        if limit < 1 or limit > self.settings.turn_retention:
            raise ValueError("turn limit is outside the configured retention range")
        return await self._run(self._turns, _session_id(session_id), scope, limit)

    def artifact_root(self, session_id: str, *, scope: ProfileScope) -> Path:
        """Return the configured per-session artifact directory."""

        validated_id = _session_id(session_id)
        self._get(validated_id, scope)
        return self.root / validated_id / "artifacts"

    async def _run(self, operation: Callable[..., Any], *args: Any) -> Any:
        try:
            task = asyncio.create_task(asyncio.to_thread(operation, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # SQLite cannot be cancelled once its worker thread starts.
                # Join it so callers never race an indeterminate transaction.
                await task
                raise
        except SessionStoreError:
            raise
        except sqlite3.Error as exc:
            raise SessionStoreError("persistent session store operation failed") from exc

    def _initialize(self) -> None:
        from ricky.sessions.upgrade import (
            SessionsUpgradeError,
            create_current_sessions_store,
            inspect_sessions_store,
        )

        inspection = inspect_sessions_store(self.db_path)
        if inspection.state != "current":
            if inspection.state != "absent":
                raise SessionSchemaError(inspection.detail)
            try:
                create_current_sessions_store(self.db_path)
            except SessionsUpgradeError as exc:
                raise SessionSchemaError(str(exc)) from exc
        # Sessions retain conversation history as private evidence, so every open
        # re-asserts the private modes instead of trusting the creation path. A
        # store restored, copied, or repaired by another tool heals on reopen.
        os.chmod(self.root, 0o700)
        os.chmod(self.db_path, 0o600)
        for path in (Path(f"{self.db_path}-wal"), Path(f"{self.db_path}-shm")):
            # SQLite removes transient sidecars when the last connection
            # closes. Their disappearance during mode repair is benign.
            with suppress(FileNotFoundError):
                os.chmod(path, 0o600)

    def _create(self, session: AgentSession) -> StoredSession:
        durable = _durable_session(session)
        now = _utc(self._clock())
        payload = _dump_session(durable)
        try:
            with self._transaction() as connection:
                connection.execute(
                    """INSERT INTO sessions(
                           id, schema_version, session_json, revision, status,
                           created_at, updated_at, lease_fence
                       ) VALUES (?, ?, ?, 0, 'active', ?, ?, 0)""",
                    (durable.id, SCHEMA_VERSION, payload, _dump_dt(now), _dump_dt(now)),
                )
        except sqlite3.IntegrityError as exc:
            raise SessionConflictError(f"session already exists: {durable.id}") from exc
        return StoredSession(
            session=durable,
            profile_label=durable.profile_scope.label(),
            revision=0,
            status="active",
            created_at=now,
            updated_at=now,
            last_turn_id=None,
        )

    def _get(self, session_id: str, scope: ProfileScope) -> StoredSession:
        with self._connect() as connection:
            row = self._required_session(connection, session_id)
            stored = self._stored_session(row)
            _require_profile_access(scope, stored.profile_label, "session", session_id)
            return stored

    def _list(
        self,
        scope: ProfileScope,
        status: SessionStatus | None,
        limit: int,
    ) -> list[StoredSession]:
        with self._connect() as connection:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM sessions ORDER BY updated_at DESC, id"
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM sessions WHERE status = ?
                       ORDER BY updated_at DESC, id""",
                    (status,),
                ).fetchall()
            permitted = [
                stored
                for row in rows
                if scope.permits((stored := self._stored_session(row)).profile_label)
            ]
            return permitted[:limit]

    def _acquire(
        self,
        session_id: str,
        owner: str,
        scope: ProfileScope,
        duration: int,
    ) -> SessionLease:
        now = _utc(self._clock())
        expires = now + timedelta(seconds=duration)
        token = f"lease_{uuid4().hex}"
        with self._transaction() as connection:
            row = self._required_session(connection, session_id)
            label = self._stored_session(row).profile_label
            _require_profile_access(scope, label, "session", session_id)
            if row["status"] != "active":
                raise SessionStateError(
                    f"session is not resumable while status is {row['status']}: {session_id}"
                )
            lease_expires = _load_optional_dt(row["lease_expires_at"])
            if row["lease_token"] is not None and lease_expires is not None and lease_expires > now:
                raise SessionLeaseError(f"session lease is busy: {session_id}")
            fence = int(row["lease_fence"]) + 1
            connection.execute(
                """UPDATE sessions SET lease_owner = ?, lease_token = ?, lease_fence = ?,
                   lease_acquired_at = ?, lease_expires_at = ? WHERE id = ?""",
                (owner, token, fence, _dump_dt(now), _dump_dt(expires), session_id),
            )
        return SessionLease(
            session_id=session_id,
            profile_label=label,
            owner=owner,
            token=token,
            fence=fence,
            acquired_at=now,
            expires_at=expires,
        )

    def _renew(self, lease: SessionLease) -> SessionLease:
        now = _utc(self._clock())
        expires = now + timedelta(seconds=self.settings.lease_seconds)
        with self._transaction() as connection:
            self._required_lease(connection, lease, now)
            connection.execute(
                "UPDATE sessions SET lease_expires_at = ? WHERE id = ?",
                (_dump_dt(expires), lease.session_id),
            )
        return lease.model_copy(update={"expires_at": expires})

    def _begin_turn(self, lease: SessionLease, turn: StoredTurn) -> StoredTurn:
        if turn.session_id != lease.session_id or turn.status != "running":
            raise SessionStateError("running turn does not match the leased session")
        if turn.profile_label != lease.profile_label:
            raise SessionStateError("running turn profile label does not match the lease")
        now = _utc(self._clock())
        with self._transaction() as connection:
            row = self._required_lease(connection, lease, now)
            if int(row["revision"]) != turn.base_revision:
                raise SessionConflictError("turn base revision is stale")
            try:
                connection.execute(
                    """INSERT INTO turns(
                           id, session_id, inbound_ref, base_revision, status,
                           started_at, finished_at, error
                       ) VALUES (?, ?, ?, ?, 'running', ?, NULL, NULL)""",
                    (
                        turn.id,
                        turn.session_id,
                        turn.inbound_ref,
                        turn.base_revision,
                        _dump_dt(turn.started_at),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise SessionConflictError(f"turn already exists: {turn.id}") from exc
        return turn

    def _commit(
        self,
        lease: SessionLease,
        expected_revision: int,
        session: AgentSession,
        turn: StoredTurn,
    ) -> StoredSession:
        if session.id != lease.session_id or turn.session_id != lease.session_id:
            raise SessionStateError("commit payload does not match the leased session")
        if (
            session.profile_scope.label() != lease.profile_label
            or turn.profile_label != lease.profile_label
        ):
            raise SessionStateError("commit payload profile label does not match the lease")
        if turn.base_revision != expected_revision:
            raise SessionConflictError("turn base revision does not match expected revision")
        durable = _durable_session(session)
        now = _utc(self._clock())
        committed_turn = turn.model_copy(
            update={"status": "committed", "finished_at": now, "error": None}
        )
        with self._transaction() as connection:
            row = self._required_lease(connection, lease, now)
            if int(row["revision"]) != expected_revision:
                raise SessionConflictError("session revision changed before commit")
            turn_row = connection.execute("SELECT * FROM turns WHERE id = ?", (turn.id,)).fetchone()
            if turn_row is None:
                self._insert_terminal_turn(connection, committed_turn)
            elif turn_row["status"] != "running":
                raise SessionStateError(f"turn is already {turn_row['status']}: {turn.id}")
            else:
                connection.execute(
                    """UPDATE turns SET status = 'committed', finished_at = ?, error = NULL
                       WHERE id = ?""",
                    (_dump_dt(now), turn.id),
                )
            revision = expected_revision + 1
            connection.execute(
                """UPDATE sessions SET session_json = ?, schema_version = ?, revision = ?,
                   status = 'active', updated_at = ?, last_turn_id = ? WHERE id = ?""",
                (
                    _dump_session(durable),
                    SCHEMA_VERSION,
                    revision,
                    _dump_dt(now),
                    turn.id,
                    durable.id,
                ),
            )
            self._trim_turns(connection, durable.id)
            created_at = _load_dt(row["created_at"])
        return StoredSession(
            session=durable,
            profile_label=lease.profile_label,
            revision=revision,
            status="active",
            created_at=created_at,
            updated_at=now,
            last_turn_id=turn.id,
        )

    def _stale_leases(
        self,
        scope: ProfileScope,
        now: datetime | None,
    ) -> list[StaleSessionLease]:
        moment = _utc(now or self._clock())
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, lease_owner, lease_fence, lease_expires_at FROM sessions
                   WHERE lease_token IS NOT NULL AND lease_expires_at IS NOT NULL
                     AND lease_expires_at <= ?
                   ORDER BY id""",
                (_dump_dt(moment),),
            ).fetchall()
            leases: list[StaleSessionLease] = []
            for row in rows:
                stored = self._stored_session(self._required_session(connection, row["id"]))
                label = stored.profile_label
                if not scope.permits(label):
                    continue
                turn_rows = connection.execute(
                    """SELECT id FROM turns WHERE session_id = ? AND status = 'running'
                       ORDER BY started_at, id LIMIT 100""",
                    (row["id"],),
                ).fetchall()
                leases.append(
                    StaleSessionLease(
                        session_id=cast(str, row["id"]),
                        profile_label=label,
                        owner=cast(str, row["lease_owner"]) or "unknown",
                        fence=int(row["lease_fence"]),
                        expired_at=_utc(_load_dt(cast(str, row["lease_expires_at"]))),
                        running_turn_ids=[cast(str, item["id"]) for item in turn_rows],
                    )
                )
        return leases

    def _recover_expired_lease(
        self,
        session_id: str,
        scope: ProfileScope,
        error: str,
        now: datetime | None,
    ) -> StaleSessionLease:
        moment = _utc(now or self._clock())
        with self._transaction() as connection:
            row = self._required_session(connection, session_id)
            label = self._stored_session(row).profile_label
            _require_profile_access(scope, label, "session", session_id)
            expires = _load_optional_dt(row["lease_expires_at"])
            if row["lease_token"] is None or expires is None:
                raise SessionLeaseError(f"session holds no lease: {session_id}")
            if expires > moment:
                raise SessionLeaseError(f"session lease has not expired: {session_id}")
            turn_rows = connection.execute(
                """SELECT id FROM turns WHERE session_id = ? AND status = 'running'
                   ORDER BY started_at, id LIMIT 100""",
                (session_id,),
            ).fetchall()
            turn_ids = [cast(str, item["id"]) for item in turn_rows]
            for turn_id in turn_ids:
                connection.execute(
                    """UPDATE turns SET status = 'uncertain', finished_at = ?, error = ?
                       WHERE id = ? AND status = 'running'""",
                    (_dump_dt(moment), error[:16_000], turn_id),
                )
            session_status = "uncertain" if turn_ids else row["status"]
            connection.execute(
                """UPDATE sessions SET status = ?, updated_at = ?, lease_owner = NULL,
                   lease_token = NULL, lease_fence = ?, lease_acquired_at = NULL,
                   lease_expires_at = NULL WHERE id = ?""",
                (session_status, _dump_dt(moment), int(row["lease_fence"]) + 1, session_id),
            )
        return StaleSessionLease(
            session_id=session_id,
            profile_label=label,
            owner=cast(str, row["lease_owner"]) or "unknown",
            fence=int(row["lease_fence"]),
            expired_at=_utc(expires),
            running_turn_ids=turn_ids,
        )

    def _session_counts(self, scope: ProfileScope) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM sessions").fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            stored = self._stored_session(row)
            if scope.permits(stored.profile_label):
                counts[stored.status] = counts.get(stored.status, 0) + 1
        return counts

    def _fail_turn(
        self,
        lease: SessionLease,
        turn_id: str,
        error: str,
        uncertain: bool,
    ) -> StoredTurn:
        now = _utc(self._clock())
        status = "uncertain" if uncertain else "failed"
        with self._transaction() as connection:
            row = self._required_lease(connection, lease, now)
            turn_row = connection.execute(
                "SELECT * FROM turns WHERE id = ? AND session_id = ?",
                (turn_id, lease.session_id),
            ).fetchone()
            if turn_row is None:
                raise SessionNotFoundError(f"turn not found: {turn_id}")
            if turn_row["status"] != "running":
                raise SessionStateError(f"turn is already {turn_row['status']}: {turn_id}")
            connection.execute(
                "UPDATE turns SET status = ?, finished_at = ?, error = ? WHERE id = ?",
                (status, _dump_dt(now), error, turn_id),
            )
            if uncertain:
                connection.execute(
                    "UPDATE sessions SET status = 'uncertain', updated_at = ? WHERE id = ?",
                    (_dump_dt(now), lease.session_id),
                )
            self._trim_turns(connection, lease.session_id)
            result = StoredTurn(
                id=cast(str, turn_row["id"]),
                session_id=cast(str, turn_row["session_id"]),
                profile_label=lease.profile_label,
                inbound_ref=cast(str | None, turn_row["inbound_ref"]),
                base_revision=int(turn_row["base_revision"]),
                status=cast(Any, status),
                started_at=_load_dt(turn_row["started_at"]),
                finished_at=now,
                error=error,
            )
            _ = row
        return result

    def _release(self, lease: SessionLease) -> None:
        with self._transaction() as connection:
            row = self._required_session(connection, lease.session_id)
            if self._stored_session(row).profile_label != lease.profile_label:
                raise SessionLeaseError("session lease profile label is stale or foreign")
            if row["lease_token"] != lease.token or int(row["lease_fence"]) != lease.fence:
                raise SessionLeaseError("session lease is stale")
            connection.execute(
                """UPDATE sessions SET lease_owner = NULL, lease_token = NULL,
                   lease_acquired_at = NULL, lease_expires_at = NULL WHERE id = ?""",
                (lease.session_id,),
            )

    def _archive(
        self,
        session_id: str,
        scope: ProfileScope,
        expected_revision: int,
    ) -> StoredSession:
        now = _utc(self._clock())
        with self._transaction() as connection:
            row = self._required_session(connection, session_id)
            _require_profile_access(
                scope,
                self._stored_session(row).profile_label,
                "session",
                session_id,
            )
            if int(row["revision"]) != expected_revision:
                raise SessionConflictError("session revision changed before archive")
            lease_expires = _load_optional_dt(row["lease_expires_at"])
            if row["lease_token"] is not None and lease_expires is not None and lease_expires > now:
                raise SessionLeaseError("cannot archive a session with a live lease")
            if row["status"] == "archived":
                raise SessionStateError(f"session is already archived: {session_id}")
            revision = expected_revision + 1
            connection.execute(
                """UPDATE sessions SET status = 'archived', revision = ?, updated_at = ?,
                   lease_owner = NULL, lease_token = NULL, lease_acquired_at = NULL,
                   lease_expires_at = NULL WHERE id = ?""",
                (revision, _dump_dt(now), session_id),
            )
            stored = self._stored_session(row).model_copy(
                update={"revision": revision, "status": "archived", "updated_at": now}
            )
        return stored

    def _turns(
        self,
        session_id: str,
        scope: ProfileScope,
        limit: int,
    ) -> list[StoredTurn]:
        with self._connect() as connection:
            session = self._stored_session(self._required_session(connection, session_id))
            _require_profile_access(scope, session.profile_label, "session", session_id)
            rows = connection.execute(
                """SELECT * FROM turns WHERE session_id = ?
                   ORDER BY started_at DESC, id DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
            return [self._stored_turn(row, session.profile_label) for row in rows]

    def _stored_session(self, row: sqlite3.Row) -> StoredSession:
        if int(row["schema_version"]) != SCHEMA_VERSION:
            raise SessionSchemaError(
                f"unsupported stored session schema version: {row['schema_version']}"
            )
        session = _load_session(cast(str, row["session_json"]))
        if session.id != row["id"]:
            raise SessionSchemaError("stored session id does not match its database key")
        try:
            return StoredSession(
                session=session,
                profile_label=session.profile_scope.label(),
                revision=int(row["revision"]),
                status=cast(Any, row["status"]),
                created_at=_load_dt(row["created_at"]),
                updated_at=_load_dt(row["updated_at"]),
                last_turn_id=cast(str | None, row["last_turn_id"]),
            )
        except ValidationError as exc:
            raise SessionSchemaError("stored session metadata is malformed") from exc

    def _stored_turn(self, row: sqlite3.Row, profile_label: ProfileLabel) -> StoredTurn:
        try:
            return StoredTurn(
                id=cast(str, row["id"]),
                session_id=cast(str, row["session_id"]),
                profile_label=profile_label,
                inbound_ref=cast(str | None, row["inbound_ref"]),
                base_revision=int(row["base_revision"]),
                status=cast(Any, row["status"]),
                started_at=_load_dt(row["started_at"]),
                finished_at=_load_optional_dt(row["finished_at"]),
                error=cast(str | None, row["error"]),
            )
        except ValidationError as exc:
            raise SessionSchemaError("stored turn is malformed") from exc

    def _required_session(self, connection: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise SessionNotFoundError(f"session not found: {session_id}")
        return row

    def _required_lease(
        self,
        connection: sqlite3.Connection,
        lease: SessionLease,
        now: datetime,
    ) -> sqlite3.Row:
        row = self._required_session(connection, lease.session_id)
        label = self._stored_session(row).profile_label
        if label != lease.profile_label:
            raise SessionLeaseError("session lease profile label is stale or foreign")
        if (
            row["lease_token"] != lease.token
            or int(row["lease_fence"]) != lease.fence
            or row["lease_owner"] != lease.owner
        ):
            raise SessionLeaseError("session lease is stale or foreign")
        expires = _load_optional_dt(row["lease_expires_at"])
        if expires is None or expires <= now:
            raise SessionLeaseError("session lease has expired")
        return row

    def _insert_terminal_turn(self, connection: sqlite3.Connection, turn: StoredTurn) -> None:
        connection.execute(
            """INSERT INTO turns(
                   id, session_id, inbound_ref, base_revision, status,
                   started_at, finished_at, error
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                turn.id,
                turn.session_id,
                turn.inbound_ref,
                turn.base_revision,
                turn.status,
                _dump_dt(turn.started_at),
                _dump_dt(cast(datetime, turn.finished_at)),
                turn.error,
            ),
        )

    def _trim_turns(self, connection: sqlite3.Connection, session_id: str) -> None:
        connection.execute(
            """DELETE FROM turns WHERE session_id = ? AND id IN (
                   SELECT id FROM turns WHERE session_id = ?
                   ORDER BY started_at DESC, id DESC LIMIT -1 OFFSET ?
               )""",
            (session_id, session_id, self.settings.turn_retention),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=self.settings.sqlite_busy_timeout_ms / 1_000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.settings.sqlite_busy_timeout_ms}")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def _durable_session(session: AgentSession) -> AgentSession:
    durable = session.model_copy(deep=True)
    durable.permission_grants.clear()
    durable.active_task_leases.clear()
    return durable


def _dump_session(session: AgentSession) -> str:
    return json.dumps(
        session.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _load_session(payload: str) -> AgentSession:
    try:
        raw = json.loads(payload)
        if not isinstance(raw, dict):
            raise ValueError("session JSON must be an object")
        session = AgentSession.model_validate(raw)
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
        raise SessionSchemaError("stored AgentSession JSON is malformed") from exc
    if session.model_dump(mode="json") != raw:
        raise SessionSchemaError("stored AgentSession JSON is non-canonical or has unknown fields")
    return session


def _session_id(value: str) -> str:
    if not value or len(value) > 128:
        raise ValueError("session id must contain 1 to 128 characters")
    return value


def _turn_id(value: str) -> str:
    if not value or len(value) > 128:
        raise ValueError("turn id must contain 1 to 128 characters")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("store clock must return an aware datetime")
    return value.astimezone(UTC)


def _dump_dt(value: datetime) -> str:
    return _utc(value).isoformat()


def _load_dt(value: str) -> datetime:
    try:
        return _utc(datetime.fromisoformat(value))
    except (TypeError, ValueError) as exc:
        raise SessionSchemaError("stored timestamp is malformed") from exc


def _load_optional_dt(value: str | None) -> datetime | None:
    return None if value is None else _load_dt(value)


def _require_profile_access(
    scope: ProfileScope,
    label: ProfileLabel,
    kind: str,
    identifier: str,
) -> None:
    if not scope.permits(label):
        raise SessionNotFoundError(f"{kind} not found: {identifier}")
