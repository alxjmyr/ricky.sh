"""Transactional inbox, cursor, poller, and outbound-part persistence."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import ValidationError

from ricky.config import RickySettings, user_data_subpath
from ricky.messaging.leases import PollerLease
from ricky.messaging.types import (
    DeliveryPart,
    DeliveryReceipt,
    InboundAttempt,
    InboundMessage,
    InboxClaim,
    PollerLeaseRecord,
    ReceiveBatch,
    StaleInboxClaim,
    TransportCursor,
    TransportMessage,
)
from ricky.notifications.types import OutboxEntry


class MessagingStoreError(RuntimeError):
    """Base durable messaging error safe to render to an operator."""


class MessagingNotFoundError(MessagingStoreError):
    """A requested inbox message or delivery part does not exist."""


class MessagingStateError(MessagingStoreError):
    """A requested messaging state transition is invalid."""


class InboxLeaseError(MessagingStoreError):
    """An inbox claim is absent, expired, foreign, or stale."""


class PollerConflictError(MessagingStoreError):
    """A transport account already has a live poller."""


class MessagingStore:
    """Async-first API sharing the user-global messaging SQLite database."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings.messaging
        self.db_path = user_data_subpath(settings, self.settings.store_path)
        self.root: Path = self.db_path.parent
        self._clock = clock or (lambda: datetime.now(UTC))

    async def initialize(self) -> None:
        await self._run(self._initialize)

    async def cursor(self, transport: str, account: str) -> TransportCursor | None:
        return await self._run(
            self._cursor,
            _text(transport, "transport", 100),
            _text(account, "account", 100),
        )

    async def ingest(self, batch: ReceiveBatch) -> list[InboundMessage]:
        """Persist every handled update and its cursor in one transaction."""

        return await self._run(self._ingest, batch)

    async def list_inbox(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[InboundMessage]:
        if limit < 1 or limit > 1_000:
            raise ValueError("inbox list limit must be between 1 and 1000")
        valid = {"pending", "claimed", "processed", "rejected", "uncertain"}
        if status is not None and status not in valid:
            raise ValueError("invalid inbox status")
        return await self._run(self._list_inbox, status, limit)

    async def list_pending_oldest(self, *, limit: int = 50) -> list[InboundMessage]:
        """Return the globally oldest pending work for ordered dispatch."""

        if limit < 1 or limit > 1_000:
            raise ValueError("inbox list limit must be between 1 and 1000")
        return await self._run(self._list_pending_oldest, limit)

    async def get_inbox(self, message_id: str) -> InboundMessage:
        return await self._run(self._get_inbox, _text(message_id, "message id", 100))

    async def inbound_attempts(self, update_id: str | None = None) -> list[InboundAttempt]:
        return await self._run(self._inbound_attempts, update_id)

    async def claim_inbox(
        self,
        message_id: str,
        *,
        owner: str,
        lease_seconds: int | None = None,
    ) -> InboxClaim:
        duration = lease_seconds or self.settings.lease_seconds
        if duration < 1 or duration > 3_600:
            raise ValueError("inbox lease_seconds must be between 1 and 3600")
        return await self._run(
            self._claim_inbox,
            _text(message_id, "message id", 100),
            _text(owner, "owner", 200),
            duration,
        )

    async def finish_inbox(
        self,
        claim: InboxClaim,
        *,
        status: Literal["processed", "uncertain"],
    ) -> InboundMessage:
        return await self._run(self._finish_inbox, claim, status)

    async def settle_inbox_from_terminal_result(
        self,
        message_id: str,
        *,
        status: Literal["processed", "uncertain"],
        now: datetime | None = None,
    ) -> InboundMessage:
        """Project authoritative terminal turn evidence without stealing a live claim."""

        return await self._run(
            self._settle_inbox_from_terminal_result,
            _text(message_id, "message id", 100),
            status,
            now,
        )

    async def dismiss_inbox(self, message_id: str) -> InboundMessage:
        """Acknowledge pending or uncertain inbox work without running it.

        An operator dismissal deliberately uses the existing terminal
        ``processed`` state: the message has been handled by the operator, not
        by a foreground turn. A claimed record cannot be dismissed because its
        worker might still produce observable output.
        """

        return await self._run(self._dismiss_inbox, _text(message_id, "message id", 100))

    async def acquire_poller(
        self,
        transport: str,
        account: str,
        *,
        owner: str,
        lease_seconds: int,
    ) -> PollerLease:
        if lease_seconds < 1 or lease_seconds > 3_600:
            raise ValueError("poller lease_seconds must be between 1 and 3600")
        return await self._run(
            self._acquire_poller,
            _text(transport, "transport", 100),
            _text(account, "account", 100),
            _text(owner, "owner", 200),
            lease_seconds,
        )

    async def release_poller(self, lease: PollerLease) -> None:
        await self._run(self._release_poller, lease)

    async def prepare_parts(
        self,
        entry: OutboxEntry,
        messages: Sequence[TransportMessage],
    ) -> list[DeliveryPart]:
        if not messages:
            raise ValueError("delivery requires at least one message part")
        return await self._run(self._prepare_parts, entry, list(messages))

    async def delivery_parts(self, outbox_id: str) -> list[DeliveryPart]:
        return await self._run(self._delivery_parts, _text(outbox_id, "outbox id", 100))

    async def find_delivery_part(
        self,
        *,
        transport: str,
        account: str,
        destination_id: str,
        platform_message_id: str,
    ) -> DeliveryPart | None:
        """Resolve a platform reply only through a confirmed stored receipt."""

        return await self._run(
            self._find_delivery_part,
            _text(transport, "transport", 100),
            _text(account, "account", 100),
            _text(destination_id, "destination id", 500),
            _text(platform_message_id, "platform message id", 500),
        )

    async def record_receipt(
        self,
        entry: OutboxEntry,
        receipt: DeliveryReceipt,
    ) -> DeliveryPart:
        return await self._run(self._record_receipt, entry, receipt)

    async def mark_part_in_doubt(
        self,
        entry: OutboxEntry,
        transport_message_id: str,
        *,
        error: str,
    ) -> DeliveryPart:
        return await self._run(
            self._mark_part_in_doubt,
            entry,
            _text(transport_message_id, "transport message id", 100),
            _text(error, "delivery error", 2_000),
        )

    async def stale_inbox_claims(self, *, now: datetime | None = None) -> list[StaleInboxClaim]:
        """List claimed inbox messages whose worker lease has already expired."""

        return await self._run(self._stale_inbox_claims, now)

    async def recover_inbox_claim(
        self,
        message_id: str,
        *,
        status: Literal["pending", "uncertain"],
        now: datetime | None = None,
    ) -> InboundMessage:
        """Resolve one expired inbox claim so a stale worker can never commit."""

        return await self._run(self._recover_inbox_claim, message_id, status, now)

    async def stale_poller_leases(self, *, now: datetime | None = None) -> list[PollerLeaseRecord]:
        """List transport poller leases whose owner process is gone."""

        return await self._run(self._stale_poller_leases, now)

    async def active_poller_leases(self, *, now: datetime | None = None) -> list[PollerLeaseRecord]:
        """List transport poller leases that are still live at this moment."""

        return await self._run(self._active_poller_leases, now)

    async def clear_poller_lease(self, transport: str, account: str) -> bool:
        """Delete one expired poller lease. Returns False when it was already gone."""

        return await self._run(self._clear_poller_lease, transport, account)

    async def inbox_counts(self) -> dict[str, int]:
        """Count inbox messages by status without loading any message body."""

        return await self._run(self._inbox_counts)

    async def oldest_pending_inbox(self) -> datetime | None:
        """Return the receive time of the oldest pending inbox message."""

        return await self._run(self._oldest_pending_inbox)

    async def protected_inbox_ids(self) -> tuple[str, ...]:
        """Return every non-disposable inbox id without an operator-list cap."""

        return await self._run(self._protected_inbox_ids)

    async def last_cursor_update(self, transport: str, account: str) -> datetime | None:
        """Return when this transport account last durably advanced its cursor."""

        return await self._run(self._last_cursor_update, transport, account)

    async def prunable_inbox(
        self,
        *,
        keep: int,
        before: datetime,
        protected: Sequence[str] = (),
    ) -> list[str]:
        """List terminal inbox message ids that exceed the retention ceiling."""

        return await self._run(self._prunable_inbox, keep, before, tuple(protected))

    async def prune_inbox(self, message_ids: Sequence[str]) -> int:
        """Delete exactly these terminal inbox messages and their attempts."""

        return await self._run(self._prune_inbox, tuple(message_ids))

    async def _run(self, operation: Callable[..., Any], *args: Any) -> Any:
        try:
            task = asyncio.create_task(asyncio.to_thread(operation, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
        except MessagingStoreError:
            raise
        except sqlite3.Error as exc:
            raise MessagingStoreError("messaging store operation failed") from exc

    def _initialize(self) -> None:
        from ricky.messaging.upgrade import (
            MessagingUpgradeError,
            create_current_messaging_store,
            inspect_messaging_store,
        )

        inspection = inspect_messaging_store(self.db_path)
        if inspection.state != "current":
            if inspection.state != "absent":
                raise MessagingStoreError(inspection.detail)
            try:
                create_current_messaging_store(self.db_path)
            except MessagingUpgradeError as exc:
                raise MessagingStoreError(str(exc)) from exc
        # The inbox holds authenticated message text and sender ids as private
        # evidence, so every open re-asserts the private modes instead of
        # trusting the creation path.
        os.chmod(self.root, 0o700)
        for path in (self.db_path, Path(f"{self.db_path}-wal"), Path(f"{self.db_path}-shm")):
            if path.is_file():
                os.chmod(path, 0o600)

    def _cursor(self, transport: str, account: str) -> TransportCursor | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM transport_cursors WHERE transport = ? AND account = ?",
                (transport, account),
            ).fetchone()
            if row is None:
                return None
            return TransportCursor(transport=transport, account=account, value=row["value"])

    def _ingest(self, batch: ReceiveBatch) -> list[InboundMessage]:
        now = self._now()
        stored: list[InboundMessage] = []
        with self._connect() as connection, self._transaction(connection):
            for update in batch.updates:
                existing = connection.execute(
                    """SELECT id FROM inbox_messages
                    WHERE transport = ? AND account = ? AND update_id = ?""",
                    (batch.transport, batch.account, update.update_id),
                ).fetchone()
                outcome: Literal["accepted", "rejected", "duplicate"]
                if existing is None:
                    message = update.message
                    connection.execute(
                        """INSERT INTO inbox_messages(
                            id, transport, account, update_id, message_json, status,
                            received_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            message.id,
                            message.transport,
                            message.account,
                            message.update_id,
                            message.model_dump_json(),
                            message.status,
                            _iso(message.received_at),
                            _iso(now),
                        ),
                    )
                    stored.append(message)
                    outcome = "rejected" if message.status == "rejected" else "accepted"
                    message_id: str | None = message.id
                else:
                    outcome = "duplicate"
                    message_id = existing["id"]
                attempt = InboundAttempt(
                    id=f"inbound_attempt_{uuid4().hex}",
                    transport=batch.transport,
                    account=batch.account,
                    update_id=update.update_id,
                    message_id=message_id,
                    outcome=outcome,
                    reason=update.rejection_reason
                    if outcome != "duplicate"
                    else "duplicate update",
                    created_at=now,
                )
                connection.execute(
                    """INSERT INTO inbound_attempts(
                        id, transport, account, update_id, activity_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        attempt.id,
                        attempt.transport,
                        attempt.account,
                        attempt.update_id,
                        attempt.model_dump_json(),
                        _iso(now),
                    ),
                )
            if batch.next_cursor is not None:
                connection.execute(
                    """INSERT INTO transport_cursors(transport, account, value, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(transport, account) DO UPDATE
                    SET value = excluded.value, updated_at = excluded.updated_at""",
                    (batch.transport, batch.account, batch.next_cursor.value, _iso(now)),
                )
        return stored

    def _list_inbox(self, status: str | None, limit: int) -> list[InboundMessage]:
        with self._connect() as connection:
            query = "SELECT * FROM inbox_messages"
            params: tuple[object, ...]
            if status is None:
                query += " ORDER BY received_at DESC, id DESC LIMIT ?"
                params = (limit,)
            else:
                query += " WHERE status = ? ORDER BY received_at DESC, id DESC LIMIT ?"
                params = (status, limit)
            return [self._message_from_row(row) for row in connection.execute(query, params)]

    def _list_pending_oldest(self, limit: int) -> list[InboundMessage]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM inbox_messages WHERE status = 'pending'
                ORDER BY received_at ASC, id ASC LIMIT ?""",
                (limit,),
            )
            return [self._message_from_row(row) for row in rows]

    def _get_inbox(self, message_id: str) -> InboundMessage:
        with self._connect() as connection:
            return self._message_from_row(self._inbox_row(connection, message_id))

    def _inbound_attempts(self, update_id: str | None) -> list[InboundAttempt]:
        with self._connect() as connection:
            if update_id is None:
                rows = connection.execute("SELECT * FROM inbound_attempts ORDER BY rowid")
            else:
                rows = connection.execute(
                    "SELECT * FROM inbound_attempts WHERE update_id = ? ORDER BY rowid",
                    (_text(update_id, "update id", 100),),
                )
            result: list[InboundAttempt] = []
            for row in rows:
                try:
                    result.append(InboundAttempt.model_validate_json(row["activity_json"]))
                except ValidationError as exc:
                    raise MessagingStoreError("stored inbound activity is invalid") from exc
            return result

    def _claim_inbox(self, message_id: str, owner: str, duration: int) -> InboxClaim:
        now = self._now()
        with self._connect() as connection, self._transaction(connection):
            row = self._inbox_row(connection, message_id)
            if row["status"] == "claimed":
                if _datetime(row["lease_expires_at"]) > now:
                    raise InboxLeaseError("inbox message is already claimed")
                connection.execute(
                    """UPDATE inbox_messages SET status = 'uncertain', lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL, updated_at = ? WHERE id = ?""",
                    (_iso(now), message_id),
                )
                raise InboxLeaseError("expired inbox claim became uncertain")
            if row["status"] != "pending":
                raise MessagingStateError(f"cannot claim inbox message in {row['status']} state")
            fence = int(row["fence"]) + 1
            token = uuid4().hex
            expires_at = now + timedelta(seconds=duration)
            connection.execute(
                """UPDATE inbox_messages SET status = 'claimed', fence = ?, lease_owner = ?,
                lease_token = ?, lease_expires_at = ?, updated_at = ? WHERE id = ?""",
                (fence, owner, token, _iso(expires_at), _iso(now), message_id),
            )
            return InboxClaim(
                message_id=message_id,
                owner=owner,
                token=token,
                fence=fence,
                expires_at=expires_at,
            )

    def _finish_inbox(
        self,
        claim: InboxClaim,
        status: Literal["processed", "uncertain"],
    ) -> InboundMessage:
        now = self._now()
        with self._connect() as connection, self._transaction(connection):
            row = self._inbox_row(connection, claim.message_id)
            if (
                row["status"] != "claimed"
                or row["lease_token"] != claim.token
                or int(row["fence"]) != claim.fence
            ):
                raise InboxLeaseError("inbox claim is foreign or stale")
            if _datetime(row["lease_expires_at"]) <= now:
                connection.execute(
                    """UPDATE inbox_messages SET status = 'uncertain', lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL, updated_at = ? WHERE id = ?""",
                    (_iso(now), claim.message_id),
                )
                raise InboxLeaseError("inbox claim expired and became uncertain")
            connection.execute(
                """UPDATE inbox_messages SET status = ?, lease_owner = NULL,
                lease_token = NULL, lease_expires_at = NULL, updated_at = ? WHERE id = ?""",
                (status, _iso(now), claim.message_id),
            )
            return self._message_from_row(self._inbox_row(connection, claim.message_id))

    def _settle_inbox_from_terminal_result(
        self,
        message_id: str,
        status: Literal["processed", "uncertain"],
        now: datetime | None,
    ) -> InboundMessage:
        moment = now or self._now()
        with self._connect() as connection, self._transaction(connection):
            row = self._inbox_row(connection, message_id)
            current = str(row["status"])
            if current == "rejected":
                raise MessagingStateError("rejected inbox messages cannot have gateway results")
            if current == "claimed" and _datetime(row["lease_expires_at"]) > moment:
                raise InboxLeaseError("live inbox claim still owns terminal-result settlement")
            if current not in {"pending", "claimed", "processed", "uncertain"}:
                raise MessagingStateError(
                    f"cannot settle terminal gateway result from inbox state {current}"
                )
            connection.execute(
                """UPDATE inbox_messages SET status = ?, lease_owner = NULL,
                   lease_token = NULL, lease_expires_at = NULL, updated_at = ? WHERE id = ?""",
                (status, _iso(moment), message_id),
            )
            return self._message_from_row(self._inbox_row(connection, message_id))

    def _dismiss_inbox(self, message_id: str) -> InboundMessage:
        now = self._now()
        with self._connect() as connection, self._transaction(connection):
            row = self._inbox_row(connection, message_id)
            if row["status"] not in {"pending", "uncertain"}:
                raise MessagingStateError(
                    f"can only dismiss pending or uncertain inbox messages, not {row['status']}"
                )
            connection.execute(
                """UPDATE inbox_messages SET status = 'processed', lease_owner = NULL,
                lease_token = NULL, lease_expires_at = NULL, updated_at = ? WHERE id = ?""",
                (_iso(now), message_id),
            )
            return self._message_from_row(self._inbox_row(connection, message_id))

    def _acquire_poller(
        self,
        transport: str,
        account: str,
        owner: str,
        duration: int,
    ) -> PollerLease:
        now = self._now()
        expires_at = now + timedelta(seconds=duration)
        with self._connect() as connection, self._transaction(connection):
            row = connection.execute(
                "SELECT * FROM poller_leases WHERE transport = ? AND account = ?",
                (transport, account),
            ).fetchone()
            if row is not None and _datetime(row["expires_at"]) > now:
                raise PollerConflictError(
                    f"{transport} account {account!r} already has an active poller"
                )
            fence = 1 if row is None else int(row["fence"]) + 1
            token = uuid4().hex
            connection.execute(
                """INSERT INTO poller_leases(transport, account, owner, token, fence, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(transport, account) DO UPDATE SET
                    owner = excluded.owner, token = excluded.token,
                    fence = excluded.fence, expires_at = excluded.expires_at""",
                (transport, account, owner, token, fence, _iso(expires_at)),
            )
            return PollerLease(
                transport=transport,
                account=account,
                owner=owner,
                token=token,
                fence=fence,
                expires_at=expires_at,
            )

    def _release_poller(self, lease: PollerLease) -> None:
        with self._connect() as connection, self._transaction(connection):
            cursor = connection.execute(
                """DELETE FROM poller_leases
                WHERE transport = ? AND account = ? AND token = ? AND fence = ?""",
                (lease.transport, lease.account, lease.token, lease.fence),
            )
            if cursor.rowcount != 1:
                raise PollerConflictError("poller lease is foreign or stale")

    def _stale_inbox_claims(self, now: datetime | None) -> list[StaleInboxClaim]:
        moment = now or self._now()
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM inbox_messages
                   WHERE status = 'claimed' AND lease_expires_at <= ?
                   ORDER BY received_at, id""",
                (_iso(moment),),
            ).fetchall()
        return [
            StaleInboxClaim(
                message=self._message_from_row(row),
                owner=row["lease_owner"] or "unknown",
                fence=int(row["fence"]),
                expired_at=_datetime(row["lease_expires_at"]),
            )
            for row in rows
        ]

    def _recover_inbox_claim(
        self,
        message_id: str,
        status: Literal["pending", "uncertain"],
        now: datetime | None,
    ) -> InboundMessage:
        moment = now or self._now()
        with self._connect() as connection, self._transaction(connection):
            row = self._inbox_row(connection, message_id)
            if row["status"] != "claimed":
                raise MessagingStateError(f"cannot recover inbox message in {row['status']} state")
            if _datetime(row["lease_expires_at"]) > moment:
                raise InboxLeaseError("inbox claim has not expired")
            connection.execute(
                """UPDATE inbox_messages SET status = ?, lease_owner = NULL,
                   lease_token = NULL, lease_expires_at = NULL, updated_at = ? WHERE id = ?""",
                (status, _iso(moment), message_id),
            )
            return self._message_from_row(self._inbox_row(connection, message_id))

    def _stale_poller_leases(self, now: datetime | None) -> list[PollerLeaseRecord]:
        moment = now or self._now()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM poller_leases WHERE expires_at <= ? ORDER BY transport, account",
                (_iso(moment),),
            ).fetchall()
        return [
            PollerLeaseRecord(
                transport=row["transport"],
                account=row["account"],
                owner=row["owner"],
                fence=int(row["fence"]),
                expires_at=_datetime(row["expires_at"]),
            )
            for row in rows
        ]

    def _active_poller_leases(self, now: datetime | None) -> list[PollerLeaseRecord]:
        moment = now or self._now()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM poller_leases WHERE expires_at > ? ORDER BY transport, account",
                (_iso(moment),),
            ).fetchall()
        return [
            PollerLeaseRecord(
                transport=row["transport"],
                account=row["account"],
                owner=row["owner"],
                fence=int(row["fence"]),
                expires_at=_datetime(row["expires_at"]),
            )
            for row in rows
        ]

    def _clear_poller_lease(self, transport: str, account: str) -> bool:
        with self._connect() as connection, self._transaction(connection):
            cursor = connection.execute(
                "DELETE FROM poller_leases WHERE transport = ? AND account = ?",
                (transport, account),
            )
            return cursor.rowcount == 1

    def _inbox_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM inbox_messages GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def _oldest_pending_inbox(self) -> datetime | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MIN(received_at) AS oldest FROM inbox_messages WHERE status = 'pending'"
            ).fetchone()
        if row is None or row["oldest"] is None:
            return None
        return _datetime(row["oldest"])

    def _protected_inbox_ids(self) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM inbox_messages
                WHERE status IN ('pending','claimed','uncertain')
                ORDER BY id ASC
                """
            ).fetchall()
        return tuple(str(row["id"]) for row in rows)

    def _last_cursor_update(self, transport: str, account: str) -> datetime | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT updated_at FROM transport_cursors WHERE transport = ? AND account = ?",
                (transport, account),
            ).fetchone()
        return None if row is None else _datetime(row["updated_at"])

    def _prunable_inbox(
        self,
        keep: int,
        before: datetime,
        protected: tuple[str, ...],
    ) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id FROM inbox_messages
                   WHERE status IN ('processed','rejected') AND received_at < ?
                   ORDER BY received_at DESC, id DESC""",
                (_iso(before),),
            ).fetchall()
        blocked = set(protected)
        candidates = [str(row["id"]) for row in rows if str(row["id"]) not in blocked]
        return sorted(candidates[keep:])

    def _prune_inbox(self, message_ids: tuple[str, ...]) -> int:
        if not message_ids:
            return 0
        removed = 0
        with self._connect() as connection, self._transaction(connection):
            for message_id in message_ids:
                row = connection.execute(
                    "SELECT status, transport, account, update_id FROM inbox_messages WHERE id = ?",
                    (message_id,),
                ).fetchone()
                if row is None or row["status"] not in {"processed", "rejected"}:
                    continue
                connection.execute(
                    """DELETE FROM inbound_attempts
                       WHERE transport = ? AND account = ? AND update_id = ?""",
                    (row["transport"], row["account"], row["update_id"]),
                )
                connection.execute("DELETE FROM inbox_messages WHERE id = ?", (message_id,))
                removed += 1
        return removed

    def _prepare_parts(
        self,
        entry: OutboxEntry,
        messages: list[TransportMessage],
    ) -> list[DeliveryPart]:
        now = self._now()
        with self._connect() as connection, self._transaction(connection):
            self._assert_outbox_claim(connection, entry, now)
            existing = connection.execute(
                "SELECT COUNT(*) AS count FROM delivery_parts WHERE outbox_id = ? AND fence = ?",
                (entry.id, entry.fence),
            ).fetchone()
            if int(existing["count"]) != 0:
                raise MessagingStateError("delivery parts were already prepared")
            expected = list(range(1, len(messages) + 1))
            if [item.part_number for item in messages] != expected:
                raise ValueError("delivery parts must be complete and ordered")
            if any(
                item.part_count != len(messages) or item.outbox_id != entry.id for item in messages
            ):
                raise ValueError("delivery part identity does not match its outbox entry")
            for message in messages:
                connection.execute(
                    """INSERT INTO delivery_parts(
                        outbox_id, fence, transport_message_id, part_number,
                        message_json, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
                    (
                        entry.id,
                        entry.fence,
                        message.id,
                        message.part_number,
                        message.model_dump_json(),
                        _iso(now),
                        _iso(now),
                    ),
                )
            return self._delivery_parts_with_connection(connection, entry.id)

    def _delivery_parts(self, outbox_id: str) -> list[DeliveryPart]:
        with self._connect() as connection:
            return self._delivery_parts_with_connection(connection, outbox_id)

    def _find_delivery_part(
        self,
        transport: str,
        account: str,
        destination_id: str,
        platform_message_id: str,
    ) -> DeliveryPart | None:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM delivery_parts
                   WHERE status = 'delivered' AND platform_message_id = ?""",
                (platform_message_id,),
            ).fetchall()
        matches: list[DeliveryPart] = []
        for row in rows:
            part = self._part_from_row(row)
            message = part.message
            if (
                message.transport == transport
                and message.account == account
                and message.destination_id == destination_id
            ):
                matches.append(part)
        if len(matches) > 1:
            raise MessagingStateError("platform receipt is not unique for this destination")
        return matches[0] if matches else None

    def _record_receipt(self, entry: OutboxEntry, receipt: DeliveryReceipt) -> DeliveryPart:
        now = self._now()
        with self._connect() as connection, self._transaction(connection):
            self._assert_outbox_claim(connection, entry, now)
            row = connection.execute(
                """SELECT * FROM delivery_parts
                WHERE outbox_id = ? AND fence = ? AND transport_message_id = ?""",
                (entry.id, entry.fence, receipt.transport_message_id),
            ).fetchone()
            if row is None:
                raise MessagingNotFoundError("delivery part was not found")
            if row["status"] != "pending":
                raise MessagingStateError("delivery part is not pending")
            connection.execute(
                """UPDATE delivery_parts SET status = 'delivered', platform_message_id = ?,
                updated_at = ? WHERE transport_message_id = ?""",
                (receipt.platform_message_id, _iso(now), receipt.transport_message_id),
            )
            return self._delivery_part_row(
                connection, entry.id, entry.fence, int(row["part_number"])
            )

    def _mark_part_in_doubt(
        self,
        entry: OutboxEntry,
        transport_message_id: str,
        error: str,
    ) -> DeliveryPart:
        now = self._now()
        with self._connect() as connection, self._transaction(connection):
            self._assert_outbox_claim(connection, entry, now)
            row = connection.execute(
                """SELECT * FROM delivery_parts
                WHERE outbox_id = ? AND fence = ? AND transport_message_id = ?""",
                (entry.id, entry.fence, transport_message_id),
            ).fetchone()
            if row is None:
                raise MessagingNotFoundError("delivery part was not found")
            if row["status"] != "pending":
                raise MessagingStateError("delivery part is not pending")
            connection.execute(
                """UPDATE delivery_parts SET status = 'in_doubt', error = ?, updated_at = ?
                WHERE transport_message_id = ?""",
                (error, _iso(now), transport_message_id),
            )
            return self._delivery_part_row(
                connection, entry.id, entry.fence, int(row["part_number"])
            )

    def _delivery_parts_with_connection(
        self,
        connection: sqlite3.Connection,
        outbox_id: str,
    ) -> list[DeliveryPart]:
        rows = connection.execute(
            "SELECT * FROM delivery_parts WHERE outbox_id = ? ORDER BY part_number",
            (outbox_id,),
        )
        return [self._part_from_row(row) for row in rows]

    def _delivery_part_row(
        self,
        connection: sqlite3.Connection,
        outbox_id: str,
        fence: int,
        part_number: int,
    ) -> DeliveryPart:
        row = connection.execute(
            """SELECT * FROM delivery_parts
            WHERE outbox_id = ? AND fence = ? AND part_number = ?""",
            (outbox_id, fence, part_number),
        ).fetchone()
        assert row is not None
        return self._part_from_row(row)

    def _assert_outbox_claim(
        self,
        connection: sqlite3.Connection,
        entry: OutboxEntry,
        now: datetime,
    ) -> None:
        row = connection.execute("SELECT * FROM outbox WHERE id = ?", (entry.id,)).fetchone()
        if row is None:
            raise MessagingNotFoundError("outbox entry was not found")
        if (
            row["status"] != "claimed"
            or row["lease_token"] != entry.lease_token
            or int(row["fence"]) != entry.fence
        ):
            raise MessagingStateError("outbox delivery claim is foreign or stale")
        if _datetime(row["lease_expires_at"]) <= now:
            raise MessagingStateError("outbox delivery claim expired")

    def _inbox_row(self, connection: sqlite3.Connection, message_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM inbox_messages WHERE id = ?", (message_id,)
        ).fetchone()
        if row is None:
            raise MessagingNotFoundError("inbox message was not found")
        return row

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> InboundMessage:
        try:
            message = InboundMessage.model_validate_json(row["message_json"])
            return message.model_copy(update={"status": row["status"]})
        except ValidationError as exc:
            raise MessagingStoreError("stored inbox message is invalid") from exc

    @staticmethod
    def _part_from_row(row: sqlite3.Row) -> DeliveryPart:
        try:
            message = TransportMessage.model_validate_json(row["message_json"])
        except ValidationError as exc:
            raise MessagingStoreError("stored delivery part is invalid") from exc
        return DeliveryPart(
            outbox_id=row["outbox_id"],
            fence=int(row["fence"]),
            message=message,
            status=row["status"],
            platform_message_id=row["platform_message_id"],
            error=row["error"],
            created_at=_datetime(row["created_at"]),
            updated_at=_datetime(row["updated_at"]),
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise MessagingStoreError("messaging store clock must return aware UTC")
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
        else:
            connection.commit()


def _text(value: str, name: str, limit: int) -> str:
    value = value.strip()
    if not value or len(value) > limit:
        raise ValueError(f"{name} must contain 1 to {limit} characters")
    return value


def _iso(value: datetime) -> str:
    return value.isoformat()


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)
