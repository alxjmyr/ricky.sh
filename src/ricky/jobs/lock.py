"""POSIX single-flight locks for named jobs and transcript retention."""

from __future__ import annotations

import fcntl
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ricky.jobs.spec import validate_job_name
from ricky.profiles import ProfileResourceRef

_BROWSER_WORKER_ID = re.compile(r"^browser-worker-v1-([0-9a-f]{32})$")
_BROWSER_WORKER_LEASES: dict[tuple[Path, int], BrowserWorkerLease] = {}
_BROWSER_WORKER_LEASES_LOCK = threading.Lock()


class FileLock:
    """One non-blocking advisory lock backed by a distinct file descriptor."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._descriptor: int | None = None

    def acquire(self) -> bool:
        """Acquire without waiting, returning false when another process holds it."""

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return False
        self._descriptor = descriptor
        return True

    def release(self) -> None:
        """Release and close the owned descriptor."""

        descriptor, self._descriptor = self._descriptor, None
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def __enter__(self) -> FileLock:
        if not self.acquire():
            raise BlockingIOError(f"lock is already held: {self.path}")
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


def job_lock(run_root: Path, name: str) -> FileLock:
    """Construct the lock for one profile-qualified job identity."""

    profile, separator, local_name = name.partition("/")
    if not separator:
        raise ValueError("job lock identity must be profile-qualified")
    ProfileResourceRef(profile=profile, name=validate_job_name(local_name))
    return FileLock(run_root / "locks" / f"{profile}--{local_name}.lock")


@dataclass
class BrowserWorkerLease:
    """Process-lifetime proof backing a durable browser-attempt worker id."""

    identity: str
    lock: FileLock

    def release(self) -> None:
        """Release the liveness proof after the attempt has terminalized."""

        self.lock.release()
        self.lock.path.unlink(missing_ok=True)


def browser_worker_identity(run_root: Path) -> str:
    """Return the process-resident browser worker identity for one run root."""

    key = (run_root.resolve(), os.getpid())
    with _BROWSER_WORKER_LEASES_LOCK:
        lease = _BROWSER_WORKER_LEASES.get(key)
        if lease is None:
            lease = browser_worker_lease(run_root)
            _BROWSER_WORKER_LEASES[key] = lease
        return lease.identity


def browser_worker_lease(run_root: Path) -> BrowserWorkerLease:
    """Acquire one unforgeable local-process liveness proof for a browser owner."""

    token = uuid4().hex
    identity = f"browser-worker-v1-{token}"
    lock = FileLock(_browser_worker_path(run_root, token))
    if not lock.acquire():  # A fresh random identity cannot legitimately collide.
        raise RuntimeError("fresh browser worker identity is already in use")
    return BrowserWorkerLease(identity=identity, lock=lock)


def browser_worker_is_alive(run_root: Path, identity: str) -> bool | None:
    """Return local worker liveness, or ``None`` for a legacy/foreign identity.

    Advisory locks are released by the kernel when a worker process exits, so
    this does not confuse a reused PID with the original browser owner.
    """

    match = _BROWSER_WORKER_ID.fullmatch(identity)
    if match is None:
        return None
    path = _browser_worker_path(run_root, match.group(1))
    try:
        descriptor = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _browser_worker_path(run_root: Path, token: str) -> Path:
    return run_root / "locks" / "browser-workers" / f"{token}.lock"
