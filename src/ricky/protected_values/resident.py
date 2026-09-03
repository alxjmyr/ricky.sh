"""Process-resident protected-value unlock ownership.

The registry is independent of any interface or consumer.  A long-running
process may unlock explicitly selected profile backends once, then issue
scope-narrowed broker leases that share those backends without sharing an
unlock prompt or raw key material.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import AsyncExitStack

from pydantic import SecretStr

from ricky.config import RickySettings
from ricky.profiles import ProfileScope, validate_profile_name
from ricky.protected_values.backend import (
    ProtectedValueBackend,
    ProtectedValueBackendFactory,
)
from ricky.protected_values.service import (
    DestinationApprovalResponder,
    ProtectedValueBroker,
    SecureValueResponder,
    UnlockResponder,
    deny_destination,
    deny_secure_value,
    deny_unlock,
)
from ricky.protected_values.store import ProfileVaultStore, ProtectedValueStoreError


class ResidentProtectedValueRegistry:
    """Own unlocked profile backends for exactly one process lifetime."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        backend_factory: ProtectedValueBackendFactory = ProfileVaultStore,
    ) -> None:
        self._settings = settings
        self._backend_factory = backend_factory
        self._backends: dict[str, ProtectedValueBackend] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._closed = False

    @property
    def unlocked_profiles(self) -> tuple[str, ...]:
        """Return safe profile names whose resident backends are unlocked."""

        return tuple(
            sorted(profile for profile, backend in self._backends.items() if backend.unlocked)
        )

    def backend(self, profile: str) -> ProtectedValueBackend:
        """Return one enabled profile backend, creating it while still locked."""

        self._ensure_open()
        selected = validate_profile_name(profile)
        if selected not in self._settings.profiles.enabled:
            raise ProtectedValueStoreError(f"protected-value profile is unavailable: {selected}")
        backend = self._backends.get(selected)
        if backend is None:
            backend = self._backend_factory(self._settings, selected)
            self._backends[selected] = backend
        return backend

    def ensure_open(self) -> None:
        """Reject use after the process owner has closed the registry."""

        self._ensure_open()

    async def initialized(self, profile: str) -> bool:
        """Inspect vault initialization without unlocking it."""

        return await self.backend(profile).initialized()

    async def unlock(self, profile: str, passphrase: SecretStr) -> None:
        """Unlock one resident backend until this registry closes."""

        async with self._lifecycle_lock:
            self._ensure_open()
            backend = self.backend(profile)
            if not backend.unlocked:
                await backend.unlock(passphrase)

    async def unlock_many(self, passphrases: Mapping[str, SecretStr]) -> None:
        """Unlock an exact set atomically from the process owner's perspective.

        If any unlock fails or the caller is cancelled, every backend in the
        registry is locked before the failure propagates.  Startup therefore
        never leaves a partially unlocked process.
        """

        async with self._lifecycle_lock:
            self._ensure_open()
            try:
                for profile, passphrase in passphrases.items():
                    backend = self.backend(profile)
                    if not backend.unlocked:
                        await backend.unlock(passphrase)
            except BaseException:
                await self._lock_all()
                raise

    def lease(
        self,
        *,
        scope: ProfileScope,
        consumer_ids: frozenset[str] = frozenset(),
        unlock_responder: UnlockResponder = deny_unlock,
        secure_value_responder: SecureValueResponder = deny_secure_value,
        destination_responder: DestinationApprovalResponder = deny_destination,
    ) -> ProtectedValueBroker:
        """Issue one scope-enforcing broker over the resident backends."""

        self._ensure_open()
        if any(profile not in self._settings.profiles.enabled for profile in scope.profiles):
            raise ProtectedValueStoreError("protected-value broker scope is unavailable")
        return ProtectedValueBroker(
            self._settings,
            scope=scope,
            consumer_ids=consumer_ids,
            unlock_responder=unlock_responder,
            secure_value_responder=secure_value_responder,
            destination_responder=destination_responder,
            backend_provider=self,
        )

    async def aclose(self) -> None:
        """Lock every resident backend and make all issued leases unusable."""

        async with self._lifecycle_lock:
            if self._closed:
                return
            await self._lock_all()
            self._closed = True
            self._backends.clear()

    async def lock(self) -> None:
        """Relock every resident backend while keeping the registry reusable."""

        async with self._lifecycle_lock:
            self._ensure_open()
            await self._lock_all()

    async def _lock_all(self) -> None:
        stack = AsyncExitStack()
        for backend in self._backends.values():
            stack.push_async_callback(backend.lock)
        await stack.aclose()

    def _ensure_open(self) -> None:
        if self._closed:
            raise ProtectedValueStoreError("resident protected-value registry is closed")
