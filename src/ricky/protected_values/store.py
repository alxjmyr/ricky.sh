"""Cancellation-safe profile-local encrypted protected-value storage."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter

from ricky.config import RickySettings, ensure_private_user_data_root, profile_data_subpath
from ricky.profiles import ProfileResourceRef, validate_profile_name
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

SCHEMA_VERSION = 2
ENVELOPE_VERSION = 1
UNLOCK_SLOT_VERSION = 1
_FIELDS = TypeAdapter(tuple[ProtectedFieldDescriptor, ...])
_USE_REQUEST = TypeAdapter(ProtectedUseRequest)
_COMMIT_REQUEST = TypeAdapter(ProtectedCommitRequest)


class _Argon2idParameters(BaseModel):
    """Exact KDF inputs persisted with one passphrase unlock slot."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    algorithm: Literal["argon2id"] = "argon2id"
    length: Literal[32] = 32
    iterations: int = Field(ge=1, le=100)
    lanes: int = Field(ge=1, le=32)
    memory_kib: int = Field(ge=8_192, le=1_048_576)


class ProtectedValueStoreError(RuntimeError):
    """A protected-value store operation failed without exposing material."""


class VaultNotInitializedError(ProtectedValueStoreError):
    """The selected profile has no initialized vault."""


class VaultAlreadyInitializedError(ProtectedValueStoreError):
    """The selected profile already has a vault."""


class VaultLockedError(ProtectedValueStoreError):
    """The selected profile vault is locked."""


class ProtectedValueNotFoundError(ProtectedValueStoreError):
    """The protected resource is absent or outside the caller's scope."""


class ProtectedValueConflictError(ProtectedValueStoreError):
    """The protected resource changed from the expected revision."""


