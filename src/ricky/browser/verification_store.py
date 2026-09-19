"""Durable one-use source claims, containing no message bodies or OTP values."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from ricky.browser.challenge_store import BrowserChallengeStore
from ricky.browser.challenges import ChallengeError
from ricky.browser.types import BrowserModel
from ricky.config import RickySettings, ensure_private_user_data_root, user_data_subpath
from ricky.profiles import ProfileResourceRef, ProfileScope


class VerificationSourceClaim(BrowserModel):
    version: Literal[1] = 1
    profile_scope: ProfileScope
    account: ProfileResourceRef
    message_id: str = Field(min_length=1, max_length=200)
    challenge_id: str = Field(pattern=r"^browser_challenge_[0-9a-f]{32}$")
    claimed_at: AwareDatetime

    @model_validator(mode="after")
    def _scope(self) -> VerificationSourceClaim:
        if self.account.profile not in self.profile_scope.profiles:
            raise ValueError("verification claim account is outside its scope")
        return self


class VerificationClaimStore:
    """Claiming never authorizes browser dispatch and cannot be undone for retry."""

    def __init__(self, settings: RickySettings) -> None:
        self.settings = settings
        self.root = user_data_subpath(settings, settings.browser.challenge_dir) / "sources"

    @staticmethod
    def _name(account: ProfileResourceRef, message_id: str) -> str:
        identity = json.dumps([account.qualified, message_id], separators=(",", ":"))
        return hashlib.sha256(identity.encode()).hexdigest() + ".json"

    async def claimed(
        self, account: ProfileResourceRef, message_id: str, *, scope: ProfileScope
    ) -> bool:
        self._require_scope(account, scope)
        return await BrowserChallengeStore._call(
            lambda: (self.root / self._name(account, message_id)).exists()
        )

    async def claim(self, claim: VerificationSourceClaim, *, scope: ProfileScope) -> bool:
        self._require_scope(claim.account, scope)
        if claim.profile_scope != scope:
            raise ChallengeError("verification source claim scope differs from its owner")
        return await BrowserChallengeStore._call(lambda: self._claim(claim, scope))

    def _claim(self, claim: VerificationSourceClaim, scope: ProfileScope) -> bool:
        ensure_private_user_data_root(self.settings)
        expected = user_data_subpath(self.settings, self.settings.browser.challenge_dir) / "sources"
        if expected != self.root or self.root.is_symlink():
            raise ChallengeError("verification claim storage path changed")
        challenge = BrowserChallengeStore(self.settings)._read(claim.challenge_id, scope)
        if challenge.state != "waiting_for_user" or datetime.now(UTC) >= challenge.expires_at:
            raise ChallengeError("verification challenge no longer accepts source claims")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        lock = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = self.root / self._name(claim.account, claim.message_id)
            if path.exists() or path.is_symlink():
                return False
            fd, filename = tempfile.mkstemp(prefix=".claim-", dir=self.root)
            staged = Path(filename)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(claim.model_dump_json())
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
            return True
        finally:
            os.close(lock)

    @staticmethod
    def _require_scope(account: ProfileResourceRef, scope: ProfileScope) -> None:
        if account.profile not in scope.profiles:
            raise ChallengeError("verification source is outside the issued profile scope")

    @classmethod
    def validate_file(cls, path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise ValueError("verification source claim is not a regular file")
        record = VerificationSourceClaim.model_validate_json(path.read_bytes())
        if cls._name(record.account, record.message_id) != path.name:
            raise ValueError("verification source claim identity differs from its path")
