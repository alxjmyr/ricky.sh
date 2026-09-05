"""Transactional SQLite notification outbox with fenced delivery claims."""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import ValidationError

from ricky.config import RickySettings, user_data_path, user_data_subpath
from ricky.notifications.types import (
    DeliveryAttempt,
    NotificationRecord,
    NotificationRequest,
    OperatorResolution,
    OutboxEntry,
    OutboxStatus,
    ResolutionDisposition,
)
from ricky.profiles import ProfileLabel, ProfileScope

SCHEMA_VERSION = 1


class NotificationStoreError(RuntimeError):
    """Base store error safe to surface to an operator."""


class NotificationNotFoundError(NotificationStoreError):
    """A notification or outbox identity does not exist."""


class NotificationStateError(NotificationStoreError):
    """A requested outbox state transition is invalid."""


class NotificationLeaseError(NotificationStoreError):
    """A delivery lease is missing, expired, foreign, or stale."""


class NotificationSchemaError(NotificationStoreError):
    """Stored schema or canonical request JSON is invalid."""


class _LeaseExpired(RuntimeError):
    """Internal signal used to commit conservative expiry in a new transaction."""


@runtime_checkable
class OutboxDeliveryStore(Protocol):
    """Narrow protocol consumed by a future platform delivery worker."""

    async def list(
        self,
        *,
        scope: ProfileScope,
        status: OutboxStatus | None = None,
        limit: int = 50,
    ) -> list[NotificationRecord]: ...

    async def claim(
        self,
        outbox_id: str,
        *,
        scope: ProfileScope,
        worker: str,
        transport: str,
        destination_ref: str,
        lease_seconds: int | None = None,
    ) -> OutboxEntry: ...

    async def renew(self, entry: OutboxEntry, *, scope: ProfileScope) -> OutboxEntry: ...

    async def mark_delivered(
        self,
        entry: OutboxEntry,
        *,
        scope: ProfileScope,
        platform_message_id: str,
    ) -> OutboxEntry: ...

    async def mark_failed(
        self, entry: OutboxEntry, *, scope: ProfileScope, error: str
    ) -> OutboxEntry: ...

    async def mark_in_doubt(
        self, entry: OutboxEntry, *, scope: ProfileScope, error: str
    ) -> OutboxEntry: ...

    async def release(self, entry: OutboxEntry, *, scope: ProfileScope) -> OutboxEntry: ...


