"""Host-local exclusive leases for configured browser resources."""

from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ricky.browser.resources import browser_lease_path
from ricky.browser.types import BrowserError, BrowserFailure
from ricky.config import RickySettings, ensure_private_user_data_root
from ricky.profiles import ProfileResourceRef

try:  # pragma: no cover - the explicit failure is exercised on non-POSIX hosts only.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


class BrowserLeaseOwner(BaseModel):
    """Safe readable metadata for the process holding a browser resource."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pid: int = Field(ge=1)
    host: str = Field(min_length=1, max_length=300)
    resource: ProfileResourceRef
    acquired_at: datetime

    @field_validator("acquired_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("acquired_at must be timezone-aware UTC")
        return value


class BrowserResourceLease:
    """Non-blocking advisory lease held for one configured resource lifetime."""

    def __init__(self, settings: RickySettings, resource: ProfileResourceRef) -> None:
        self._settings = settings
        self.resource = resource
        self.path = browser_lease_path(settings, resource)
        self._descriptor: int | None = None
        self._owner: BrowserLeaseOwner | None = None

    @property
    def held(self) -> bool:
        return self._descriptor is not None

    @property
    def owner(self) -> BrowserLeaseOwner | None:
        return self._owner

    def acquire(self, *, now: datetime | None = None) -> BrowserLeaseOwner:
        if self._descriptor is not None:
            raise RuntimeError("this browser resource lease is already held")
        if fcntl is None:
            raise BrowserError(
                BrowserFailure(
                    code="attachment_unavailable",
                    message="configured browser resource locking requires a POSIX host",
                )
            )
        current = self._current_path()
        ensure_private_user_data_root(self._settings)
        current.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        current = self._current_path()
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(current, flags, 0o600)
        except OSError as exc:
            raise BrowserError(
                BrowserFailure(
                    code="resource_busy",
                    message="configured browser resource could not be leased safely",
                )
            ) from exc
        try:
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise BrowserError(
                    BrowserFailure(
                        code="resource_busy",
                        message="configured browser resource is already in use",
                    )
                ) from exc
            owner = BrowserLeaseOwner(
                pid=os.getpid(),
                host=socket.gethostname() or "unknown",
                resource=self.resource,
                acquired_at=now or datetime.now(UTC),
            )
            payload = json.dumps(owner.model_dump(mode="json"), sort_keys=True).encode("utf-8")
            os.ftruncate(descriptor, 0)
            os.write(descriptor, payload)
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        self._owner = owner
        return owner

    def release(self) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        self._owner = None
        if descriptor is None:
            return
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    def is_active(self) -> bool:
        if self._descriptor is not None:
            return True
        if fcntl is None:
            return False
        try:
            current = self._current_path()
        except ValueError:
            return False
        if not current.exists():
            return False
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(current, flags)
        except OSError:
            return False
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return False
        finally:
            os.close(descriptor)

    def _current_path(self) -> Path:
        current = browser_lease_path(self._settings, self.resource)
        if current != self.path:
            raise ValueError("browser resource lease path changed after construction")
        return current

    def __enter__(self) -> BrowserLeaseOwner:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