class ProfileVaultStore:
    """One profile-owned encrypted payload store and safe catalog."""

    def __init__(self, settings: RickySettings, profile: str) -> None:
        self._settings = settings
        self.profile = validate_profile_name(profile)
        self.root = profile_data_subpath(settings, self.profile, settings.protected_values.dir)
        self.path = self.root / "protected-values.sqlite3"
        self._data_key: bytes | None = None

    @property
    def unlocked(self) -> bool:
        return self._data_key is not None

    async def initialized(self) -> bool:
        return await self._run(self._initialized)

    async def initialize(self, passphrase: SecretStr) -> None:
        await self._run(self._initialize, passphrase)

    async def unlock(self, passphrase: SecretStr) -> None:
        key = await self._run(self._unlock, passphrase)
        self._data_key = key

    async def lock(self) -> None:
        self._data_key = None

    async def rotate_passphrase(self, new_passphrase: SecretStr) -> None:
        key = self._require_key()
        await self._run(self._rotate_passphrase, key, new_passphrase)

    async def list(
        self,
        *,
        limit: int,
        kind: ProtectedValueKind | None = None,
    ) -> list[ProtectedValueDescriptor]:
        if limit < 1 or limit > self._settings.protected_values.catalog_limit:
            raise ValueError("protected-value catalog limit is outside configured bounds")
        return await self._run(self._list, limit, kind)

    async def count(self) -> int:
        return await self._run(self._count)

    async def get(self, ref: ProfileResourceRef) -> ProtectedValueDescriptor:
        self._require_owner(ref)
        found = await self._run(self._get, ref.name)
        if found is None:
            raise ProtectedValueNotFoundError(f"protected value not found: {ref.qualified}")
        return found

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
    ) -> ProtectedValueDescriptor:
        key = self._require_key()
        ref = ProfileResourceRef(profile=self.profile, name=name)
        now = _now()
        descriptor = ProtectedValueDescriptor(
            ref=ref,
            kind=kind,
            label=label,
            description=description,
            fields=fields,
            policy=policy,
            revision=1,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        payload = self._encrypt_payload(key, descriptor, values)
        return await self._run(self._create, descriptor, payload)

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
    ) -> ProtectedValueDescriptor:
        self._require_owner(descriptor.ref)
        key = self._require_key()
        updated = descriptor.model_copy(
            update={
                "label": label.strip(),
                "description": description.strip(),
                "fields": fields,
                "policy": policy,
                "revision": expected_revision + 1,
                "updated_at": _now(),
            }
        )
        updated = ProtectedValueDescriptor.model_validate(updated.model_dump(), strict=True)
        payload = self._encrypt_payload(key, updated, values)
        return await self._run(self._replace, updated, expected_revision, payload)

    async def set_enabled(
        self, ref: ProfileResourceRef, *, expected_revision: int, enabled: bool
    ) -> ProtectedValueDescriptor:
        self._require_owner(ref)
        key = self._require_key()
        descriptor = await self.get(ref)
        values = await self.read_all(descriptor)
        updated = descriptor.model_copy(
            update={
                "revision": expected_revision + 1,
                "enabled": enabled,
                "updated_at": _now(),
            }
        )
        payload = self._encrypt_payload(key, updated, values.values)
        return await self._run(self._replace, updated, expected_revision, payload)

    async def delete(self, ref: ProfileResourceRef, *, expected_revision: int) -> None:
        self._require_owner(ref)
        self._require_key()
        await self._run(self._delete, ref.name, expected_revision)

    async def read_all(self, descriptor: ProtectedValueDescriptor) -> StoredSecretPayload:
        self._require_owner(descriptor.ref)
        key = self._require_key()
        token = await self._run(self._payload, descriptor.ref.name, descriptor.revision)
        return self._decrypt_payload(key, descriptor, token)

    async def approve(
        self, ref: ProfileResourceRef, *, top_level_origin: str, frame_origin: str
    ) -> ProtectedDestinationApproval:
        self._require_owner(ref)
        await self.get(ref)
        approval = ProtectedDestinationApproval(
            ref=ref,
            top_level_origin=top_level_origin,
            frame_origin=frame_origin,
            approved_at=_now(),
        )
        return await self._run(self._approve, approval)

    async def revoke_approval(
        self, ref: ProfileResourceRef, *, top_level_origin: str, frame_origin: str
    ) -> bool:
        self._require_owner(ref)
        return await self._run(self._revoke_approval, ref.name, top_level_origin, frame_origin)

    async def approvals(
        self, ref: ProfileResourceRef, *, limit: int
    ) -> list[ProtectedDestinationApproval]:
        self._require_owner(ref)
        if limit < 1 or limit > self._settings.protected_values.catalog_limit:
            raise ValueError("protected approval list limit is outside configured bounds")
        return await self._run(self._approvals, ref, limit)

    async def is_approved(
        self, ref: ProfileResourceRef, *, top_level_origin: str, frame_origin: str
    ) -> bool:
        self._require_owner(ref)
        return await self._run(self._is_approved, ref.name, top_level_origin, frame_origin)

    async def reserve(
        self,
        request: ProtectedUseRequest,
        *,
        revision: int,
        materialization_limit: int | None = None,
    ) -> ProtectedUseRecord:
        self._require_owner(request.ref)
        record = ProtectedUseRecord(
            id=f"protected_use_{uuid4().hex}",
            request=request,
            resource_revision=revision,
            disposition="reserved",
            created_at=_now(),
        )
        return await self._run(self._reserve, record, materialization_limit)

    async def finalize(self, record: ProtectedUseRecord, *, disposition: str) -> ProtectedUseRecord:
        if disposition not in {"materialized", "cancelled", "failed"}:
            raise ValueError("invalid protected-use final disposition")
        return await self._run(self._finalize, record, disposition)

    async def uses(self, *, limit: int) -> list[ProtectedUseRecord]:
        if limit < 1 or limit > self._settings.protected_values.audit_limit:
            raise ValueError("protected-use audit limit is outside configured bounds")
        return await self._run(self._uses, limit)

    async def reserve_commit(
        self,
        request: ProtectedCommitRequest,
        *,
        commit_limit: int,
    ) -> ProtectedCommitRecord:
        self._require_owner(request.ref)
        if commit_limit < 1:
            raise ProtectedValueStoreError("protected value forbids unattended commits")
        record = ProtectedCommitRecord(
            id=f"protected_commit_{uuid4().hex}",
            request=request,
            disposition="reserved",
            created_at=_now(),
        )
        return await self._run(self._reserve_commit, record, commit_limit)

    async def finalize_commit(
        self,
        record: ProtectedCommitRecord,
        *,
        disposition: str,
    ) -> ProtectedCommitRecord:
        if disposition not in {"performed", "not_performed", "in_doubt"}:
            raise ValueError("invalid protected-commit final disposition")
        return await self._run(self._finalize_commit, record, disposition)

    async def _run[T](self, operation: Callable[..., T], *args: object) -> T:
        task = asyncio.create_task(asyncio.to_thread(self._call, operation, args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.shield(task)
            raise
        except ProtectedValueStoreError:
            raise
        except (sqlite3.Error, OSError, ValueError, InvalidToken) as exc:
            raise ProtectedValueStoreError("protected-value store operation failed") from exc

    def _call[T](self, operation: Callable[..., T], args: tuple[object, ...]) -> T:
        try:
            return operation(*args)
        finally:
            self._secure_paths()

    def _secure_paths(self) -> None:
        if os.name != "posix":
            return
        with suppress(OSError):
            os.chmod(self.root, 0o700)
        sidecars = (
            self.path,
            self.path.with_name(self.path.name + "-wal"),
            self.path.with_name(self.path.name + "-shm"),
        )
        for path in sidecars:
            with suppress(OSError):
                os.chmod(path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self._settings.protected_values.sqlite_busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            f"PRAGMA busy_timeout = {self._settings.protected_values.sqlite_busy_timeout_ms}"
        )
        connection.execute("PRAGMA secure_delete = ON")
        return connection

    def _initialized(self) -> bool:
        if not self.path.is_file():
            return False
        from ricky.protected_values.upgrade import inspect_protected_values_store

        inspect_protected_values_store(self.path, allow_supported_old=False)
        uri = self.path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT profile FROM vault_header WHERE id = 1").fetchone()
        if row is None or row["profile"] != self.profile:
            raise ProtectedValueStoreError("protected-value vault owner does not match its path")
        return True

    def _initialize(self, passphrase: SecretStr) -> None:
        if self.path.exists():
            if self._initialized():
                raise VaultAlreadyInitializedError(
                    f"protected-value vault already initialized for profile {self.profile}"
                )
            raise ProtectedValueStoreError("protected-value vault path is already occupied")
        ensure_private_user_data_root(self._settings)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(self.root, 0o700)
        temporary = self.root / f".protected-values-{uuid4().hex}.tmp"
        data_key = Fernet.generate_key()
        salt = os.urandom(16)
        parameters = self._configured_kdf_parameters()
        wrapped = self._wrap_key(data_key, passphrase, salt, parameters=parameters)
        now = _now().isoformat()
        try:
            connection = sqlite3.connect(temporary, isolation_level=None)
            try:
                if os.name == "posix":
                    os.chmod(temporary, 0o600)
                connection.execute("PRAGMA journal_mode = WAL")
                with connection:
                    connection.execute("PRAGMA foreign_keys = ON")
                    connection.execute("PRAGMA secure_delete = ON")
                    connection.executescript(_SCHEMA)
                    connection.execute(
                        """
                        INSERT INTO vault_header (
                            id, profile, envelope_version, created_at, updated_at
                        ) VALUES (1, ?, ?, ?, ?)
                        """,
                        (self.profile, ENVELOPE_VERSION, now, now),
                    )
                    connection.execute(
                        """
                        INSERT INTO unlock_slots (
                            id, kind, slot_version, kdf_parameters_json, salt, wrapped_key,
                            created_at, updated_at
                        ) VALUES ('interactive-passphrase', 'passphrase', ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            UNLOCK_SLOT_VERSION,
                            parameters.model_dump_json(),
                            salt,
                            wrapped,
                            now,
                            now,
                        ),
                    )
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    connection.commit()
            finally:
                connection.close()
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            try:
                os.link(temporary, self.path)
            except FileExistsError as exc:
                raise VaultAlreadyInitializedError(
                    f"protected-value vault already initialized for profile {self.profile}"
                ) from exc
            temporary.unlink()
            if os.name == "posix":
                with suppress(OSError):
                    directory_fd = os.open(self.root, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
        finally:
            with suppress(OSError):
                temporary.unlink()

    def _unlock(self, passphrase: SecretStr) -> bytes:
        if not self._initialized():
            raise VaultNotInitializedError(
                f"protected-value vault is not initialized for profile {self.profile}"
            )
        with self._connect() as connection:
            header = connection.execute("SELECT * FROM vault_header WHERE id = 1").fetchone()
            slot = connection.execute(
                "SELECT * FROM unlock_slots WHERE id = 'interactive-passphrase'"
            ).fetchone()
        assert header is not None
        if header["envelope_version"] != ENVELOPE_VERSION or slot is None:
            raise ProtectedValueStoreError("unsupported protected-value envelope version")
        if slot["kind"] != "passphrase" or slot["slot_version"] != UNLOCK_SLOT_VERSION:
            raise ProtectedValueStoreError("unsupported protected-value unlock slot")
        parameters = _Argon2idParameters.model_validate_json(
            slot["kdf_parameters_json"], strict=True
        )
        return self._unwrap_key(
            bytes(slot["wrapped_key"]),
            passphrase,
            bytes(slot["salt"]),
            parameters=parameters,
        )

    def _rotate_passphrase(self, data_key: bytes, passphrase: SecretStr) -> None:
        salt = os.urandom(16)
        parameters = self._configured_kdf_parameters()
        wrapped = self._wrap_key(data_key, passphrase, salt, parameters=parameters)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE unlock_slots SET
                    kdf_parameters_json = ?, salt = ?, wrapped_key = ?, updated_at = ?
                WHERE id = 'interactive-passphrase' AND kind = 'passphrase'
                """,
                (parameters.model_dump_json(), salt, wrapped, _now().isoformat()),
            )
            if cursor.rowcount != 1:
                raise ProtectedValueStoreError("interactive passphrase unlock slot is unavailable")
            connection.execute(
                "UPDATE vault_header SET updated_at = ? WHERE id = 1",
                (_now().isoformat(),),
            )
            connection.commit()

    def _configured_kdf_parameters(self) -> _Argon2idParameters:
        return _Argon2idParameters(
            iterations=self._settings.protected_values.argon2_iterations,
            lanes=self._settings.protected_values.argon2_lanes,
            memory_kib=self._settings.protected_values.argon2_memory_kib,
        )

    def _derive_wrapping_key(
        self,
        passphrase: SecretStr,
        salt: bytes,
        *,
        parameters: _Argon2idParameters | None = None,
    ) -> bytes:
        selected = parameters or self._configured_kdf_parameters()
        raw = Argon2id(
            salt=salt,
            length=selected.length,
            iterations=selected.iterations,
            lanes=selected.lanes,
            memory_cost=selected.memory_kib,
        ).derive(passphrase.get_secret_value().encode("utf-8"))
        return base64.urlsafe_b64encode(raw)

    def _wrap_key(
        self,
        data_key: bytes,
        passphrase: SecretStr,
        salt: bytes,
        *,
        parameters: _Argon2idParameters,
    ) -> bytes:
        body = json.dumps(
            {
                "version": UNLOCK_SLOT_VERSION,
                "profile": self.profile,
                "data_key": data_key.decode("ascii"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return Fernet(self._derive_wrapping_key(passphrase, salt, parameters=parameters)).encrypt(
            body
        )

    def _unwrap_key(
        self,
        token: bytes,
        passphrase: SecretStr,
        salt: bytes,
        *,
        parameters: _Argon2idParameters,
    ) -> bytes:
        try:
            body = Fernet(
                self._derive_wrapping_key(passphrase, salt, parameters=parameters)
            ).decrypt(token)
            decoded = json.loads(body)
            key = decoded["data_key"].encode("ascii")
        except (InvalidToken, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise VaultLockedError("protected-value vault unlock failed") from None
        if decoded.get("version") != UNLOCK_SLOT_VERSION or decoded.get("profile") != self.profile:
            raise VaultLockedError("protected-value vault unlock failed")
        try:
            Fernet(key)
        except (TypeError, ValueError):
            raise VaultLockedError("protected-value vault unlock failed") from None
        return key

    def _encrypt_payload(
        self,
        data_key: bytes,
        descriptor: ProtectedValueDescriptor,
        values: Mapping[str, SecretStr],
    ) -> bytes:
        stored_names = {field.name for field in descriptor.fields if field.mode == "stored"}
        if set(values) != stored_names:
            raise ValueError("stored protected fields require exactly one value each")
        body = json.dumps(
            {
                "version": ENVELOPE_VERSION,
                "profile": descriptor.ref.profile,
                "name": descriptor.ref.name,
                "revision": descriptor.revision,
                "fields": [field.model_dump(mode="json") for field in descriptor.fields],
                "values": {name: values[name].get_secret_value() for name in sorted(stored_names)},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return Fernet(data_key).encrypt(body)

    def _decrypt_payload(
        self,
        data_key: bytes,
        descriptor: ProtectedValueDescriptor,
        token: bytes,
    ) -> StoredSecretPayload:
        try:
            decoded = json.loads(Fernet(data_key).decrypt(token))
            fields = _FIELDS.validate_json(
                json.dumps(decoded["fields"], separators=(",", ":")), strict=True
            )
            values = decoded["values"]
        except (InvalidToken, KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise ProtectedValueStoreError(
                "protected-value payload failed authentication"
            ) from None
        if (
            decoded.get("version") != ENVELOPE_VERSION
            or decoded.get("profile") != descriptor.ref.profile
            or decoded.get("name") != descriptor.ref.name
            or decoded.get("revision") != descriptor.revision
            or fields != descriptor.fields
            or not isinstance(values, dict)
        ):
            raise ProtectedValueStoreError("protected-value payload binding does not match")
        stored_names = {field.name for field in descriptor.fields if field.mode == "stored"}
        if set(values) != stored_names or any(
            not isinstance(value, str) for value in values.values()
        ):
            raise ProtectedValueStoreError("protected-value payload fields do not match")
        return StoredSecretPayload(values={name: SecretStr(values[name]) for name in stored_names})

    def _list(self, limit: int, kind: ProtectedValueKind | None) -> list[ProtectedValueDescriptor]:
        self._require_initialized_sync()
        with self._connect() as connection:
            if kind is None:
                rows = connection.execute(
                    "SELECT * FROM protected_resources ORDER BY profile, name LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM protected_resources
                    WHERE kind = ? ORDER BY profile, name LIMIT ?
                    """,
                    (kind, limit),
                ).fetchall()
        return [self._descriptor(row) for row in rows]

    def _count(self) -> int:
        self._require_initialized_sync()
        with self._connect() as connection:
            row = connection.execute("SELECT count(*) FROM protected_resources").fetchone()
        assert row is not None
        return int(row[0])

    def _get(self, name: str) -> ProtectedValueDescriptor | None:
        self._require_initialized_sync()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM protected_resources WHERE name = ?", (name,)
            ).fetchone()
        return None if row is None else self._descriptor(row)

    def _create(
        self, descriptor: ProtectedValueDescriptor, payload: bytes
    ) -> ProtectedValueDescriptor:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO protected_resources (
                        name, profile, kind, label, description, fields_json, policy_json,
                        revision, enabled, payload, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._resource_values(descriptor, payload),
                )
            except sqlite3.IntegrityError as exc:
                raise ProtectedValueConflictError(
                    f"protected value already exists: {descriptor.ref.qualified}"
                ) from exc
            connection.commit()
        return descriptor

    def _replace(
        self, descriptor: ProtectedValueDescriptor, expected_revision: int, payload: bytes
    ) -> ProtectedValueDescriptor:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE protected_resources SET
                    kind = ?, label = ?, description = ?, fields_json = ?, policy_json = ?,
                    revision = ?, enabled = ?, payload = ?, updated_at = ?
                WHERE name = ? AND revision = ?
                """,
                (
                    descriptor.kind,
                    descriptor.label,
                    descriptor.description,
                    json.dumps([field.model_dump(mode="json") for field in descriptor.fields]),
                    descriptor.policy.model_dump_json(),
                    descriptor.revision,
                    int(descriptor.enabled),
                    payload,
                    descriptor.updated_at.isoformat(),
                    descriptor.ref.name,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ProtectedValueConflictError("protected value changed before update")
            connection.commit()
        return descriptor

    def _delete(self, name: str, expected_revision: int) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM protected_resources WHERE name = ? AND revision = ?",
                (name, expected_revision),
            )
            if cursor.rowcount != 1:
                raise ProtectedValueConflictError("protected value changed before deletion")
            connection.commit()

    def _payload(self, name: str, revision: int) -> bytes:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM protected_resources WHERE name = ? AND revision = ?",
                (name, revision),
            ).fetchone()
        if row is None:
            raise ProtectedValueConflictError("protected value changed before materialization")
        return bytes(row["payload"])

    def _approve(self, approval: ProtectedDestinationApproval) -> ProtectedDestinationApproval:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO destination_approvals (
                    resource_name, top_level_origin, frame_origin, approved_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(resource_name, top_level_origin, frame_origin)
                DO UPDATE SET approved_at = excluded.approved_at
                """,
                (
                    approval.ref.name,
                    approval.top_level_origin,
                    approval.frame_origin,
                    approval.approved_at.isoformat(),
                ),
            )
            connection.commit()
        return approval

    def _revoke_approval(self, name: str, top_origin: str, frame_origin: str) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                DELETE FROM destination_approvals
                WHERE resource_name = ? AND top_level_origin = ? AND frame_origin = ?
                """,
                (name, top_origin, frame_origin),
            )
            connection.commit()
        return cursor.rowcount == 1

    def _approvals(self, ref: ProfileResourceRef, limit: int) -> list[ProtectedDestinationApproval]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM destination_approvals
                WHERE resource_name = ?
                ORDER BY approved_at DESC LIMIT ?
                """,
                (ref.name, limit),
            ).fetchall()
        return [
            ProtectedDestinationApproval(
                ref=ref,
                top_level_origin=row["top_level_origin"],
                frame_origin=row["frame_origin"],
                approved_at=datetime.fromisoformat(row["approved_at"]),
            )
            for row in rows
        ]

    def _is_approved(self, name: str, top_origin: str, frame_origin: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM destination_approvals
                WHERE resource_name = ? AND top_level_origin = ? AND frame_origin = ?
                """,
                (name, top_origin, frame_origin),
            ).fetchone()
        return row is not None

    def _reserve(
        self,
        record: ProtectedUseRecord,
        materialization_limit: int | None,
    ) -> ProtectedUseRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision, enabled FROM protected_resources WHERE name = ?",
                (record.request.ref.name,),
            ).fetchone()
            if row is None or not bool(row["enabled"]):
                raise ProtectedValueNotFoundError(
                    f"protected value is unavailable: {record.request.ref.qualified}"
                )
            if int(row["revision"]) != record.resource_revision:
                raise ProtectedValueConflictError("protected value changed before reservation")
            if materialization_limit is not None:
                execution_id = record.request.execution_id
                if execution_id is None:
                    raise ProtectedValueStoreError(
                        "unattended materialization reservation requires an execution id"
                    )
                used = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM protected_uses
                    WHERE resource_name = ?
                      AND json_extract(request_json, '$.execution_id') = ?
                    """,
                    (record.request.ref.name, execution_id),
                ).fetchone()
                assert used is not None
                if int(used["count"]) >= materialization_limit:
                    raise ProtectedValueStoreError(
                        "protected-value unattended materialization ceiling was reached"
                    )
            connection.execute(
                """
                INSERT INTO protected_uses (
                    id, resource_name, revision, request_json, disposition,
                    created_at, finalized_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    record.id,
                    record.request.ref.name,
                    record.resource_revision,
                    record.request.model_dump_json(),
                    record.disposition,
                    record.created_at.isoformat(),
                ),
            )
            connection.commit()
        return record

    def _finalize(self, record: ProtectedUseRecord, disposition: str) -> ProtectedUseRecord:
        finalized = record.model_copy(update={"disposition": disposition, "finalized_at": _now()})
        assert finalized.finalized_at is not None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE protected_uses SET disposition = ?, finalized_at = ?
                WHERE id = ? AND disposition = 'reserved'
                """,
                (disposition, finalized.finalized_at.isoformat(), record.id),
            )
            if cursor.rowcount != 1:
                raise ProtectedValueConflictError("protected use was already finalized")
            connection.commit()
        return ProtectedUseRecord.model_validate(finalized.model_dump(), strict=True)

    def _uses(self, limit: int) -> list[ProtectedUseRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM protected_uses ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            ProtectedUseRecord(
                id=row["id"],
                request=_USE_REQUEST.validate_json(row["request_json"], strict=True),
                resource_revision=int(row["revision"]),
                disposition=row["disposition"],
                created_at=datetime.fromisoformat(row["created_at"]),
                finalized_at=(
                    None
                    if row["finalized_at"] is None
                    else datetime.fromisoformat(row["finalized_at"])
                ),
            )
            for row in rows
        ]

    def _reserve_commit(
        self,
        record: ProtectedCommitRecord,
        commit_limit: int,
    ) -> ProtectedCommitRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision, enabled FROM protected_resources WHERE name = ?",
                (record.request.ref.name,),
            ).fetchone()
            if row is None or not bool(row["enabled"]):
                raise ProtectedValueNotFoundError(
                    f"protected value is unavailable: {record.request.ref.qualified}"
                )
            if int(row["revision"]) != record.request.revision:
                raise ProtectedValueConflictError(
                    "protected value changed before commit reservation"
                )
            existing = connection.execute(
                """
                SELECT id, request_json FROM protected_commits
                WHERE resource_name = ? AND execution_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (record.request.ref.name, record.request.execution_id),
            ).fetchall()
            for item in existing:
                prior = _COMMIT_REQUEST.validate_json(item["request_json"], strict=True)
                if prior.logical_effect_key == record.request.logical_effect_key:
                    raise ProtectedValueConflictError(
                        "protected commit logical effect was already reserved"
                    )
            if len(existing) >= commit_limit:
                raise ProtectedValueStoreError(
                    "protected-value unattended commit ceiling was reached"
                )
            connection.execute(
                """
                INSERT INTO protected_commits (
                    id, resource_name, execution_id, revision, request_json,
                    disposition, created_at, finalized_at
                ) VALUES (?, ?, ?, ?, ?, 'reserved', ?, NULL)
                """,
                (
                    record.id,
                    record.request.ref.name,
                    record.request.execution_id,
                    record.request.revision,
                    record.request.model_dump_json(),
                    record.created_at.isoformat(),
                ),
            )
            connection.commit()
        return record

    def _finalize_commit(
        self,
        record: ProtectedCommitRecord,
        disposition: str,
    ) -> ProtectedCommitRecord:
        finalized = record.model_copy(update={"disposition": disposition, "finalized_at": _now()})
        assert finalized.finalized_at is not None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE protected_commits SET disposition = ?, finalized_at = ?
                WHERE id = ? AND disposition = 'reserved'
                """,
                (disposition, finalized.finalized_at.isoformat(), record.id),
            )
            if cursor.rowcount != 1:
                raise ProtectedValueConflictError("protected commit was already finalized")
            connection.commit()
        return ProtectedCommitRecord.model_validate(finalized.model_dump(), strict=True)

    def _descriptor(self, row: sqlite3.Row) -> ProtectedValueDescriptor:
        return ProtectedValueDescriptor(
            ref=ProfileResourceRef(profile=row["profile"], name=row["name"]),
            kind=row["kind"],
            label=row["label"],
            description=row["description"],
            fields=_FIELDS.validate_json(row["fields_json"], strict=True),
            policy=ProtectedDestinationPolicy.model_validate_json(row["policy_json"], strict=True),
            revision=int(row["revision"]),
            enabled=bool(row["enabled"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _resource_values(
        descriptor: ProtectedValueDescriptor, payload: bytes
    ) -> tuple[object, ...]:
        return (
            descriptor.ref.name,
            descriptor.ref.profile,
            descriptor.kind,
            descriptor.label,
            descriptor.description,
            json.dumps([field.model_dump(mode="json") for field in descriptor.fields]),
            descriptor.policy.model_dump_json(),
            descriptor.revision,
            int(descriptor.enabled),
            payload,
            descriptor.created_at.isoformat(),
            descriptor.updated_at.isoformat(),
        )

    def _require_key(self) -> bytes:
        if self._data_key is None:
            raise VaultLockedError(f"protected-value vault is locked for profile {self.profile}")
        return self._data_key

    def _require_owner(self, ref: ProfileResourceRef) -> None:
        if ref.profile != self.profile:
            raise ProtectedValueNotFoundError(f"protected value not found: {ref.qualified}")

    def _require_initialized_sync(self) -> None:
        if not self._initialized():
            raise VaultNotInitializedError(
                f"protected-value vault is not initialized for profile {self.profile}"
            )


def _now() -> datetime:
    return datetime.now(UTC)


_SCHEMA = """
CREATE TABLE vault_header (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    profile TEXT NOT NULL,
    envelope_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE unlock_slots (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    slot_version INTEGER NOT NULL,
    kdf_parameters_json TEXT NOT NULL,
    salt BLOB NOT NULL,
    wrapped_key BLOB NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE protected_resources (
    name TEXT PRIMARY KEY,
    profile TEXT NOT NULL,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    description TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    payload BLOB NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE destination_approvals (
    resource_name TEXT NOT NULL REFERENCES protected_resources(name) ON DELETE CASCADE,
    top_level_origin TEXT NOT NULL,
    frame_origin TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    PRIMARY KEY (resource_name, top_level_origin, frame_origin)
);

CREATE TABLE protected_uses (
    id TEXT PRIMARY KEY,
    resource_name TEXT REFERENCES protected_resources(name) ON DELETE SET NULL,
    revision INTEGER NOT NULL,
    request_json TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (
        disposition IN ('reserved', 'materialized', 'cancelled', 'failed')
    ),
    created_at TEXT NOT NULL,
    finalized_at TEXT
);

CREATE TABLE protected_commits (
    id TEXT PRIMARY KEY,
    resource_name TEXT REFERENCES protected_resources(name) ON DELETE SET NULL,
    execution_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    request_json TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (
        disposition IN ('reserved', 'performed', 'not_performed', 'in_doubt')
    ),
    created_at TEXT NOT NULL,
    finalized_at TEXT
);
"""
