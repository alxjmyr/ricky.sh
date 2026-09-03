"""Atomic user-global schedule desired-state persistence."""

from __future__ import annotations

import asyncio
import fcntl
import os
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import tomlkit
from pydantic import ValidationError

from ricky.config import RickySettings, user_data_path
from ricky.profiles import ProfileScope
from ricky.schedules.types import ScheduleFile, ScheduleSpec

_T = TypeVar("_T")


class ScheduleStoreError(RuntimeError):
    """A bounded schedules.toml persistence failure."""


class ScheduleStore:
    """Purpose-specific async CRUD over canonical schedules.toml."""

    def __init__(self, settings: RickySettings, *, scope: ProfileScope) -> None:
        self.root = user_data_path(settings)
        self.scope = scope
        self.path = self.root / "schedules.toml"
        self.lock_path = self.root / "cron" / "lock"

    async def list(self) -> list[ScheduleSpec]:
        schedules = await asyncio.to_thread(self._read_sync)
        return [item for item in schedules if self.scope.permits(item.profile_scope.label())]

    async def get(self, schedule_id: str) -> ScheduleSpec:
        schedules = await self.list()
        for schedule in schedules:
            if schedule.id == schedule_id:
                return schedule
        raise ScheduleStoreError(f"schedule not found: {schedule_id}")

    async def create(self, schedule: ScheduleSpec) -> ScheduleSpec:
        self._require(schedule)

        def mutate(items: list[ScheduleSpec]) -> tuple[list[ScheduleSpec], ScheduleSpec]:
            if any(item.id == schedule.id for item in items):
                raise ScheduleStoreError(f"schedule already exists: {schedule.id}")
            return [*items, schedule], schedule

        return await asyncio.to_thread(self._mutate_sync, mutate)

    async def replace(self, schedule: ScheduleSpec) -> ScheduleSpec:
        self._require(schedule)

        def mutate(items: list[ScheduleSpec]) -> tuple[list[ScheduleSpec], ScheduleSpec]:
            current = next((item for item in items if item.id == schedule.id), None)
            if current is None:
                raise ScheduleStoreError(f"schedule not found: {schedule.id}")
            self._require(current)
            return [schedule if item.id == schedule.id else item for item in items], schedule

        return await asyncio.to_thread(self._mutate_sync, mutate)

    async def remove(self, schedule_id: str) -> ScheduleSpec:
        def mutate(items: list[ScheduleSpec]) -> tuple[list[ScheduleSpec], ScheduleSpec]:
            removed = next((item for item in items if item.id == schedule_id), None)
            if removed is None:
                raise ScheduleStoreError(f"schedule not found: {schedule_id}")
            self._require(removed)
            return [item for item in items if item.id != schedule_id], removed

        return await asyncio.to_thread(self._mutate_sync, mutate)

    def _require(self, schedule: ScheduleSpec) -> None:
        if not self.scope.permits(schedule.profile_scope.label()):
            raise ScheduleStoreError(f"schedule is outside the active profile scope: {schedule.id}")

    def _mutate_sync(
        self,
        operation: Callable[[list[ScheduleSpec]], tuple[list[ScheduleSpec], _T]],
    ) -> _T:
        descriptor = self._lock()
        try:
            schedules = self._read_sync()
            updated, result = operation(schedules)
            self._write_sync(updated)
            return result
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read_sync(self) -> list[ScheduleSpec]:
        if not self.path.exists():
            return []
        try:
            payload = tomllib.loads(self.path.read_text(encoding="utf-8"))
            document = ScheduleFile.model_validate(payload)
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValidationError) as exc:
            raise ScheduleStoreError(f"invalid schedule store {self.path}: {exc}") from exc
        return sorted(document.schedules, key=lambda item: item.id)

    def _write_sync(self, schedules: list[ScheduleSpec]) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        document = tomlkit.document()
        document["version"] = 1
        entries = tomlkit.aot()
        for schedule in sorted(schedules, key=lambda item: item.id):
            table = tomlkit.table()
            for key, value in schedule.model_dump(mode="python", exclude_none=True).items():
                table[key] = value
            entries.append(table)
        document["schedules"] = entries
        encoded = tomlkit.dumps(document).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(prefix=".schedules-", dir=self.root)
        temporary_path = Path(temporary)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            os.chmod(self.path, 0o600)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def _lock(self) -> int:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.lock_path.parent, 0o700)
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor
