"""Bounded, protective pruning of finished gateway evidence.

Retention can only ever remove a record that is simultaneously terminal, old
enough, beyond its configured ceiling, and unreferenced by anything unresolved.
The protection set is computed first and subtracted from every candidate list,
so a configuration mistake shrinks what is deleted rather than widening it.

``plan`` is read-only and lists exact ids and paths. ``apply`` performs only the
planned deletions and requires an explicit operator command. Both are idempotent
and cancellation-safe: every delete is conditional on the record still being
terminal, so a partial run simply resumes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ricky.authority.store import AuthorityStore
from ricky.config import RickySettings, user_data_subpath
from ricky.executions.store import ExecutionStore
from ricky.gateway.store import GatewayStore
from ricky.messaging.store import MessagingStore
from ricky.notifications.store import NotificationStore
from ricky.profiles import ProfileScope


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RetentionGroup(_FrozenModel):
    """Exactly what one category would remove, and what protected it."""

    name: str = Field(min_length=1, max_length=100)
    keep: int = Field(ge=0)
    removable_ids: tuple[str, ...] = ()
    removable_paths: tuple[str, ...] = ()
    protected_ids: tuple[str, ...] = ()
    removed: int = Field(default=0, ge=0)


class RetentionPlan(_FrozenModel):
    """The complete retention decision for one user data root."""

    generated_at: datetime
    applied: bool
    enabled: bool
    groups: tuple[RetentionGroup, ...] = ()

    @field_validator("generated_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("generated_at must be timezone-aware UTC")
        return value

    @property
    def total_removable(self) -> int:
        """Count every record and file this plan would remove."""

        return sum(len(group.removable_ids) + len(group.removable_paths) for group in self.groups)

    def group(self, name: str) -> RetentionGroup | None:
        """Return one named category, or None when it is absent."""

        for group in self.groups:
            if group.name == name:
                return group
        return None


class GatewayRetention:
    """Compute and optionally apply protective pruning across every store."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        scope: ProfileScope,
        messaging: MessagingStore | None = None,
        notifications: NotificationStore | None = None,
        gateway: GatewayStore | None = None,
        executions: ExecutionStore | None = None,
        authority: AuthorityStore | None = None,
    ) -> None:
        self.settings = settings
        self.profile_scope = scope
        self.config = settings.gateway.retention
        self.messaging = messaging or MessagingStore(settings)
        self.notifications = notifications or NotificationStore(settings)
        self.gateway = gateway or GatewayStore(settings)
        self.executions = executions or ExecutionStore(settings)
        self.authority = authority or AuthorityStore(settings)

    async def plan(self, *, now: datetime | None = None) -> RetentionPlan:
        """List exactly what pruning would remove. Changes no state."""

        return await self._run(apply_changes=False, now=now)

    async def apply(self, *, now: datetime | None = None) -> RetentionPlan:
        """Remove exactly the planned records. Requires an explicit command."""

        if not self.config.enabled:
            raise ValueError("gateway.retention.enabled must be true to apply pruning")
        return await self._run(apply_changes=True, now=now)

    async def _run(self, *, apply_changes: bool, now: datetime | None) -> RetentionPlan:
        moment = now or datetime.now(UTC)
        cutoff = moment - timedelta(seconds=self.config.min_age_seconds)
        await self._initialize()
        protected = await self._protected_message_ids()
        groups: list[RetentionGroup] = []

        inbox_ids = await self.messaging.prunable_inbox(
            keep=self.config.inbound_messages, before=cutoff, protected=protected
        )
        groups.append(
            await self._group(
                "inbound_messages",
                self.config.inbound_messages,
                inbox_ids,
                protected,
                apply_changes,
                self.messaging.prune_inbox,
            )
        )

        result_ids = await self.gateway.prunable_results(
            scope=self.profile_scope,
            keep=self.config.turn_results,
            before=cutoff,
        )
        result_ids = [item for item in result_ids if item not in set(protected)]
        groups.append(
            await self._group(
                "turn_results",
                self.config.turn_results,
                result_ids,
                protected,
                apply_changes,
                lambda ids: self.gateway.prune_results(ids, scope=self.profile_scope),
            )
        )

        conversation_ids = await self.gateway.prunable_conversations(
            scope=self.profile_scope,
            keep=self.config.archived_conversations,
            before=cutoff,
        )
        protected_conversations = await self.notifications.unresolved_conversation_ids(
            scope=self.profile_scope
        )
        conversation_ids = [
            item for item in conversation_ids if item not in set(protected_conversations)
        ]
        groups.append(
            await self._group(
                "archived_conversations",
                self.config.archived_conversations,
                conversation_ids,
                protected_conversations,
                apply_changes,
                lambda ids: self.gateway.prune_conversations(
                    ids,
                    scope=self.profile_scope,
                ),
            )
        )

        execution_ids = await self.executions.prunable(
            scope=self.profile_scope,
            keep=self.config.execution_requests,
            before=cutoff,
        )
        protected_executions = await self._protected_execution_ids()
        execution_ids = [item for item in execution_ids if item not in protected_executions]
        groups.append(
            await self._group(
                "execution_requests",
                self.config.execution_requests,
                execution_ids,
                protected_executions,
                apply_changes,
                lambda ids: self.executions.prune(ids, scope=self.profile_scope),
            )
        )

        protected_outbox = await self._protected_outbox_ids()
        notification_ids = await self.notifications.prunable_notifications(
            scope=self.profile_scope,
            keep=self.config.notifications,
            before=cutoff,
            protected=protected_outbox,
        )
        groups.append(
            await self._group(
                "notifications",
                self.config.notifications,
                notification_ids,
                protected_outbox,
                apply_changes,
                lambda ids: self.notifications.prune_notifications(
                    ids,
                    scope=self.profile_scope,
                ),
            )
        )

        groups.append(self._log_group(cutoff, apply_changes))
        return RetentionPlan(
            generated_at=moment,
            applied=apply_changes,
            enabled=self.config.enabled,
            groups=tuple(groups),
        )

    async def _initialize(self) -> None:
        await self.messaging.initialize()
        await self.notifications.initialize()
        await self.gateway.initialize()
        await self.executions.initialize()
        await self.authority.initialize()

    async def _group(
        self,
        name: str,
        keep: int,
        ids: list[str],
        protected: tuple[str, ...] | list[str],
        apply_changes: bool,
        prune: Callable[[Sequence[str]], Awaitable[int]],
    ) -> RetentionGroup:
        removed = 0
        if apply_changes and ids:
            removed = int(await prune(ids))
        return RetentionGroup(
            name=name,
            keep=keep,
            removable_ids=tuple(ids),
            protected_ids=tuple(protected),
            removed=removed,
        )

    async def _protected_message_ids(self) -> tuple[str, ...]:
        """Collect every inbound id an unresolved record still depends on."""

        protected: set[str] = set()
        protected.update(await self.messaging.protected_inbox_ids())
        for result in await self.gateway.running_results(scope=self.profile_scope):
            protected.add(result.message_id)
        protected.update(await self.executions.protected_message_ids(scope=self.profile_scope))
        return tuple(sorted(protected))

    async def _protected_execution_ids(self) -> tuple[str, ...]:
        """Protect request/grant/contract chains that are not fully disposable."""

        protected = set(await self.authority.active_execution_request_ids(scope=self.profile_scope))
        protected.update(
            await self.executions.protected_parent_request_ids(scope=self.profile_scope)
        )
        return tuple(sorted(protected))

    async def _protected_outbox_ids(self) -> tuple[str, ...]:
        """Collect every outbox id that is unresolved or still referenced."""

        return await self.notifications.unresolved_outbox_ids(scope=self.profile_scope)

    def _log_group(self, cutoff: datetime, apply_changes: bool) -> RetentionGroup:
        """Bound the private service log directory by byte and file count."""

        directory = user_data_subpath(self.settings, self.settings.gateway.service.log_dir)
        if not directory.is_dir():
            return RetentionGroup(name="service_logs", keep=self.config.log_file_limit)
        files = sorted(
            (item for item in directory.iterdir() if item.is_file()),
            key=lambda item: (item.stat().st_mtime, item.name),
            reverse=True,
        )
        removable: list[Path] = list(files[self.config.log_file_limit :])
        total = 0
        for item in files[: self.config.log_file_limit]:
            total += item.stat().st_size
            if total > self.config.log_byte_limit:
                removable.append(item)
        removed = 0
        if apply_changes:
            for item in removable:
                try:
                    item.unlink()
                except OSError:  # pragma: no cover - a vanished file is already pruned
                    continue
                removed += 1
        return RetentionGroup(
            name="service_logs",
            keep=self.config.log_file_limit,
            removable_paths=tuple(sorted(str(item) for item in removable)),
            removed=removed,
        )
