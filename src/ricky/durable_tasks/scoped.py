"""Scope-enforcing facade over profile-owned durable task stores."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from ricky.config import RickySettings
from ricky.durable_tasks.artifacts import TaskArtifactStore
from ricky.durable_tasks.store import DurableTaskStore, TaskNotFoundError, TaskStoreError
from ricky.durable_tasks.types import (
    DurableTask,
    TaskActivity,
    TaskArtifactEntry,
    TaskSearchQuery,
)
from ricky.profiles import ProfileName, ProfileScope, validate_profile_name


class ScopedDurableTaskStore:
    """Route task operations only among stores in an immutable profile scope."""

    def __init__(
        self,
        *,
        scope: ProfileScope,
        stores: dict[str, DurableTaskStore],
    ) -> None:
        self.profile_scope = scope
        self._stores = stores
        self._owners: dict[str, str] = {}

    @classmethod
    async def create(
        cls,
        settings: RickySettings,
        *,
        scope: ProfileScope,
    ) -> ScopedDurableTaskStore:
        """Open exactly the profile stores visible to ``scope``."""

        if any(profile not in settings.profiles.enabled for profile in scope.profiles):
            raise ValueError("task profile scope contains an unknown or disabled profile")
        opened = await asyncio.gather(
            *(DurableTaskStore.create(settings, profile=profile) for profile in scope.profiles)
        )
        return cls(scope=scope, stores=dict(zip(scope.profiles, opened, strict=True)))

    @property
    def search_limit(self) -> int:
        return min(store.search_limit for store in self._stores.values())

    @property
    def primary_profile(self) -> ProfileName:
        return self.profile_scope.primary

    def _select_profile(self, profile: str | None) -> str:
        selected = validate_profile_name(profile or self.profile_scope.primary)
        if not self.profile_scope.includes(selected):
            raise ValueError(f"task profile is outside the active scope: {selected}")
        return selected

    async def create_task(self, *, profile: str | None = None, **kwargs: Any) -> DurableTask:
        """Create in an explicit profile, defaulting ordinary writes to the primary."""

        store = self._stores[self._select_profile(profile)]
        task = await store.create_task(**kwargs)
        self._owners[task.id] = task.profile
        return task

    async def search(
        self,
        query: TaskSearchQuery | None = None,
        *,
        profiles: list[str] | tuple[str, ...] = (),
    ) -> list[DurableTask]:
        """Search selected accessible profiles and return globally ordered labelled tasks."""

        query = query or TaskSearchQuery(limit=self.search_limit)
        selected = (
            tuple(self._select_profile(profile) for profile in profiles)
            if profiles
            else self.profile_scope.profiles
        )
        if len(selected) != len(set(selected)):
            raise ValueError("task search profiles must be unique")
        per_store_limit = min(self.search_limit, query.limit + query.offset)
        local_query = query.model_copy(update={"offset": 0, "limit": per_store_limit})
        pages = await asyncio.gather(
            *(self._stores[profile].search(local_query) for profile in selected)
        )
        tasks = [task for page in pages for task in page]
        for task in tasks:
            self._owners[task.id] = task.profile
        tasks.sort(key=_task_sort_key)
        return tasks[query.offset : query.offset + query.limit]

    async def owner_profile(self, task_id: str) -> ProfileName:
        store = await self._store_for_task(task_id)
        return store.profile

    async def get_task(self, task_id: str) -> DurableTask:
        store = await self._store_for_task(task_id)
        return await store.get_task(task_id)

    async def activities(self, task_id: str, **kwargs: Any) -> list[TaskActivity]:
        store = await self._store_for_task(task_id)
        return await store.activities(task_id, **kwargs)

    async def claim(self, task_id: str, **kwargs: Any) -> DurableTask:
        return await (await self._store_for_task(task_id)).claim(task_id, **kwargs)

    async def renew(self, task_id: str, **kwargs: Any) -> DurableTask:
        return await (await self._store_for_task(task_id)).renew(task_id, **kwargs)

    async def progress(self, task_id: str, **kwargs: Any) -> DurableTask:
        return await (await self._store_for_task(task_id)).progress(task_id, **kwargs)

    async def wait(self, task_id: str, **kwargs: Any) -> DurableTask:
        return await (await self._store_for_task(task_id)).wait(task_id, **kwargs)

    async def block(self, task_id: str, **kwargs: Any) -> DurableTask:
        return await (await self._store_for_task(task_id)).block(task_id, **kwargs)

    async def complete(self, task_id: str, **kwargs: Any) -> DurableTask:
        return await (await self._store_for_task(task_id)).complete(task_id, **kwargs)

    async def cancel(self, task_id: str, **kwargs: Any) -> DurableTask:
        return await (await self._store_for_task(task_id)).cancel(task_id, **kwargs)

    async def reopen(self, task_id: str, **kwargs: Any) -> DurableTask:
        return await (await self._store_for_task(task_id)).reopen(task_id, **kwargs)

    async def release(self, task_id: str, **kwargs: Any) -> DurableTask:
        return await (await self._store_for_task(task_id)).release(task_id, **kwargs)

    async def update_tags(self, task_id: str, **kwargs: Any) -> DurableTask:
        return await (await self._store_for_task(task_id)).update_tags(task_id, **kwargs)

    async def record_artifact_activity(self, task_id: str, **kwargs: Any) -> DurableTask:
        return await (await self._store_for_task(task_id)).record_artifact_activity(
            task_id, **kwargs
        )

    async def mutate_artifact(self, task_id: str, **kwargs: Any) -> Any:
        return await (await self._store_for_task(task_id)).mutate_artifact(task_id, **kwargs)

    async def verify_lease(self, task_id: str, **kwargs: Any) -> DurableTask:
        return await (await self._store_for_task(task_id)).verify_lease(task_id, **kwargs)

    async def release_session_leases(self, session_id: str) -> list[str]:
        released = await asyncio.gather(
            *(store.release_session_leases(session_id) for store in self._stores.values())
        )
        return [task_id for profile_tasks in released for task_id in profile_tasks]

    async def _store_for_task(self, task_id: str) -> DurableTaskStore:
        cached = self._owners.get(task_id)
        if cached is not None:
            return self._stores[cached]
        matches: list[DurableTaskStore] = []
        for store in self._stores.values():
            try:
                task = await store.get_task(task_id)
            except TaskNotFoundError:
                continue
            matches.append(store)
            self._owners[task.id] = task.profile
        if not matches:
            raise TaskNotFoundError(f"durable task not found in active profile scope: {task_id}")
        if len(matches) > 1:
            raise TaskStoreError(f"duplicate durable task id across active profiles: {task_id}")
        return matches[0]


class ScopedTaskArtifactStore:
    """Route task artifact operations through their in-scope owning task profile."""

    def __init__(self, tasks: ScopedDurableTaskStore) -> None:
        self._tasks = tasks
        self._stores = {
            profile: TaskArtifactStore(store) for profile, store in tasks._stores.items()
        }

    async def _store(self, task_id: str) -> TaskArtifactStore:
        profile = await self._tasks.owner_profile(task_id)
        return self._stores[profile]

    async def list(self, task_id: str) -> list[TaskArtifactEntry]:
        return await (await self._store(task_id)).list(task_id)

    async def inspect(self, task_id: str, path: str) -> TaskArtifactEntry:
        return await (await self._store(task_id)).inspect(task_id, path)

    async def read(self, task_id: str, path: str, **kwargs: Any) -> Any:
        return await (await self._store(task_id)).read(task_id, path, **kwargs)

    async def write(self, task_id: str, path: str, content: str, **kwargs: Any) -> Any:
        return await (await self._store(task_id)).write(task_id, path, content, **kwargs)

    async def exact_edit(self, task_id: str, path: str, **kwargs: Any) -> Any:
        return await (await self._store(task_id)).exact_edit(task_id, path, **kwargs)


def _task_sort_key(task: DurableTask) -> tuple[object, ...]:
    due = task.due_at or datetime.max.replace(tzinfo=UTC)
    return (-task.priority, task.due_at is None, due, -task.updated_at.timestamp(), task.id)
