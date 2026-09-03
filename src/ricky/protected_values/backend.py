"""Implementation-neutral storage contract for protected values."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from pydantic import SecretStr

from ricky.config import RickySettings
from ricky.profiles import ProfileResourceRef
from ricky.protected_values.types import (
    ProtectedCommitRecord,
    ProtectedCommitRequest,
    ProtectedDestinationApproval,
    ProtectedDestinationPolicy,
    ProtectedFieldDescriptor,
    ProtectedUseRecord,
    ProtectedUseRequest,
    ProtectedValueDescriptor,
    ProtectedValueKind,
    StoredSecretPayload,
)


class ProtectedValueBackend(Protocol):
    """One profile-owned persistence and unlock implementation.

    Raw payload access remains an internal broker/backend operation. Consumer
    tools depend on the broker and never receive this backend directly.
    """

    profile: str

    @property
    def unlocked(self) -> bool: ...

    async def initialized(self) -> bool: ...

    async def initialize(self, passphrase: SecretStr) -> None: ...

    async def unlock(self, passphrase: SecretStr) -> None: ...

    async def lock(self) -> None: ...

    async def rotate_passphrase(self, new_passphrase: SecretStr) -> None: ...

    async def list(
        self,
        *,
        limit: int,
        kind: ProtectedValueKind | None = None,
    ) -> list[ProtectedValueDescriptor]: ...

    async def count(self) -> int: ...

    async def get(self, ref: ProfileResourceRef) -> ProtectedValueDescriptor: ...

    async def create(
        self,
        *,
        name: str,
        kind: ProtectedValueKind,
        label: str,
        description: str,
        fields: tuple[ProtectedFieldDescriptor, ...],
        policy: ProtectedDestinationPolicy,
        values: Mapping[str, SecretStr],
    ) -> ProtectedValueDescriptor: ...

    async def replace(
        self,
        descriptor: ProtectedValueDescriptor,
        *,
        expected_revision: int,
        label: str,
        description: str,
        fields: tuple[ProtectedFieldDescriptor, ...],
        policy: ProtectedDestinationPolicy,
        values: Mapping[str, SecretStr],
    ) -> ProtectedValueDescriptor: ...

    async def set_enabled(
        self,
        ref: ProfileResourceRef,
        *,
        expected_revision: int,
        enabled: bool,
    ) -> ProtectedValueDescriptor: ...

    async def delete(self, ref: ProfileResourceRef, *, expected_revision: int) -> None: ...

    async def read_all(self, descriptor: ProtectedValueDescriptor) -> StoredSecretPayload: ...

    async def approve(
        self,
        ref: ProfileResourceRef,
        *,
        top_level_origin: str,
        frame_origin: str,
    ) -> ProtectedDestinationApproval: ...

    async def revoke_approval(
        self,
        ref: ProfileResourceRef,
        *,
        top_level_origin: str,
        frame_origin: str,
    ) -> bool: ...

    async def approvals(
        self,
        ref: ProfileResourceRef,
        *,
        limit: int,
    ) -> list[ProtectedDestinationApproval]: ...

    async def is_approved(
        self,
        ref: ProfileResourceRef,
        *,
        top_level_origin: str,
        frame_origin: str,
    ) -> bool: ...

    async def reserve(
        self,
        request: ProtectedUseRequest,
        *,
        revision: int,
        materialization_limit: int | None = None,
    ) -> ProtectedUseRecord: ...

    async def finalize(
        self,
        record: ProtectedUseRecord,
        *,
        disposition: str,
    ) -> ProtectedUseRecord: ...

    async def uses(self, *, limit: int) -> list[ProtectedUseRecord]: ...

    async def reserve_commit(
        self,
        request: ProtectedCommitRequest,
        *,
        commit_limit: int,
    ) -> ProtectedCommitRecord: ...

    async def finalize_commit(
        self,
        record: ProtectedCommitRecord,
        *,
        disposition: str,
    ) -> ProtectedCommitRecord: ...


class ProtectedValueBackendFactory(Protocol):
    """Construct one profile-owned backend without discovering wider scope."""

    def __call__(self, settings: RickySettings, profile: str) -> ProtectedValueBackend: ...


class ProtectedValueBackendProvider(Protocol):
    """Provide process-resident profile backends without widening caller scope."""

    def ensure_open(self) -> None: ...

    def backend(self, profile: str) -> ProtectedValueBackend: ...
