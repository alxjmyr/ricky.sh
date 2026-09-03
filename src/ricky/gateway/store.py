"""SQLite persistence for foreground conversation mappings and inbound results."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import ValidationError

from ricky.config import RickySettings, user_data_subpath
from ricky.gateway.types import (
    Conversation,
    ConversationKey,
    ConversationStatus,
    GatewayInboundResult,
)
from ricky.notifications.routes import ResolvedRoute
from ricky.profiles import ProfileLabel, ProfileScope


class GatewayStoreError(RuntimeError):
    """Base gateway persistence error safe for an operator."""


class ConversationNotFoundError(GatewayStoreError):
    """A requested conversation does not exist."""


class ConversationConflictError(GatewayStoreError):
    """A conversation revision or active key changed concurrently."""


class GatewayResultConflictError(GatewayStoreError):
    """An inbound result already exists or changed state."""


class GatewayStore:
    """Async-first store for conversation identity and one-shot inbound results."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings.gateway
        self.db_path = user_data_subpath(settings, self.settings.store_path)
        self.root: Path = self.db_path.parent
        self._clock = clock or (lambda: datetime.now(UTC))

    async def initialize(self) -> None:
        await self._run(self._initialize)

    async def get(self, conversation_id: str, *, scope: ProfileScope) -> Conversation:
        return await self._run(self._get, conversation_id, scope)

    async def get_active(
        self,
        key: ConversationKey,
        *,
        scope: ProfileScope,
    ) -> Conversation | None:
        return await self._run(self._get_active, key, scope)

    async def find_rotation_source(
        self,
        key: ConversationKey,
        inbound_message_id: str,
        *,
        scope: ProfileScope,
    ) -> Conversation | None:
        """Find the archived predecessor that durably records one `/new` intent."""

        return await self._run(self._find_rotation_source, key, inbound_message_id, scope)

    async def list(
        self,
        *,
        scope: ProfileScope,
        status: ConversationStatus | None = None,
        limit: int = 50,
    ) -> list[Conversation]:
        if not 1 <= limit <= 1_000:
            raise ValueError("conversation list limit must be between 1 and 1000")
        return await self._run(self._list, status, limit, scope)

    async def create(
        self,
        *,
        key: ConversationKey,
        session_id: str,
        route_name: str,
        provider: str,
        model: str,
        profile_scope: ProfileScope,
        project_root: str | None,
        route_policy_digest: str | None = None,
        created_for_inbound_message_id: str | None = None,
    ) -> Conversation:
        conversation = Conversation(
            id=f"conversation_{uuid4().hex}",
            key=key,
            session_id=session_id,
            route_name=route_name,
            provider=provider,
            model=model,
            profile_scope=profile_scope,
            project_root=project_root,
            route_policy_digest=route_policy_digest,
            created_for_inbound_message_id=created_for_inbound_message_id,
            status="active",
            revision=0,
            created_at=self._now(),
            updated_at=self._now(),
        )
        return await self._run(self._create, conversation)

    async def archive(
        self,
        conversation_id: str,
        *,
        scope: ProfileScope,
        expected_revision: int,
        for_inbound_message_id: str | None = None,
    ) -> Conversation:
        return await self._run(
            self._archive,
            conversation_id,
            expected_revision,
            for_inbound_message_id,
            scope,
        )

    async def mark_uncertain(
        self,
        conversation_id: str,
        *,
        scope: ProfileScope,
        expected_revision: int,
    ) -> Conversation:
        return await self._run(
            self._mark_uncertain,
            conversation_id,
            expected_revision,
            scope,
        )

    async def begin_result(
        self,
        *,
        message_id: str,
        conversation_id: str,
        session_id: str,
        scope: ProfileScope,
    ) -> GatewayInboundResult:
        conversation = await self.get(conversation_id, scope=scope)
        result = GatewayInboundResult(
            message_id=message_id,
            conversation_id=conversation_id,
            session_id=session_id,
            profile_label=conversation.profile_scope.label(),
            status="running",
            started_at=self._now(),
        )
        return await self._run(self._begin_result, result)

    async def get_result(
        self,
        message_id: str,
        *,
        scope: ProfileScope,
    ) -> GatewayInboundResult | None:
        return await self._run(self._get_result, message_id, scope)

    async def finish_result(
        self,
        *,
        scope: ProfileScope,
        message_id: str,
        conversation_id: str,
        expected_conversation_revision: int,
        status: Literal["committed", "failed", "uncertain"],
        session_revision: int | None,
        response_outbox_id: str | None,
        error: str | None,
    ) -> tuple[Conversation, GatewayInboundResult]:
        return await self._run(
            self._finish_result,
            message_id,
            conversation_id,
            expected_conversation_revision,
            status,
            session_revision,
            response_outbox_id,
            error,
            scope,
        )

    async def results(
        self,
        conversation_id: str,
        *,
        scope: ProfileScope,
        limit: int = 50,
    ) -> list[GatewayInboundResult]:
        if not 1 <= limit <= 1_000:
            raise ValueError("gateway result limit must be between 1 and 1000")
        return await self._run(self._results, conversation_id, limit, scope)

    async def running_results(self, *, scope: ProfileScope) -> list[GatewayInboundResult]:
        """List foreground turns that were still running when the process stopped."""

        return await self._run(self._running_results, scope)

    async def recover_running_result(
        self,
        message_id: str,
        *,
        scope: ProfileScope,
        error: str,
    ) -> tuple[Conversation, GatewayInboundResult]:
        """Mark one interrupted foreground turn and its conversation uncertain.

        A turn that began owns model or tool output that this process cannot
        observe after a restart, so the only honest terminal state is uncertain.
        """

        return await self._run(self._recover_running_result, message_id, error, scope)

    async def result_counts(self, *, scope: ProfileScope) -> dict[str, int]:
        """Count foreground turn results by state."""

        return await self._run(self._result_counts, scope)

    async def conversation_counts(self, *, scope: ProfileScope) -> dict[str, int]:
        """Count conversations by state."""

        return await self._run(self._conversation_counts, scope)

    async def result_for_message(
        self,
        message_id: str,
        *,
        scope: ProfileScope,
    ) -> GatewayInboundResult | None:
        """Read one durable turn outcome by exact inbound message id."""

        return await self._run(self._get_result, message_id, scope)

    async def prunable_results(
        self,
        *,
        scope: ProfileScope,
        keep: int,
        before: datetime,
    ) -> list[str]:
        """List terminal, committed turn results that exceed the retention ceiling."""

        return await self._run(self._prunable_results, keep, before, scope)

    async def prune_results(
        self,
        message_ids: Sequence[str],
        *,
        scope: ProfileScope,
    ) -> int:
        """Delete exactly these committed turn results."""

        return await self._run(self._prune_results, tuple(message_ids), scope)

    async def prunable_conversations(
        self,
        *,
        scope: ProfileScope,
        keep: int,
        before: datetime,
    ) -> list[str]:
        """List archived conversations with no remaining turn result."""

        return await self._run(self._prunable_conversations, keep, before, scope)

    async def prune_conversations(
        self,
        conversation_ids: Sequence[str],
        *,
        scope: ProfileScope,
    ) -> int:
        """Delete exactly these archived, result-free conversations."""

        return await self._run(self._prune_conversations, tuple(conversation_ids), scope)

    async def resolve_conversation_route(
        self,
        conversation_id: str,
        profile_label: ProfileLabel,
    ) -> ResolvedRoute:
        conversation = await self._run(self._get_for_label, conversation_id, profile_label)
        if conversation.status == "uncertain":
            raise GatewayStoreError("uncertain conversation cannot resolve an outbound route")
        return ResolvedRoute(
            route=f"conversation:{conversation.id}",
            transport=conversation.key.transport,
            account=conversation.key.account,
            destination_ref=conversation.key.destination_id,
            owner_profile=conversation.profile_scope.primary,
            accepted_profiles=list(conversation.profile_scope.profiles),
        )

    async def _run[T](self, operation: Callable[..., T], *args: object) -> T:
        task = asyncio.create_task(asyncio.to_thread(operation, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.shield(task)
            raise
        except GatewayStoreError:
            raise
        except sqlite3.Error as exc:
            raise GatewayStoreError("gateway store operation failed") from exc

    def _initialize(self) -> None:
        # Import lazily to keep the ordinary store module independent of the
        # installation-wide coordinator while retaining schema ownership here.
        from ricky.gateway.upgrade import create_current_gateway_store, inspect_gateway_store

        # inspect_gateway_store raises for every store that is not current, so a
        # returned inspection is this path's proof.  Consult it rather than
        # discarding it: a store removed between the exists() test and the
        # inspection reports exists=False and still needs to be created.
        if self.db_path.exists():
            inspection = inspect_gateway_store(self.db_path)
            if inspection.exists:
                self.db_path.chmod(0o600)
                return
        create_current_gateway_store(self.db_path)

    def _create(self, conversation: Conversation) -> Conversation:
        with self._connect() as connection:
            try:
                connection.execute(
                    """INSERT INTO conversations(
                        id, key_digest, conversation_json, status, revision,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        conversation.id,
                        conversation.key.digest(),
                        conversation.model_dump_json(),
                        conversation.status,
                        conversation.revision,
                        _iso(conversation.created_at),
                        _iso(conversation.updated_at),
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                raise ConversationConflictError(
                    "an active conversation already exists for this transport key"
                ) from exc
        return conversation

    def _get(self, conversation_id: str, scope: ProfileScope) -> Conversation:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        if row is None:
            raise ConversationNotFoundError(f"conversation not found: {conversation_id}")
        conversation = self._conversation(row)
        self._assert_conversation_scope(conversation, scope)
        return conversation

    def _get_active(
        self,
        key: ConversationKey,
        scope: ProfileScope,
    ) -> Conversation | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM conversations
                   WHERE key_digest = ? AND status = 'active'""",
                (key.digest(),),
            ).fetchone()
        if row is None:
            return None
        conversation = self._conversation(row)
        if conversation.key != key:
            raise GatewayStoreError("conversation key digest collision")
        self._assert_conversation_scope(conversation, scope)
        return conversation

    def _find_rotation_source(
        self,
        key: ConversationKey,
        inbound_message_id: str,
        scope: ProfileScope,
    ) -> Conversation | None:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM conversations
                   WHERE key_digest = ? AND status = 'archived'
                   ORDER BY updated_at DESC, id DESC""",
                (key.digest(),),
            ).fetchall()
        for row in rows:
            conversation = self._conversation(row)
            if conversation.key != key:
                raise GatewayStoreError("conversation key digest collision")
            if conversation.archived_for_inbound_message_id == inbound_message_id:
                self._assert_conversation_scope(conversation, scope)
                return conversation
        return None

    def _list(
        self,
        status: ConversationStatus | None,
        limit: int,
        scope: ProfileScope,
    ) -> list[Conversation]:
        with self._connect() as connection:
            if status is None:
                rows = connection.execute(
                    """SELECT * FROM conversations
                       ORDER BY updated_at DESC, id DESC"""
                )
            else:
                rows = connection.execute(
                    """SELECT * FROM conversations WHERE status = ?
                       ORDER BY updated_at DESC, id DESC""",
                    (status,),
                )
            conversations = (self._conversation(row) for row in rows)
            return [item for item in conversations if scope.permits(item.profile_scope.label())][
                :limit
            ]

    def _archive(
        self,
        conversation_id: str,
        expected_revision: int,
        for_inbound_message_id: str | None,
        scope: ProfileScope,
    ) -> Conversation:
        return self._change_status(
            conversation_id,
            expected_revision,
            "archived",
            for_inbound_message_id=for_inbound_message_id,
            scope=scope,
        )

    def _mark_uncertain(
        self,
        conversation_id: str,
        expected_revision: int,
        scope: ProfileScope,
    ) -> Conversation:
        return self._change_status(
            conversation_id,
            expected_revision,
            "uncertain",
            scope=scope,
        )

    def _change_status(
        self,
        conversation_id: str,
        expected_revision: int,
        status: Literal["archived", "uncertain"],
        *,
        for_inbound_message_id: str | None = None,
        scope: ProfileScope,
    ) -> Conversation:
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise ConversationNotFoundError(f"conversation not found: {conversation_id}")
            current = self._conversation(row)
            self._assert_conversation_scope(current, scope)
            if current.revision != expected_revision or current.status != "active":
                connection.rollback()
                raise ConversationConflictError("conversation revision or status changed")
            changed = current.model_copy(
                update={
                    "status": status,
                    "revision": current.revision + 1,
                    "updated_at": now,
                    "archived_for_inbound_message_id": for_inbound_message_id,
                }
            )
            connection.execute(
                """UPDATE conversations SET conversation_json = ?, status = ?,
                   revision = ?, updated_at = ? WHERE id = ?""",
                (
                    changed.model_dump_json(),
                    changed.status,
                    changed.revision,
                    _iso(now),
                    changed.id,
                ),
            )
            connection.commit()
        return changed

    def _begin_result(self, result: GatewayInboundResult) -> GatewayInboundResult:
        with self._connect() as connection:
            conversation_row = connection.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (result.conversation_id,),
            ).fetchone()
            if conversation_row is None:
                raise ConversationNotFoundError(f"conversation not found: {result.conversation_id}")
            conversation = self._conversation(conversation_row)
            if result.profile_label != conversation.profile_scope.label():
                raise GatewayResultConflictError(
                    "gateway result profile label does not match its conversation"
                )
            try:
                connection.execute(
                    """INSERT INTO gateway_inbound_results(
                        message_id, conversation_id, result_json, status,
                        started_at, finished_at
                    ) VALUES (?, ?, ?, 'running', ?, NULL)""",
                    (
                        result.message_id,
                        result.conversation_id,
                        result.model_dump_json(),
                        _iso(result.started_at),
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                raise GatewayResultConflictError(
                    f"gateway result already exists: {result.message_id}"
                ) from exc
        return result

    def _get_result(
        self,
        message_id: str,
        scope: ProfileScope,
    ) -> GatewayInboundResult | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT result_json FROM gateway_inbound_results WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        if row is None:
            return None
        result = self._result(row["result_json"])
        if not scope.permits(result.profile_label):
            return None
        return result

    def _finish_result(
        self,
        message_id: str,
        conversation_id: str,
        expected_conversation_revision: int,
        status: Literal["committed", "failed", "uncertain"],
        session_revision: int | None,
        response_outbox_id: str | None,
        error: str | None,
        scope: ProfileScope,
    ) -> tuple[Conversation, GatewayInboundResult]:
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            conversation_row = connection.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            result_row = connection.execute(
                """SELECT result_json, status FROM gateway_inbound_results
                   WHERE message_id = ?""",
                (message_id,),
            ).fetchone()
            if conversation_row is None:
                connection.rollback()
                raise ConversationNotFoundError(f"conversation not found: {conversation_id}")
            if result_row is None:
                connection.rollback()
                raise GatewayResultConflictError(f"gateway result not found: {message_id}")
            conversation = self._conversation(conversation_row)
            self._assert_conversation_scope(conversation, scope)
            if (
                conversation.revision != expected_conversation_revision
                or conversation.status != "active"
                or result_row["status"] != "running"
            ):
                connection.rollback()
                raise GatewayResultConflictError("gateway result or conversation changed")
            result = GatewayInboundResult(
                message_id=message_id,
                conversation_id=conversation_id,
                session_id=conversation.session_id,
                profile_label=conversation.profile_scope.label(),
                status=status,
                session_revision=session_revision,
                response_outbox_id=response_outbox_id,
                error=error,
                started_at=self._result(result_row["result_json"]).started_at,
                finished_at=now,
            )
            next_status: ConversationStatus = (
                "uncertain" if status == "uncertain" else conversation.status
            )
            changed = conversation.model_copy(
                update={
                    "status": next_status,
                    "revision": conversation.revision + 1,
                    "updated_at": now,
                    "last_processed_inbound_message_id": message_id,
                }
            )
            connection.execute(
                """UPDATE conversations SET conversation_json = ?, status = ?,
                   revision = ?, updated_at = ? WHERE id = ?""",
                (
                    changed.model_dump_json(),
                    changed.status,
                    changed.revision,
                    _iso(now),
                    changed.id,
                ),
            )
            connection.execute(
                """UPDATE gateway_inbound_results SET result_json = ?, status = ?,
                   finished_at = ? WHERE message_id = ?""",
                (result.model_dump_json(), result.status, _iso(now), message_id),
            )
            connection.commit()
        return changed, result

    def _results(
        self,
        conversation_id: str,
        limit: int,
        scope: ProfileScope,
    ) -> list[GatewayInboundResult]:
        with self._connect() as connection:
            conversation_row = connection.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if conversation_row is None:
                raise ConversationNotFoundError(f"conversation not found: {conversation_id}")
            self._assert_conversation_scope(self._conversation(conversation_row), scope)
            rows = connection.execute(
                """SELECT result_json FROM gateway_inbound_results
                   WHERE conversation_id = ?
                   ORDER BY started_at DESC, message_id DESC LIMIT ?""",
                (conversation_id, limit),
            )
            return [self._result(row["result_json"]) for row in rows]

    def _running_results(self, scope: ProfileScope) -> list[GatewayInboundResult]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT result_json FROM gateway_inbound_results
                   WHERE status = 'running' ORDER BY started_at, message_id"""
            ).fetchall()
        results = (self._result(row["result_json"]) for row in rows)
        return [result for result in results if scope.permits(result.profile_label)]

    def _recover_running_result(
        self,
        message_id: str,
        error: str,
        scope: ProfileScope,
    ) -> tuple[Conversation, GatewayInboundResult]:
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result_row = connection.execute(
                """SELECT result_json, status, conversation_id FROM gateway_inbound_results
                   WHERE message_id = ?""",
                (message_id,),
            ).fetchone()
            if result_row is None:
                connection.rollback()
                raise GatewayResultConflictError(f"gateway result not found: {message_id}")
            if result_row["status"] != "running":
                connection.rollback()
                raise GatewayResultConflictError("gateway result is no longer running")
            conversation_id = str(result_row["conversation_id"])
            conversation_row = connection.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if conversation_row is None:
                connection.rollback()
                raise ConversationNotFoundError(f"conversation not found: {conversation_id}")
            conversation = self._conversation(conversation_row)
            self._assert_conversation_scope(conversation, scope)
            previous = self._result(result_row["result_json"])
            result = previous.model_copy(
                update={"status": "uncertain", "error": error, "finished_at": now}
            )
            changed = conversation.model_copy(
                update={
                    "status": "uncertain",
                    "revision": conversation.revision + 1,
                    "updated_at": now,
                }
            )
            connection.execute(
                """UPDATE conversations SET conversation_json = ?, status = ?,
                   revision = ?, updated_at = ? WHERE id = ?""",
                (
                    changed.model_dump_json(),
                    changed.status,
                    changed.revision,
                    _iso(now),
                    changed.id,
                ),
            )
            connection.execute(
                """UPDATE gateway_inbound_results SET result_json = ?, status = ?,
                   finished_at = ? WHERE message_id = ?""",
                (result.model_dump_json(), result.status, _iso(now), message_id),
            )
            connection.commit()
        return changed, result

    def _result_counts(self, scope: ProfileScope) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT result_json FROM gateway_inbound_results").fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            result = self._result(row["result_json"])
            if not scope.permits(result.profile_label):
                continue
            counts[result.status] = counts.get(result.status, 0) + 1
        return counts

    def _conversation_counts(self, scope: ProfileScope) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM conversations").fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            conversation = self._conversation(row)
            if not scope.permits(conversation.profile_scope.label()):
                continue
            counts[conversation.status] = counts.get(conversation.status, 0) + 1
        return counts

    def _prunable_results(
        self,
        keep: int,
        before: datetime,
        scope: ProfileScope,
    ) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT message_id, result_json FROM gateway_inbound_results
                   WHERE status = 'committed' AND finished_at IS NOT NULL AND finished_at < ?
                   ORDER BY finished_at DESC, message_id DESC""",
                (_iso(before),),
            ).fetchall()
        candidates = [
            str(row["message_id"])
            for row in rows
            if scope.permits(self._result(row["result_json"]).profile_label)
        ]
        return sorted(candidates[keep:])

    def _prune_results(
        self,
        message_ids: tuple[str, ...],
        scope: ProfileScope,
    ) -> int:
        if not message_ids:
            return 0
        removed = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for message_id in message_ids:
                row = connection.execute(
                    "SELECT result_json FROM gateway_inbound_results WHERE message_id = ?",
                    (message_id,),
                ).fetchone()
                if row is None:
                    continue
                self._assert_result_scope(self._result(row["result_json"]), scope)
                cursor = connection.execute(
                    "DELETE FROM gateway_inbound_results WHERE message_id = ? AND status = ?",
                    (message_id, "committed"),
                )
                removed += cursor.rowcount
            connection.commit()
        return removed

    def _prunable_conversations(
        self,
        keep: int,
        before: datetime,
        scope: ProfileScope,
    ) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT c.* FROM conversations c
                   WHERE c.status = 'archived' AND c.updated_at < ?
                     AND NOT EXISTS (
                       SELECT 1 FROM gateway_inbound_results r WHERE r.conversation_id = c.id
                     )
                   ORDER BY c.updated_at DESC, c.id DESC""",
                (_iso(before),),
            ).fetchall()
        candidates = [
            str(row["id"])
            for row in rows
            if scope.permits(self._conversation(row).profile_scope.label())
        ]
        return sorted(candidates[keep:])

    def _prune_conversations(
        self,
        conversation_ids: tuple[str, ...],
        scope: ProfileScope,
    ) -> int:
        if not conversation_ids:
            return 0
        removed = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for conversation_id in conversation_ids:
                conversation_row = connection.execute(
                    "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
                ).fetchone()
                if conversation_row is None:
                    continue
                self._assert_conversation_scope(
                    self._conversation(conversation_row),
                    scope,
                )
                linked = connection.execute(
                    """SELECT COUNT(*) AS count FROM gateway_inbound_results
                       WHERE conversation_id = ?""",
                    (conversation_id,),
                ).fetchone()
                if linked is not None and int(linked["count"]) != 0:
                    continue
                cursor = connection.execute(
                    "DELETE FROM conversations WHERE id = ? AND status = 'archived'",
                    (conversation_id,),
                )
                removed += cursor.rowcount
            connection.commit()
        return removed

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=self.settings.sqlite_busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.settings.sqlite_busy_timeout_ms}")
        return connection

    def _get_for_label(
        self,
        conversation_id: str,
        profile_label: ProfileLabel,
    ) -> Conversation:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        if row is None:
            raise ConversationNotFoundError(f"conversation not found: {conversation_id}")
        conversation = self._conversation(row)
        if not set(profile_label.required_profiles).issubset(conversation.profile_scope.profiles):
            raise ConversationNotFoundError(
                "conversation was not found for the notification profile label"
            )
        return conversation

    @staticmethod
    def _assert_conversation_scope(
        conversation: Conversation,
        scope: ProfileScope,
    ) -> None:
        if not scope.permits(conversation.profile_scope.label()):
            raise ConversationNotFoundError(
                "conversation was not found in the active profile scope"
            )

    @staticmethod
    def _assert_result_scope(result: GatewayInboundResult, scope: ProfileScope) -> None:
        if not scope.permits(result.profile_label):
            raise GatewayResultConflictError(
                "gateway result was not found in the active profile scope"
            )

    @staticmethod
    def _conversation(row: sqlite3.Row) -> Conversation:
        try:
            conversation = Conversation.model_validate_json(row["conversation_json"])
        except ValidationError as exc:
            raise GatewayStoreError("stored conversation is invalid") from exc
        if (
            conversation.id != row["id"]
            or conversation.status != row["status"]
            or conversation.revision != int(row["revision"])
        ):
            raise GatewayStoreError("stored conversation metadata is inconsistent")
        return conversation

    @staticmethod
    def _result(payload: str) -> GatewayInboundResult:
        try:
            return GatewayInboundResult.model_validate_json(payload)
        except ValidationError as exc:
            raise GatewayStoreError("stored gateway result is invalid") from exc

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("gateway store clock must return an aware datetime")
        return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()
