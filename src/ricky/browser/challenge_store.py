"""Scoped, versioned safe challenge metadata with atomic revision updates."""

from __future__ import annotations

import asyncio
import fcntl
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from ricky.browser.challenges import BrowserChallenge, ChallengeError
from ricky.config import RickySettings, ensure_private_user_data_root, user_data_subpath
from ricky.profiles import ProfileScope

_T = TypeVar("_T")
_ID = re.compile(r"^browser_challenge_[0-9a-f]{32}$")


class BrowserChallengeStore:
    """Browser-owned metadata; codes and live handles never enter this store.

    One installation-wide lock serializes short local compare-and-swap operations.
    Publication is an fsynced private-file replacement, joined on cancellation. The
    owner's runtime lease, not a persisted pending record, determines resumability.
    """

    def __init__(self, settings: RickySettings) -> None:
        self.settings = settings
        self.root = user_data_subpath(settings, settings.browser.challenge_dir)

    async def create(self, record: BrowserChallenge, *, scope: ProfileScope) -> None:
        self._require_scope(record, scope)
        if record.revision != 1 or record.state != "waiting_for_user":
            raise ChallengeError("new challenge requires its initial state")
        await self._call(lambda: self._write(record, scope, expected_revision=0))

    async def update(
        self,
        record: BrowserChallenge,
        expected_revision: int,
        *,
        scope: ProfileScope,
    ) -> None:
        self._require_scope(record, scope)
        await self._call(lambda: self._write(record, scope, expected_revision=expected_revision))

    async def get(self, challenge_id: str, *, scope: ProfileScope) -> BrowserChallenge:
        return await self._call(lambda: self._read(challenge_id, scope))

    async def invalidate_owner(self, owner_id: str, *, scope: ProfileScope) -> None:
        """Retire an owner only after its runtime/lease is confirmed lost.

        This is an owner recovery API, not agent-callable. Never recreate a live
        response from its inbox history. Possible dispatch stays ambiguous.
        """

        for record in await self.list(scope=scope):
            if record.binding.owner_id != owner_id:
                continue
            if record.state in {"waiting_for_user", "responded", "submitted"}:
                state = "invalidated"
            elif record.state == "submitting":
                state = "in_doubt"
            else:
                continue
            await self.update(record.transition(state), record.revision, scope=scope)

    async def list(self, *, scope: ProfileScope) -> tuple[BrowserChallenge, ...]:
        def read() -> tuple[BrowserChallenge, ...]:
            records = []
            for path in sorted(self.root.glob("browser_challenge_*.json")):
                record = self._decode(path)
                if set(record.binding.profile_scope.profiles) <= set(scope.profiles):
                    records.append(record)
            return tuple(records)

        return await self._call(read)

    def _path(self, challenge_id: str) -> Path:
        if not _ID.fullmatch(challenge_id):
            raise ChallengeError("invalid browser challenge identifier")
        return self.root / f"{challenge_id}.json"

    @staticmethod
    def _require_scope(record: BrowserChallenge, scope: ProfileScope) -> None:
        if not set(record.binding.profile_scope.profiles) <= set(scope.profiles):
            raise ChallengeError("browser challenge is unavailable in this profile scope")

    @staticmethod
    def _decode(path: Path) -> BrowserChallenge:
        if path.is_symlink() or not path.is_file():
            raise ChallengeError("browser challenge record is unavailable")
        record = BrowserChallenge.model_validate_json(path.read_bytes())
        if path.name != f"{record.id}.json":
            raise ChallengeError("browser challenge record identity differs from its path")
        return record

    def _read(self, challenge_id: str, scope: ProfileScope) -> BrowserChallenge:
        record = self._decode(self._path(challenge_id))
        self._require_scope(record, scope)
        return record

    def _write(
        self,
        record: BrowserChallenge,
        scope: ProfileScope,
        *,
        expected_revision: int,
    ) -> None:
        ensure_private_user_data_root(self.settings)
        # Re-resolve on each mutation to reject a replaced configured path.
        if user_data_subpath(self.settings, self.settings.browser.challenge_dir) != self.root:
            raise ChallengeError("browser challenge storage path changed")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        lock_fd = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            path = self._path(record.id)
            if expected_revision == 0:
                if path.exists() or path.is_symlink():
                    raise ChallengeError("browser challenge already exists")
            else:
                previous = self._read(record.id, scope)
                if (
                    previous.revision != expected_revision
                    or record.revision != expected_revision + 1
                ):
                    raise ChallengeError("browser challenge revision conflict")
                if previous.binding != record.binding:
                    raise ChallengeError("browser challenge binding is immutable")
                if (
                    previous.model_copy(
                        update={
                            "state": record.state,
                            "revision": record.revision,
                            "source": record.source,
                        }
                    )
                    != record
                ):
                    raise ChallengeError("browser challenge metadata is immutable")
                if previous.source != record.source:
                    if previous.source is not None or previous.state != "waiting_for_user":
                        raise ChallengeError("browser challenge source is already bound")
                    if record.state != previous.state:
                        raise ChallengeError("source binding cannot change challenge state")
                elif previous.transition(record.state) != record:
                    raise ChallengeError("invalid browser challenge transition")
            fd, staged_name = tempfile.mkstemp(prefix=".challenge-", dir=self.root)
            staged = Path(staged_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(record.model_dump_json())
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(staged, path)
                directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                staged.unlink(missing_ok=True)
        finally:
            os.close(lock_fd)

    @staticmethod
    async def _call(operation: Callable[[], _T]) -> _T:
        task = asyncio.create_task(asyncio.to_thread(operation))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