class NotificationStore:
    """Async-first API over short, cancellation-safe SQLite transactions."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings.messaging
        self.user_data_root = user_data_path(settings)
        self.db_path = user_data_subpath(settings, self.settings.store_path)
        self.root: Path = self.db_path.parent
        self._clock = clock or (lambda: datetime.now(UTC))

    async def initialize(self) -> None:
        await self._run(self._initialize)

    async def enqueue(
        self,
        request: NotificationRequest,
        *,
        scope: ProfileScope,
    ) -> NotificationRecord:
        _require_scope(scope, request.profile_label)
        return await self._run(self._enqueue, request)

    async def get(self, notification_id: str, *, scope: ProfileScope) -> NotificationRecord:
        return await self._run(
            self._get,
            _identity(notification_id, "notification id"),
            scope,
        )

    async def get_outbox(self, outbox_id: str, *, scope: ProfileScope) -> OutboxEntry:
        return await self._run(
            self._get_outbox,
            _identity(outbox_id, "outbox id"),
            scope,
        )

    async def get_by_outbox(
        self,
        outbox_id: str,
        *,
        scope: ProfileScope,
    ) -> NotificationRecord:
        """Load the immutable notification joined to one trusted outbox id."""

        return await self._run(
            self._get_by_outbox,
            _identity(outbox_id, "outbox id"),
            scope,
        )

    async def list(
        self,
        *,
        scope: ProfileScope,
        status: OutboxStatus | None = None,
        limit: int = 50,
    ) -> list[NotificationRecord]:
        if limit < 1 or limit > 1_000:
            raise ValueError("notification list limit must be between 1 and 1000")
        return await self._run(self._list, status, limit, scope)

    async def list_pending_oldest(
        self,
        *,
        scope: ProfileScope,
        limit: int = 50,
    ) -> list[NotificationRecord]:
        """Return the globally oldest pending notifications for fair delivery."""

        if limit < 1 or limit > 1_000:
            raise ValueError("notification list limit must be between 1 and 1000")
        return await self._run(self._list_pending_oldest, limit, scope)

    async def source_ids(self, *, source_kind: str, scope: ProfileScope) -> set[str]:
        """Return every durable source id, including cancelled notifications."""

        kind = _bounded_text(source_kind, "source_kind", 100)
        return await self._run(self._source_ids, kind, scope)

    async def unresolved_outbox_ids(self, *, scope: ProfileScope) -> tuple[str, ...]:
        """Return all outbox ids whose delivery evidence is not disposable."""

        return await self._run(self._unresolved_outbox_ids, scope)

    async def unresolved_conversation_ids(self, *, scope: ProfileScope) -> tuple[str, ...]:
        """Return conversations referenced by unresolved notification evidence."""

        return await self._run(self._unresolved_conversation_ids, scope)

    async def claim(
        self,
        outbox_id: str,
        *,
        scope: ProfileScope,
        worker: str,
        transport: str,
        destination_ref: str,
        lease_seconds: int | None = None,
    ) -> OutboxEntry:
        duration = lease_seconds or self.settings.lease_seconds
        if duration < 1 or duration > 3_600:
            raise ValueError("notification lease_seconds must be between 1 and 3600")
        worker = _bounded_text(worker, "worker", 200)
        transport = _bounded_text(transport, "transport", 100)
        destination_ref = _bounded_text(destination_ref, "destination_ref", 500)
        return await self._run(
            self._claim,
            _identity(outbox_id, "outbox id"),
            worker,
            transport,
            destination_ref,
            duration,
            scope,
        )

    async def renew(self, entry: OutboxEntry, *, scope: ProfileScope) -> OutboxEntry:
        return await self._run(self._renew, entry, scope)

    async def mark_delivered(
        self,
        entry: OutboxEntry,
        *,
        scope: ProfileScope,
        platform_message_id: str,
    ) -> OutboxEntry:
        message_id = _bounded_text(platform_message_id, "platform_message_id", 500)
        return await self._run(self._finish_claim, entry, "delivered", None, message_id, scope)

    async def mark_failed(
        self,
        entry: OutboxEntry,
        *,
        scope: ProfileScope,
        error: str,
    ) -> OutboxEntry:
        safe_error = _bounded_text(error, "delivery error", 2_000)
        return await self._run(self._finish_claim, entry, "failed", safe_error, None, scope)

    async def mark_in_doubt(
        self,
        entry: OutboxEntry,
        *,
        scope: ProfileScope,
        error: str,
    ) -> OutboxEntry:
        safe_error = _bounded_text(error, "delivery error", 2_000)
        return await self._run(self._finish_claim, entry, "in_doubt", safe_error, None, scope)

    async def release(self, entry: OutboxEntry, *, scope: ProfileScope) -> OutboxEntry:
        """Release before any send is attempted; this transition is replay-safe."""

        return await self._run(self._finish_claim, entry, "pending", None, None, scope)

    async def fail_pending(
        self,
        outbox_id: str,
        *,
        scope: ProfileScope,
        error: str,
    ) -> OutboxEntry:
        """Quarantine a known pre-send failure without creating an attempt."""

        return await self._run(
            self._fail_pending,
            _identity(outbox_id, "outbox id"),
            _bounded_text(error, "delivery error", 2_000),
            scope,
        )

    async def retry(self, outbox_id: str, *, scope: ProfileScope) -> OutboxEntry:
        return await self._run(
            self._retry,
            _identity(outbox_id, "outbox id"),
            scope,
        )

    async def resolve(
        self,
        outbox_id: str,
        *,
        scope: ProfileScope,
        disposition: ResolutionDisposition,
        actor: str,
        note: str | None = None,
    ) -> OutboxEntry:
        actor = _bounded_text(actor, "actor", 200)
        safe_note = None if note is None else _bounded_text(note, "resolution note", 2_000)
        return await self._run(
            self._resolve,
            _identity(outbox_id, "outbox id"),
            disposition,
            actor,
            safe_note,
            scope,
        )

    async def cancel(self, outbox_id: str, *, scope: ProfileScope) -> OutboxEntry:
        return await self._run(
            self._cancel,
            _identity(outbox_id, "outbox id"),
            scope,
        )

    async def attempts(self, outbox_id: str, *, scope: ProfileScope) -> list[DeliveryAttempt]:
        return await self._run(
            self._attempts,
            _identity(outbox_id, "outbox id"),
            scope,
        )

    async def resolutions(self, outbox_id: str, *, scope: ProfileScope) -> list[OperatorResolution]:
        return await self._run(
            self._resolutions,
            _identity(outbox_id, "outbox id"),
            scope,
        )

    async def stale_claims(
        self,
        *,
        scope: ProfileScope,
        now: datetime | None = None,
    ) -> list[OutboxEntry]:
        """List claimed outbox entries whose delivery worker lease has expired."""

        return await self._run(self._stale_claims, now, scope)

    async def recover_claim(
        self,
        outbox_id: str,
        *,
        scope: ProfileScope,
        disposition: Literal["pending", "in_doubt"],
        error: str,
        now: datetime | None = None,
    ) -> OutboxEntry:
        """Resolve one expired delivery claim so a stale worker can never commit.

        ``pending`` is legal only when the caller proved no transport part was
        ever prepared for the claimed fence. Otherwise the send is ambiguous and
        the entry becomes ``in_doubt`` for operator reconciliation.
        """

        return await self._run(
            self._recover_claim,
            _identity(outbox_id, "outbox id"),
            disposition,
            _bounded_text(error, "recovery error", 2_000),
            now,
            scope,
        )

    async def outbox_counts(self, *, scope: ProfileScope) -> dict[str, int]:
        """Count outbox entries by status without loading any notification body."""

        return await self._run(self._outbox_counts, scope)

    async def oldest_pending_outbox(self, *, scope: ProfileScope) -> datetime | None:
        """Return the creation time of the oldest pending outbox entry."""

        return await self._run(self._oldest_pending_outbox, scope)

    async def prunable_notifications(
        self,
        *,
        scope: ProfileScope,
        keep: int,
        before: datetime,
        protected: Sequence[str] = (),
    ) -> list[str]:
        """List terminal outbox ids that exceed the retention ceiling."""

        return await self._run(
            self._prunable_notifications,
            keep,
            before,
            tuple(protected),
            scope,
        )

    async def prune_notifications(
        self,
        outbox_ids: Sequence[str],
        *,
        scope: ProfileScope,
    ) -> int:
        """Delete exactly these terminal notifications, attempts, and resolutions."""

        return await self._run(self._prune_notifications, tuple(outbox_ids), scope)

    async def _run(self, operation: Callable[..., Any], *args: Any) -> Any:
        try:
            task = asyncio.create_task(asyncio.to_thread(operation, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
        except NotificationStoreError:
            raise
        except sqlite3.Error as exc:
            raise NotificationStoreError("notification store operation failed") from exc

    def _initialize(self) -> None:
        from ricky.notifications.upgrade import (
            NotificationsUpgradeError,
            create_current_notifications_store,
            inspect_notifications_store,
        )

        inspection = inspect_notifications_store(self.db_path)
        if inspection.state != "current":
            if inspection.state != "absent":
                raise NotificationSchemaError(inspection.detail)
            try:
                create_current_notifications_store(self.db_path)
            except NotificationsUpgradeError as exc:
                raise NotificationSchemaError(str(exc)) from exc
        # The outbox holds message bodies and platform destinations as private
        # evidence, so every open re-asserts the private modes instead of
        # trusting the creation path.
        os.chmod(self.root, 0o700)
        os.chmod(self.db_path, 0o600)
        for path in (Path(f"{self.db_path}-wal"), Path(f"{self.db_path}-shm")):
            # SQLite removes transient sidecars when the last connection
            # closes. Their disappearance during mode repair is benign.
            with suppress(FileNotFoundError):
                os.chmod(path, 0o600)

    def _enqueue(self, request: NotificationRequest) -> NotificationRecord:
        with self._connect() as connection, self._transaction(connection):
            existing = connection.execute(
                """
                SELECT notification_id FROM outbox
                WHERE source_kind = ? AND source_id = ? AND dedupe_key = ? AND route = ?
                ORDER BY created_at ASC, id ASC LIMIT 1
                """,
                (request.source_kind, request.source_id, request.dedupe_key, request.route),
            ).fetchone()
            if existing is not None:
                return self._record_by_notification(connection, existing["notification_id"])
            now = _iso(request.created_at)
            outbox_id = f"outbox_{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO notifications(
                    id, schema_version, request_json, source_kind, source_id,
                    dedupe_key, route, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.id,
                    SCHEMA_VERSION,
                    request.model_dump_json(),
                    request.source_kind,
                    request.source_id,
                    request.dedupe_key,
                    request.route,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO outbox(
                    id, notification_id, route, source_kind, source_id, dedupe_key,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    outbox_id,
                    request.id,
                    request.route,
                    request.source_kind,
                    request.source_id,
                    request.dedupe_key,
                    now,
                    now,
                ),
            )
            return self._record_by_notification(connection, request.id)

    def _get(self, notification_id: str, scope: ProfileScope) -> NotificationRecord:
        with self._connect() as connection:
            record = self._record_by_notification(connection, notification_id)
            self._assert_record_scope(record, scope)
            return record

    def _get_outbox(self, outbox_id: str, scope: ProfileScope) -> OutboxEntry:
        with self._connect() as connection:
            self._assert_outbox_scope(connection, outbox_id, scope)
            return self._entry_by_id(connection, outbox_id)

    def _get_by_outbox(self, outbox_id: str, scope: ProfileScope) -> NotificationRecord:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT n.request_json, n.schema_version, o.*
                   FROM notifications n JOIN outbox o ON o.notification_id = n.id
                   WHERE o.id = ?""",
                (outbox_id,),
            ).fetchone()
            if row is None:
                raise NotificationNotFoundError("outbox entry was not found")
            record = self._record_from_join(row)
            self._assert_record_scope(record, scope)
            return record

    def _list(
        self,
        status: OutboxStatus | None,
        limit: int,
        scope: ProfileScope,
    ) -> list[NotificationRecord]:
        with self._connect() as connection:
            query = """
                SELECT n.request_json, n.schema_version, o.*
                FROM outbox o JOIN notifications n ON n.id = o.notification_id
            """
            params: tuple[object, ...]
            if status is None:
                query += " ORDER BY o.created_at DESC, o.id DESC"
                params = ()
            else:
                query += " WHERE o.status = ? ORDER BY o.created_at DESC, o.id DESC"
                params = (status,)
            records = (self._record_from_join(row) for row in connection.execute(query, params))
            return [record for record in records if scope.permits(record.request.profile_label)][
                :limit
            ]

    def _list_pending_oldest(
        self,
        limit: int,
        scope: ProfileScope,
    ) -> list[NotificationRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT n.request_json, n.schema_version, o.*
                FROM outbox o JOIN notifications n ON n.id = o.notification_id
                WHERE o.status = 'pending'
                ORDER BY o.created_at ASC, o.id ASC
                """
            ).fetchall()
        records = (self._record_from_join(row) for row in rows)
        return [record for record in records if scope.permits(record.request.profile_label)][:limit]

    def _source_ids(self, source_kind: str, scope: ProfileScope) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT source_id, request_json FROM notifications WHERE source_kind = ?",
                (source_kind,),
            ).fetchall()
        return {
            str(row["source_id"])
            for row in rows
            if scope.permits(self._request(row["request_json"]).profile_label)
        }

    def _unresolved_outbox_ids(self, scope: ProfileScope) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT o.id, n.request_json FROM outbox o
                JOIN notifications n ON n.id = o.notification_id
                WHERE o.status IN ('pending','claimed','failed','in_doubt')
                ORDER BY o.id ASC
                """
            ).fetchall()
        return tuple(
            str(row["id"])
            for row in rows
            if scope.permits(self._request(row["request_json"]).profile_label)
        )

    def _unresolved_conversation_ids(self, scope: ProfileScope) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT n.request_json
                FROM outbox o JOIN notifications n ON n.id = o.notification_id
                WHERE o.status IN ('pending','claimed','failed','in_doubt')
                """
            ).fetchall()
        protected: set[str] = set()
        for row in rows:
            request = self._request(row["request_json"])
            if not scope.permits(request.profile_label):
                continue
            if request.route.startswith("conversation:"):
                protected.add(request.route.removeprefix("conversation:"))
            protected.update(
                correlation.id
                for correlation in request.correlations
                if correlation.kind == "conversation"
            )
        return tuple(sorted(protected))

    def _claim(
        self,
        outbox_id: str,
        worker: str,
        transport: str,
        destination_ref: str,
        duration: int,
        scope: ProfileScope,
    ) -> OutboxEntry:
        now = self._now()
        expired = False
        result: OutboxEntry | None = None
        with self._connect() as connection, self._transaction(connection):
            self._assert_outbox_scope(connection, outbox_id, scope)
            row = self._outbox_row(connection, outbox_id)
            if row["status"] == "claimed":
                expires_at = _datetime(row["lease_expires_at"])
                if expires_at > now:
                    raise NotificationLeaseError("notification delivery is already claimed")
                self._expire_claim(connection, row, now)
                expired = True
            elif row["status"] not in {"pending", "failed"}:
                raise NotificationStateError(f"cannot claim outbox entry in {row['status']} state")
            elif row["status"] == "failed":
                raise NotificationStateError("failed delivery requires an explicit retry command")
            elif int(row["attempt_count"]) >= self.settings.delivery_attempt_limit:
                raise NotificationStateError("delivery attempt limit is exhausted")
            else:
                fence = int(row["fence"]) + 1
                attempt_count = int(row["attempt_count"]) + 1
                prior = connection.execute(
                    "SELECT MAX(attempt_number) FROM delivery_attempts WHERE outbox_id = ?",
                    (outbox_id,),
                ).fetchone()
                attempt_number = int(prior[0] or 0) + 1
                token = uuid4().hex
                expires_at = now + timedelta(seconds=duration)
                connection.execute(
                    """
                    UPDATE outbox SET status = 'claimed', attempt_count = ?, fence = ?,
                        lease_owner = ?, lease_token = ?, lease_expires_at = ?,
                        transport = ?, destination_ref = ?, error = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        attempt_count,
                        fence,
                        worker,
                        token,
                        _iso(expires_at),
                        transport,
                        destination_ref,
                        _iso(now),
                        outbox_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO delivery_attempts(
                        outbox_id, attempt_number, fence, worker, transport,
                        destination_ref, outcome, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?)
                    """,
                    (
                        outbox_id,
                        attempt_number,
                        fence,
                        worker,
                        transport,
                        destination_ref,
                        _iso(now),
                    ),
                )
                result = self._entry_by_id(connection, outbox_id)
        if expired:
            raise NotificationStateError(
                "expired delivery claim is in_doubt and requires operator resolution"
            )
        assert result is not None
        return result

    def _renew(self, entry: OutboxEntry, scope: ProfileScope) -> OutboxEntry:
        now = self._now()
        try:
            with self._connect() as connection, self._transaction(connection):
                self._assert_outbox_scope(connection, entry.id, scope)
                self._assert_lease(connection, entry, now)
                expires_at = now + timedelta(seconds=self.settings.lease_seconds)
                connection.execute(
                    "UPDATE outbox SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
                    (_iso(expires_at), _iso(now), entry.id),
                )
                return self._entry_by_id(connection, entry.id)
        except _LeaseExpired:
            self._persist_expired_claim(entry, now)
            raise NotificationLeaseError("notification delivery lease expired") from None

    def _finish_claim(
        self,
        entry: OutboxEntry,
        target: LiteralFinishState,
        error: str | None,
        platform_message_id: str | None,
        scope: ProfileScope,
    ) -> OutboxEntry:
        now = self._now()
        try:
            with self._connect() as connection, self._transaction(connection):
                self._assert_outbox_scope(connection, entry.id, scope)
                row = self._assert_lease(connection, entry, now)
                attempt_outcome = {
                    "delivered": "delivered",
                    "failed": "not_performed",
                    "in_doubt": "in_doubt",
                    "pending": "released",
                }[target]
                delivered_at = _iso(now) if target == "delivered" else None
                connection.execute(
                    """
                    UPDATE outbox SET status = ?, attempt_count = ?, lease_owner = NULL,
                        lease_token = NULL, lease_expires_at = NULL,
                        platform_message_id = ?, error = ?, updated_at = ?, delivered_at = ?
                    WHERE id = ?
                    """,
                    (
                        target,
                        int(row["attempt_count"]) - 1
                        if target == "pending"
                        else int(row["attempt_count"]),
                        platform_message_id,
                        error,
                        _iso(now),
                        delivered_at,
                        entry.id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE delivery_attempts SET outcome = ?, error = ?, finished_at = ?
                    WHERE outbox_id = ? AND fence = ?
                      AND outcome = 'claimed'
                    """,
                    (
                        attempt_outcome,
                        error,
                        _iso(now),
                        entry.id,
                        entry.fence,
                    ),
                )
                return self._entry_by_id(connection, entry.id)
        except _LeaseExpired:
            self._persist_expired_claim(entry, now)
            raise NotificationLeaseError("notification delivery lease expired") from None

    def _stale_claims(
        self,
        now: datetime | None,
        scope: ProfileScope,
    ) -> list[OutboxEntry]:
        moment = now or self._now()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT n.request_json, o.* FROM outbox o
                JOIN notifications n ON n.id = o.notification_id
                WHERE o.status = 'claimed' AND o.lease_expires_at <= ?
                ORDER BY o.created_at, o.id
                """,
                (_iso(moment),),
            ).fetchall()
        return [
            self._entry_from_row(row)
            for row in rows
            if scope.permits(self._request(row["request_json"]).profile_label)
        ]

    def _recover_claim(
        self,
        outbox_id: str,
        disposition: Literal["pending", "in_doubt"],
        error: str,
        now: datetime | None,
        scope: ProfileScope,
    ) -> OutboxEntry:
        moment = now or self._now()
        with self._connect() as connection, self._transaction(connection):
            self._assert_outbox_scope(connection, outbox_id, scope)
            row = self._outbox_row(connection, outbox_id)
            if row["status"] != "claimed":
                raise NotificationStateError(
                    f"cannot recover outbox entry in {row['status']} state"
                )
            if _datetime(row["lease_expires_at"]) > moment:
                raise NotificationLeaseError("delivery claim has not expired")
            attempt_outcome = "released" if disposition == "pending" else "in_doubt"
            stored_error = None if disposition == "pending" else error
            connection.execute(
                """
                UPDATE outbox SET status = ?, attempt_count = ?, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    disposition,
                    int(row["attempt_count"]) - 1
                    if disposition == "pending"
                    else int(row["attempt_count"]),
                    stored_error,
                    _iso(moment),
                    outbox_id,
                ),
            )
            connection.execute(
                """
                UPDATE delivery_attempts SET outcome = ?, error = ?, finished_at = ?
                WHERE outbox_id = ? AND fence = ? AND outcome = 'claimed'
                """,
                (
                    attempt_outcome,
                    stored_error,
                    _iso(moment),
                    outbox_id,
                    int(row["fence"]),
                ),
            )
            return self._entry_by_id(connection, outbox_id)

    def _outbox_counts(self, scope: ProfileScope) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT o.status, n.request_json FROM outbox o
                   JOIN notifications n ON n.id = o.notification_id"""
            ).fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            if not scope.permits(self._request(row["request_json"]).profile_label):
                continue
            status = str(row["status"])
            counts[status] = counts.get(status, 0) + 1
        return counts

    def _oldest_pending_outbox(self, scope: ProfileScope) -> datetime | None:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT o.created_at, n.request_json FROM outbox o
                   JOIN notifications n ON n.id = o.notification_id
                   WHERE o.status = 'pending' ORDER BY o.created_at, o.id"""
            ).fetchall()
        for row in rows:
            if scope.permits(self._request(row["request_json"]).profile_label):
                return _datetime(row["created_at"])
        return None

    def _prunable_notifications(
        self,
        keep: int,
        before: datetime,
        protected: tuple[str, ...],
        scope: ProfileScope,
    ) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT o.id, n.request_json FROM outbox o
                JOIN notifications n ON n.id = o.notification_id
                WHERE o.status IN ('delivered','cancelled') AND o.created_at < ?
                ORDER BY o.created_at DESC, o.id DESC
                """,
                (_iso(before),),
            ).fetchall()
        blocked = set(protected)
        candidates = [
            str(row["id"])
            for row in rows
            if str(row["id"]) not in blocked
            and scope.permits(self._request(row["request_json"]).profile_label)
        ]
        return sorted(candidates[keep:])

    def _prune_notifications(
        self,
        outbox_ids: tuple[str, ...],
        scope: ProfileScope,
    ) -> int:
        if not outbox_ids:
            return 0
        removed = 0
        notification_ids: list[str] = []
        with self._connect() as connection, self._transaction(connection):
            for outbox_id in outbox_ids:
                row = connection.execute(
                    """SELECT o.status, o.notification_id, n.request_json
                       FROM outbox o JOIN notifications n ON n.id = o.notification_id
                       WHERE o.id = ?""",
                    (outbox_id,),
                ).fetchone()
                if row is None or row["status"] not in {"delivered", "cancelled"}:
                    continue
                if not scope.permits(self._request(row["request_json"]).profile_label):
                    raise NotificationNotFoundError(
                        "outbox entry was not found in the active profile scope"
                    )
                connection.execute(
                    "DELETE FROM delivery_attempts WHERE outbox_id = ?", (outbox_id,)
                )
                connection.execute(
                    "DELETE FROM operator_resolutions WHERE outbox_id = ?", (outbox_id,)
                )
                connection.execute("DELETE FROM outbox WHERE id = ?", (outbox_id,))
                connection.execute(
                    "DELETE FROM notifications WHERE id = ?", (row["notification_id"],)
                )
                notification_ids.append(str(row["notification_id"]))
                removed += 1
        attachment_root = (self.user_data_root / self.settings.attachment_dir).resolve()
        if not attachment_root.is_relative_to(self.user_data_root.resolve()):
            raise NotificationStoreError("messaging attachment directory escapes user_data_dir")
        for notification_id in notification_ids:
            shutil.rmtree(attachment_root / notification_id, ignore_errors=True)
        return removed

    def _retry(self, outbox_id: str, scope: ProfileScope) -> OutboxEntry:
        now = self._now()
        with self._connect() as connection, self._transaction(connection):
            self._assert_outbox_scope(connection, outbox_id, scope)
            row = self._outbox_row(connection, outbox_id)
            if row["status"] == "in_doubt":
                raise NotificationStateError(
                    "in_doubt delivery must be resolved before it can be retried"
                )
            if row["status"] != "failed":
                raise NotificationStateError("only a known not-performed failure can be retried")
            if int(row["attempt_count"]) >= self.settings.delivery_attempt_limit:
                raise NotificationStateError("delivery attempt limit is exhausted")
            connection.execute(
                "UPDATE outbox SET status = 'pending', error = NULL, updated_at = ? WHERE id = ?",
                (_iso(now), outbox_id),
            )
            return self._entry_by_id(connection, outbox_id)

    def _fail_pending(
        self,
        outbox_id: str,
        error: str,
        scope: ProfileScope,
    ) -> OutboxEntry:
        now = self._now()
        with self._connect() as connection, self._transaction(connection):
            self._assert_outbox_scope(connection, outbox_id, scope)
            row = self._outbox_row(connection, outbox_id)
            if row["status"] == "failed":
                return self._entry_by_id(connection, outbox_id)
            if row["status"] != "pending":
                raise NotificationStateError(
                    f"cannot quarantine outbox entry in {row['status']} state"
                )
            connection.execute(
                "UPDATE outbox SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                (error, _iso(now), outbox_id),
            )
            return self._entry_by_id(connection, outbox_id)

    def _resolve(
        self,
        outbox_id: str,
        disposition: ResolutionDisposition,
        actor: str,
        note: str | None,
        scope: ProfileScope,
    ) -> OutboxEntry:
        now = self._now()
        with self._connect() as connection, self._transaction(connection):
            self._assert_outbox_scope(connection, outbox_id, scope)
            row = self._outbox_row(connection, outbox_id)
            if row["status"] != "in_doubt":
                raise NotificationStateError("only an in_doubt delivery can be resolved")
            connection.execute(
                """
                INSERT INTO operator_resolutions(outbox_id, disposition, actor, note, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (outbox_id, disposition, actor, note, _iso(now)),
            )
            if disposition == "delivered":
                connection.execute(
                    """
                    UPDATE outbox SET status = 'delivered', error = NULL,
                        delivered_at = ?, updated_at = ? WHERE id = ?
                    """,
                    (_iso(now), _iso(now), outbox_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE outbox SET status = 'failed',
                        error = 'operator confirmed delivery was not performed',
                        updated_at = ? WHERE id = ?
                    """,
                    (_iso(now), outbox_id),
                )
            return self._entry_by_id(connection, outbox_id)

    def _cancel(self, outbox_id: str, scope: ProfileScope) -> OutboxEntry:
        now = self._now()
        with self._connect() as connection, self._transaction(connection):
            self._assert_outbox_scope(connection, outbox_id, scope)
            row = self._outbox_row(connection, outbox_id)
            if row["status"] == "claimed":
                raise NotificationStateError("cannot cancel an active delivery claim")
            if row["status"] == "delivered":
                raise NotificationStateError("cannot cancel a delivered notification")
            if row["status"] == "cancelled":
                return self._entry_from_row(row)
            connection.execute(
                """
                UPDATE outbox SET status = 'cancelled', error = NULL,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    updated_at = ? WHERE id = ?
                """,
                (_iso(now), outbox_id),
            )
            return self._entry_by_id(connection, outbox_id)

    def _attempts(
        self,
        outbox_id: str,
        scope: ProfileScope,
    ) -> list[DeliveryAttempt]:
        with self._connect() as connection:
            self._assert_outbox_scope(connection, outbox_id, scope)
            rows = connection.execute(
                "SELECT * FROM delivery_attempts WHERE outbox_id = ? ORDER BY id",
                (outbox_id,),
            )
            return [self._attempt_from_row(row) for row in rows]

    def _resolutions(
        self,
        outbox_id: str,
        scope: ProfileScope,
    ) -> list[OperatorResolution]:
        with self._connect() as connection:
            self._assert_outbox_scope(connection, outbox_id, scope)
            rows = connection.execute(
                "SELECT * FROM operator_resolutions WHERE outbox_id = ? ORDER BY id",
                (outbox_id,),
            )
            return [
                OperatorResolution(
                    id=int(row["id"]),
                    outbox_id=row["outbox_id"],
                    disposition=row["disposition"],
                    actor=row["actor"],
                    note=row["note"],
                    created_at=_datetime(row["created_at"]),
                )
                for row in rows
            ]

    def _expire_claim(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        now: datetime,
    ) -> None:
        error = "delivery claim expired after send status became ambiguous"
        connection.execute(
            """
            UPDATE outbox SET status = 'in_doubt', lease_owner = NULL, lease_token = NULL,
                lease_expires_at = NULL, error = ?, updated_at = ? WHERE id = ?
            """,
            (error, _iso(now), row["id"]),
        )
        connection.execute(
            """
            UPDATE delivery_attempts SET outcome = 'in_doubt', error = ?, finished_at = ?
            WHERE outbox_id = ? AND fence = ? AND outcome = 'claimed'
            """,
            (
                error,
                _iso(now),
                row["id"],
                int(row["fence"]),
            ),
        )

    def _assert_lease(
        self,
        connection: sqlite3.Connection,
        entry: OutboxEntry,
        now: datetime,
    ) -> sqlite3.Row:
        row = self._outbox_row(connection, entry.id)
        if row["status"] != "claimed":
            raise NotificationLeaseError("notification delivery lease is not active")
        if row["lease_token"] != entry.lease_token or int(row["fence"]) != entry.fence:
            raise NotificationLeaseError("notification delivery lease is foreign or stale")
        if _datetime(row["lease_expires_at"]) <= now:
            raise _LeaseExpired
        return row

    def _persist_expired_claim(self, entry: OutboxEntry, now: datetime) -> None:
        with self._connect() as connection, self._transaction(connection):
            row = self._outbox_row(connection, entry.id)
            if (
                row["status"] == "claimed"
                and row["lease_token"] == entry.lease_token
                and int(row["fence"]) == entry.fence
                and _datetime(row["lease_expires_at"]) <= now
            ):
                self._expire_claim(connection, row, now)

    def _record_by_notification(
        self,
        connection: sqlite3.Connection,
        notification_id: str,
    ) -> NotificationRecord:
        row = connection.execute(
            """
            SELECT n.request_json, n.schema_version, o.*
            FROM notifications n JOIN outbox o ON o.notification_id = n.id
            WHERE n.id = ?
            """,
            (notification_id,),
        ).fetchone()
        if row is None:
            raise NotificationNotFoundError("notification was not found")
        return self._record_from_join(row)

    def _record_from_join(self, row: sqlite3.Row) -> NotificationRecord:
        if int(row["schema_version"]) != SCHEMA_VERSION:
            raise NotificationSchemaError("unsupported stored notification schema version")
        try:
            request = NotificationRequest.model_validate_json(row["request_json"])
        except ValidationError as exc:
            raise NotificationSchemaError("stored notification request is invalid") from exc
        return NotificationRecord(request=request, outbox=self._entry_from_row(row))

    @staticmethod
    def _request(payload: str) -> NotificationRequest:
        try:
            return NotificationRequest.model_validate_json(payload)
        except ValidationError as exc:
            raise NotificationSchemaError("stored notification request is invalid") from exc

    @staticmethod
    def _assert_record_scope(record: NotificationRecord, scope: ProfileScope) -> None:
        if not scope.permits(record.request.profile_label):
            raise NotificationNotFoundError(
                "notification was not found in the active profile scope"
            )

    def _assert_outbox_scope(
        self,
        connection: sqlite3.Connection,
        outbox_id: str,
        scope: ProfileScope,
    ) -> None:
        row = connection.execute(
            """SELECT n.request_json FROM outbox o
               JOIN notifications n ON n.id = o.notification_id WHERE o.id = ?""",
            (outbox_id,),
        ).fetchone()
        if row is None:
            raise NotificationNotFoundError("outbox entry was not found")
        if not scope.permits(self._request(row["request_json"]).profile_label):
            raise NotificationNotFoundError(
                "outbox entry was not found in the active profile scope"
            )

    def _entry_by_id(self, connection: sqlite3.Connection, outbox_id: str) -> OutboxEntry:
        return self._entry_from_row(self._outbox_row(connection, outbox_id))

    def _outbox_row(self, connection: sqlite3.Connection, outbox_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM outbox WHERE id = ?", (outbox_id,)).fetchone()
        if row is None:
            raise NotificationNotFoundError("outbox entry was not found")
        return row

    @staticmethod
    def _entry_from_row(row: sqlite3.Row) -> OutboxEntry:
        return OutboxEntry(
            id=row["id"],
            notification_id=row["notification_id"],
            route=row["route"],
            status=row["status"],
            attempt_count=int(row["attempt_count"]),
            fence=int(row["fence"]),
            lease_owner=row["lease_owner"],
            lease_token=row["lease_token"],
            lease_expires_at=_optional_datetime(row["lease_expires_at"]),
            transport=row["transport"],
            destination_ref=row["destination_ref"],
            platform_message_id=row["platform_message_id"],
            error=row["error"],
            created_at=_datetime(row["created_at"]),
            updated_at=_datetime(row["updated_at"]),
            delivered_at=_optional_datetime(row["delivered_at"]),
        )

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> DeliveryAttempt:
        return DeliveryAttempt(
            id=int(row["id"]),
            outbox_id=row["outbox_id"],
            attempt_number=int(row["attempt_number"]),
            fence=int(row["fence"]),
            worker=row["worker"],
            transport=row["transport"],
            destination_ref=row["destination_ref"],
            outcome=row["outcome"],
            error=row["error"],
            started_at=_datetime(row["started_at"]),
            finished_at=_optional_datetime(row["finished_at"]),
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise NotificationStoreError("notification store clock must return aware UTC")
        return value

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.db_path,
            timeout=self.settings.sqlite_busy_timeout_ms / 1_000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.settings.sqlite_busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    @contextmanager
    def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            connection.rollback()
            raise


LiteralFinishState = Literal["pending", "delivered", "failed", "in_doubt"]


def _identity(value: str, name: str) -> str:
    return _bounded_text(value, name, 128)


def _bounded_text(value: str, name: str, limit: int) -> str:
    value = value.strip()
    if not value or len(value) > limit:
        raise ValueError(f"{name} must contain 1 to {limit} characters")
    return value


def _require_scope(scope: ProfileScope, label: ProfileLabel) -> None:
    if not scope.permits(label):
        raise ValueError("notification profile label is outside the active profile scope")


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise NotificationSchemaError("stored notification timestamp is not timezone-aware")
    return parsed.astimezone(UTC)


def _optional_datetime(value: str | None) -> datetime | None:
    return None if value is None else _datetime(value)
