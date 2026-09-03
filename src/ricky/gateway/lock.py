"""Host-local single-instance lock for one ``user_data_dir``.

One running gateway owns one user data root. The lock is an advisory POSIX
``flock`` on a private file that also carries readable owner metadata, so an
operator can see which process holds it without attaching a debugger.

This is deliberately not distributed leader election. It protects one host from
running two gateways against one SQLite root. A lock file left behind by a
killed process is harmless: ``flock`` is released by the kernel when the owning
file descriptor closes, so a stale file with no live holder is acquirable.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ricky.config import (
    RickySettings,
    ensure_private_user_data_root,
    user_data_path,
    user_data_subpath,
)

try:  # pragma: no cover - the fallback is exercised only on non-POSIX hosts.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


class GatewayLockError(RuntimeError):
    """The single-instance gateway lock could not be acquired or inspected."""


class LockOwner(BaseModel):
    """Readable metadata about the process that holds the gateway lock."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pid: int = Field(ge=1)
    host: str = Field(min_length=1, max_length=300)
    user_data_dir: str = Field(min_length=1, max_length=2_000)
    acquired_at: datetime

    @field_validator("acquired_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("acquired_at must be timezone-aware UTC")
        return value


class GatewayLock:
    """Acquire one advisory exclusive lock for the configured user data root."""

    def __init__(self, settings: RickySettings) -> None:
        self.settings = settings
        self.root = user_data_path(settings)
        self.path = user_data_subpath(settings, settings.gateway.lock_path)
        self._descriptor: int | None = None
        self._owner: LockOwner | None = None

    @property
    def held(self) -> bool:
        """Report whether this object currently owns the lock."""

        return self._descriptor is not None

    @property
    def owner(self) -> LockOwner | None:
        """Return the metadata this object wrote when it acquired the lock."""

        return self._owner

    def acquire(self, *, now: datetime | None = None) -> LockOwner:
        """Take the lock or raise if another live process already holds it."""

        if self._descriptor is not None:
            raise GatewayLockError("this gateway lock is already held")
        ensure_private_user_data_root(self.settings)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            self._lock(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        owner = LockOwner(
            pid=os.getpid(),
            host=socket.gethostname() or "unknown",
            user_data_dir=str(self.root),
            acquired_at=now or datetime.now(UTC),
        )
        payload = json.dumps(owner.model_dump(mode="json"), sort_keys=True)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, payload.encode("utf-8"))
        os.fsync(descriptor)
        self._descriptor = descriptor
        self._owner = owner
        return owner

    def release(self) -> None:
        """Release the lock and leave the readable owner record in place."""

        descriptor = self._descriptor
        self._descriptor = None
        self._owner = None
        if descriptor is None:
            return
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    def read_owner(self) -> LockOwner | None:
        """Read the recorded owner without taking the lock. Never raises on drift."""

        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return None
        if not text.strip():
            return None
        try:
            return LockOwner.model_validate_json(text)
        except ValueError:
            return None

    def is_active(self) -> bool:
        """Report whether some live process currently holds this lock."""

        if self._descriptor is not None:
            return True
        if not self.path.exists():
            return False
        if fcntl is None:  # pragma: no cover - non-POSIX hosts cannot probe.
            return False
        descriptor = os.open(self.path, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return False
        finally:
            os.close(descriptor)

    def _lock(self, descriptor: int) -> None:
        if fcntl is None:  # pragma: no cover - non-POSIX hosts get no exclusion.
            raise GatewayLockError("single-instance gateway locking requires a POSIX host")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            existing = self.read_owner()
            detail = (
                f" held by pid {existing.pid} on {existing.host} since "
                f"{existing.acquired_at.isoformat()}"
                if existing is not None
                else ""
            )
            raise GatewayLockError(f"another gateway already owns {self.root}{detail}") from exc

    def __enter__(self) -> LockOwner:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def lock_path(settings: RickySettings) -> Path:
    """Resolve the configured lock file below ``user_data_dir``."""

    return user_data_subpath(settings, settings.gateway.lock_path)
