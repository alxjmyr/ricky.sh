"""Scoped broker, destination policy, and secure responder seams."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr

from ricky.config import RickySettings
from ricky.profiles import ProfileResourceRef, ProfileScope, validate_profile_name
from ricky.protected_values.backend import (
    ProtectedValueBackend,
    ProtectedValueBackendFactory,
    ProtectedValueBackendProvider,
)
from ricky.protected_values.store import (
    ProfileVaultStore,
    ProtectedValueConflictError,
    ProtectedValueNotFoundError,
    ProtectedValueStoreError,
    VaultLockedError,
    VaultNotInitializedError,
)
from ricky.protected_values.types import (
    DestinationApprovalRequest,
    DestinationApprovalResponse,
    ProtectedCommitRecord,
    ProtectedCommitRequest,
    ProtectedDestinationApproval,
    ProtectedDestinationPolicy,
    ProtectedFieldDescriptor,
    ProtectedMaterial,
    ProtectedUseRecord,
    ProtectedUseRequest,
    ProtectedValueDescriptor,
    ProtectedValueKind,
    ProtectedVaultStatus,
    SecureValueInputRequest,
    UnlockRequest,
)

AuthorizationEvidence = Literal["authored", "approved", "allow_once", "secure_web"]
UnlockResponder = Callable[[UnlockRequest], Awaitable[SecretStr | None]]
SecureValueResponder = Callable[[SecureValueInputRequest], Awaitable[SecretStr | None]]
DestinationApprovalResponder = Callable[
    [DestinationApprovalRequest], Awaitable[DestinationApprovalResponse]
]
ProtectedHostResolver = Callable[[str, int], Awaitable[tuple[str, ...]]]


async def _system_host_resolver(host: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    answers = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(sorted({str(answer[4][0]) for answer in answers}))


async def deny_unlock(_request: UnlockRequest) -> SecretStr | None:
    return None


async def deny_secure_value(_request: SecureValueInputRequest) -> SecretStr | None:
    return None


async def deny_destination(
    _request: DestinationApprovalRequest,
) -> DestinationApprovalResponse:
    return DestinationApprovalResponse(decision="deny")


class ProtectedValueBroker:
    """Profile-scoped protected-value catalog and local materialization boundary."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        scope: ProfileScope,
        consumer_ids: frozenset[str] = frozenset(),
        unlock_responder: UnlockResponder = deny_unlock,
        secure_value_responder: SecureValueResponder = deny_secure_value,
        destination_responder: DestinationApprovalResponder = deny_destination,
        backend_factory: ProtectedValueBackendFactory = ProfileVaultStore,
        backend_provider: ProtectedValueBackendProvider | None = None,
        host_resolver: ProtectedHostResolver = _system_host_resolver,
        resolution_timeout_seconds: float = 10.0,
    ) -> None:
        self._settings = settings
        self.scope = scope
        self._consumer_ids = consumer_ids
        self._unlock_responder = unlock_responder
        self._secure_value_responder = secure_value_responder
        self._destination_responder = destination_responder
        self._backend_factory = backend_factory
        self._backend_provider = backend_provider
        self._host_resolver = host_resolver
        self._resolution_timeout_seconds = resolution_timeout_seconds
        self._stores: dict[str, ProtectedValueBackend] = {}
        self._closed = False

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._backend_provider is None:
            stack = AsyncExitStack()
            for store in self._stores.values():
                stack.push_async_callback(store.lock)
            await stack.aclose()
        self._stores.clear()

    async def catalog(
        self,
        *,
        kind: ProtectedValueKind | None = None,
        ref: ProfileResourceRef | None = None,
        limit: int | None = None,
    ) -> list[ProtectedValueDescriptor]:
        self._ensure_open()
        requested_limit = self._settings.protected_values.catalog_limit if limit is None else limit
        if requested_limit < 1 or requested_limit > self._settings.protected_values.catalog_limit:
            raise ValueError("protected-value catalog limit is outside configured bounds")
        if ref is not None:
            self._require_ref(ref)
            try:
                exact = await self._store(ref.profile).get(ref)
            except (VaultNotInitializedError, ProtectedValueNotFoundError):
                return []
            return [exact] if kind is None or exact.kind == kind else []
        found: list[ProtectedValueDescriptor] = []
        for profile in self.scope.profiles:
            store = self._store(profile)
            if not await store.initialized():
                continue
            found.extend(await store.list(limit=requested_limit, kind=kind))
        return sorted(found, key=lambda item: item.ref.qualified)[:requested_limit]

    async def initialize(self, profile: str, passphrase: SecretStr) -> None:
        self._require_profile(profile)
        await self._store(profile).initialize(passphrase)

    async def status(self, profile: str) -> ProtectedVaultStatus:
        self._require_profile(profile)
        store = self._store(profile)
        initialized = await store.initialized()
        count = await store.count() if initialized else 0
        return ProtectedVaultStatus(
            profile=profile,
            enabled=self._settings.protected_values.enabled,
            initialized=initialized,
            unlocked=store.unlocked,
            resource_count=count,
        )

    async def unlock(self, profile: str, passphrase: SecretStr | None = None) -> None:
        self._require_profile(profile)
        store = self._store(profile)
        if store.unlocked:
            return
        supplied = passphrase
        if supplied is None:
            try:
                async with asyncio.timeout(self._settings.protected_values.prompt_timeout_seconds):
                    supplied = await self._unlock_responder(UnlockRequest(profile=profile))
            except TimeoutError:
                supplied = None
        if supplied is None:
            raise VaultLockedError(f"protected-value vault remains locked for profile {profile}")
        await store.unlock(supplied)

    async def rotate_passphrase(self, profile: str, new_passphrase: SecretStr) -> None:
        self._require_profile(profile)
        await self._store(profile).rotate_passphrase(new_passphrase)

    async def create(
        self,
        *,
        profile: str,
        name: str,
        kind: ProtectedValueKind,
        label: str,
        description: str,
        fields: tuple[ProtectedFieldDescriptor, ...],
        policy: ProtectedDestinationPolicy,
        values: Mapping[str, SecretStr],
    ) -> ProtectedValueDescriptor:
        self._require_profile(profile)
        return await self._store(profile).create(
            name=name,
            kind=kind,
            label=label,
            description=description,
            fields=fields,
            policy=_canonical_policy(policy),
            values=values,
        )

    async def replace(
        self,
        descriptor: ProtectedValueDescriptor,
        *,
        label: str,
        description: str,
        fields: tuple[ProtectedFieldDescriptor, ...],
        policy: ProtectedDestinationPolicy,
        values: Mapping[str, SecretStr],
    ) -> ProtectedValueDescriptor:
        self._require_ref(descriptor.ref)
        return await self._store(descriptor.ref.profile).replace(
            descriptor,
            expected_revision=descriptor.revision,
            label=label,
            description=description,
            fields=fields,
            policy=_canonical_policy(policy),
            values=values,
        )

    async def revise(
        self,
        descriptor: ProtectedValueDescriptor,
        *,
        label: str | None = None,
        description: str | None = None,
        fields: tuple[ProtectedFieldDescriptor, ...] | None = None,
        policy: ProtectedDestinationPolicy | None = None,
        values: Mapping[str, SecretStr] | None = None,
    ) -> ProtectedValueDescriptor:
        """Apply one operator revision, preserving encrypted values when possible."""
        self._require_ref(descriptor.ref)
        store = self._store(descriptor.ref.profile)
        selected_fields = fields or descriptor.fields
        selected_values = values
        if selected_values is None:
            if selected_fields != descriptor.fields:
                raise ValueError("field schema changes require replacement stored values")
            selected_values = (await store.read_all(descriptor)).values
        return await store.replace(
            descriptor,
            expected_revision=descriptor.revision,
            label=label if label is not None else descriptor.label,
            description=(description if description is not None else descriptor.description),
            fields=selected_fields,
            policy=_canonical_policy(policy or descriptor.policy),
            values=selected_values,
        )

    async def set_enabled(
        self, ref: ProfileResourceRef, *, expected_revision: int, enabled: bool
    ) -> ProtectedValueDescriptor:
        self._require_ref(ref)
        return await self._store(ref.profile).set_enabled(
            ref, expected_revision=expected_revision, enabled=enabled
        )

    async def delete(self, ref: ProfileResourceRef, *, expected_revision: int) -> None:
        self._require_ref(ref)
        await self._store(ref.profile).delete(ref, expected_revision=expected_revision)

    async def approve(
        self, ref: ProfileResourceRef, *, top_level_origin: str, frame_origin: str
    ) -> ProtectedDestinationApproval:
        self._require_ref(ref)
        top = canonical_protected_origin(top_level_origin, allow_private=True)
        frame = canonical_protected_origin(frame_origin, allow_private=True)
        return await self._store(ref.profile).approve(ref, top_level_origin=top, frame_origin=frame)

    async def revoke_approval(
        self, ref: ProfileResourceRef, *, top_level_origin: str, frame_origin: str
    ) -> bool:
        self._require_ref(ref)
        top = canonical_protected_origin(top_level_origin, allow_private=True)
        frame = canonical_protected_origin(frame_origin, allow_private=True)
        return await self._store(ref.profile).revoke_approval(
            ref, top_level_origin=top, frame_origin=frame
        )

    async def approvals(
        self, ref: ProfileResourceRef, *, limit: int | None = None
    ) -> list[ProtectedDestinationApproval]:
        self._require_ref(ref)
        return await self._store(ref.profile).approvals(
            ref, limit=limit or self._settings.protected_values.catalog_limit
        )

    async def prepare(self, request: ProtectedUseRequest) -> ProtectedMaterial:
        """Authorize, reserve, and release exact local material to a trusted consumer."""

        self._ensure_open()
        self._require_ref(request.ref)
        if request.consumer_id not in self._consumer_ids:
            raise ProtectedValueStoreError("protected-value consumer is not registered")
        store = self._store(request.ref.profile)
        descriptor = await store.get(request.ref)
        if not descriptor.enabled:
            raise ProtectedValueNotFoundError(
                f"protected value is disabled: {request.ref.qualified}"
            )
        field = descriptor.field(request.field)
        if request.control_kind not in field.compatible_controls:
            raise ProtectedValueStoreError(
                "protected field is incompatible with the current destination control"
            )
        self._require_execution_policy(descriptor.policy, request.execution_mode)
        authorization = await self._authorize_destination(descriptor, request)
        materialization_limit = (
            descriptor.policy.max_unattended_materializations_per_execution
            if request.execution_mode == "unattended"
            else None
        )
        record = await store.reserve(
            request,
            revision=descriptor.revision,
            materialization_limit=materialization_limit,
        )
        try:
            if field.mode == "stored":
                await self.unlock(request.ref.profile)
                payload = await store.read_all(descriptor)
                value = payload.values[field.name]
            else:
                try:
                    async with asyncio.timeout(
                        self._settings.protected_values.prompt_timeout_seconds
                    ):
                        value = await self._secure_value_responder(
                            SecureValueInputRequest(
                                ref=request.ref,
                                field=field.name,
                                label=field.label,
                                top_level_origin=request.top_level_origin,
                                frame_origin=request.frame_origin,
                            )
                        )
                except TimeoutError:
                    value = None
                if value is None:
                    await store.finalize(record, disposition="cancelled")
                    raise ProtectedValueStoreError("secure local value entry was cancelled")
            finalized = await store.finalize(record, disposition="materialized")
        except asyncio.CancelledError:
            with suppress_store_conflict():
                await store.finalize(record, disposition="cancelled")
            raise
        except BaseException:
            with suppress_store_conflict():
                await store.finalize(record, disposition="failed")
            raise
        return ProtectedMaterial(
            use=finalized,
            descriptor=descriptor,
            field=field,
            value=value,
            authorization=authorization,
        )

    async def revalidate(self, material: ProtectedMaterial) -> None:
        """Recheck current resource and destination policy immediately before dispatch."""

        self._ensure_open()
        request = material.use.request
        self._require_ref(request.ref)
        current = await self._store(request.ref.profile).get(request.ref)
        if (
            not current.enabled
            or current.revision != material.descriptor.revision
            or current.field(material.field.name) != material.field
        ):
            raise ProtectedValueConflictError(
                "protected value changed after review; prepare the operation again"
            )
        self._require_execution_policy(current.policy, request.execution_mode)
        await self._revalidate_destination(current, request, material.authorization)

    async def uses(self, profile: str, *, limit: int | None = None) -> list[ProtectedUseRecord]:
        self._require_profile(profile)
        return await self._store(profile).uses(
            limit=limit or self._settings.protected_values.audit_limit
        )

    async def reserve_commit(
        self,
        request: ProtectedCommitRequest,
    ) -> ProtectedCommitRecord:
        """Independently authorize and reserve one protected-source commit."""

        self._ensure_open()
        self._require_ref(request.ref)
        descriptor = await self._store(request.ref.profile).get(request.ref)
        if not descriptor.enabled or descriptor.revision != request.revision:
            raise ProtectedValueConflictError("protected value changed before commit reservation")
        for field in request.fields:
            descriptor.field(field)
        policy = descriptor.policy
        if (
            not policy.unattended_allowed
            or not policy.unattended_commit_allowed
            or policy.max_unattended_commits_per_execution < 1
        ):
            raise ProtectedValueStoreError("protected value forbids unattended commits")
        if request.envelope_kind == "financial" and (
            request.amount_minor is None
            or request.currency is None
            or policy.unattended_currency != request.currency
            or policy.max_unattended_amount_minor < request.amount_minor
        ):
            raise ProtectedValueStoreError(
                "protected-value unattended amount or currency ceiling denied commit"
            )
        return await self._store(request.ref.profile).reserve_commit(
            request,
            commit_limit=policy.max_unattended_commits_per_execution,
        )

    async def finalize_commit(
        self,
        record: ProtectedCommitRecord,
        *,
        disposition: Literal["performed", "not_performed", "in_doubt"],
    ) -> ProtectedCommitRecord:
        """Conservatively finalize one exact protected-source commit reservation."""

        self._ensure_open()
        self._require_ref(record.request.ref)
        return await self._store(record.request.ref.profile).finalize_commit(
            record,
            disposition=disposition,
        )

    async def _authorize_destination(
        self,
        descriptor: ProtectedValueDescriptor,
        request: ProtectedUseRequest,
    ) -> AuthorizationEvidence:
        policy = descriptor.policy
        top = canonical_protected_origin(
            request.top_level_origin, allow_private=policy.mode != "secure_web"
        )
        frame = canonical_protected_origin(
            request.frame_origin, allow_private=policy.mode != "secure_web"
        )
        if top != request.top_level_origin or frame != request.frame_origin:
            raise ProtectedValueStoreError("protected destination origins are not canonical")
        if policy.mode == "secure_web":
            await self._require_public_origins(top, frame)
        if top in policy.authored_origins and frame in policy.authored_origins:
            return "authored"
        store = self._store(descriptor.ref.profile)
        if policy.mode in {"confirm_new", "approved_only"} and await store.is_approved(
            descriptor.ref, top_level_origin=top, frame_origin=frame
        ):
            return "approved"
        if policy.mode == "secure_web":
            return "secure_web"
        if policy.mode != "confirm_new":
            raise ProtectedValueStoreError("protected-value destination policy denied use")
        try:
            async with asyncio.timeout(self._settings.protected_values.prompt_timeout_seconds):
                response = await self._destination_responder(
                    DestinationApprovalRequest(
                        ref=descriptor.ref,
                        revision=descriptor.revision,
                        field=request.field,
                        label=descriptor.label,
                        top_level_origin=top,
                        frame_origin=frame,
                        occurrence=request.occurrence,
                        binding=request.approval_binding,
                        execution_mode=request.execution_mode,
                    )
                )
        except TimeoutError:
            response = DestinationApprovalResponse(decision="deny")
        if response.decision == "deny":
            raise ProtectedValueStoreError("protected-value destination approval was denied")
        if request.execution_mode == "unattended" and response.decision != "allow_once":
            raise ProtectedValueStoreError(
                "unattended destination approval is valid for one execution only"
            )
        if response.decision == "approve":
            await store.approve(descriptor.ref, top_level_origin=top, frame_origin=frame)
            return "approved"
        return "allow_once"

    async def _revalidate_destination(
        self,
        descriptor: ProtectedValueDescriptor,
        request: ProtectedUseRequest,
        evidence: AuthorizationEvidence,
    ) -> None:
        policy = descriptor.policy
        top = canonical_protected_origin(
            request.top_level_origin, allow_private=policy.mode != "secure_web"
        )
        frame = canonical_protected_origin(
            request.frame_origin, allow_private=policy.mode != "secure_web"
        )
        if top != request.top_level_origin or frame != request.frame_origin:
            raise ProtectedValueStoreError("protected destination origins are not canonical")
        if policy.mode == "secure_web":
            await self._require_public_origins(top, frame)
        if evidence == "allow_once" and policy.mode == "confirm_new":
            return
        if top in policy.authored_origins and frame in policy.authored_origins:
            return
        if evidence == "secure_web" and policy.mode == "secure_web":
            return
        if policy.mode in {"confirm_new", "approved_only"} and await self._store(
            descriptor.ref.profile
        ).is_approved(descriptor.ref, top_level_origin=top, frame_origin=frame):
            return
        raise ProtectedValueStoreError("protected-value destination approval was revoked")

    async def _require_public_origins(self, *origins: str) -> None:
        """Resolve every secure-Web origin and reject any non-public answer."""

        for origin in dict.fromkeys(origins):
            parts = urlsplit(origin)
            host = parts.hostname
            if host is None:  # Canonical origins always have a host; fail closed if violated.
                raise ProtectedValueStoreError(
                    "secure-Web protected use requires a public HTTPS origin"
                )
            try:
                literal = ipaddress.ip_address(host)
            except ValueError:
                try:
                    async with asyncio.timeout(self._resolution_timeout_seconds):
                        answers = await self._host_resolver(host, parts.port or 443)
                except TimeoutError as exc:
                    raise ProtectedValueStoreError(
                        "secure-Web destination host resolution timed out"
                    ) from exc
                except Exception as exc:
                    raise ProtectedValueStoreError(
                        "secure-Web destination host could not be resolved"
                    ) from exc
                if not answers:
                    raise ProtectedValueStoreError(
                        "secure-Web destination host did not resolve"
                    ) from None
                try:
                    addresses = tuple(ipaddress.ip_address(answer) for answer in answers)
                except ValueError as exc:
                    raise ProtectedValueStoreError(
                        "secure-Web destination host returned an invalid address"
                    ) from exc
            else:
                addresses = (literal,)
            if any(_non_public_address(address) for address in addresses):
                raise ProtectedValueStoreError(
                    "secure-Web protected use requires a public HTTPS origin"
                )

    @staticmethod
    def _require_execution_policy(policy: ProtectedDestinationPolicy, execution_mode: str) -> None:
        if execution_mode == "foreground":
            if not policy.foreground_allowed:
                raise ProtectedValueStoreError("protected value forbids foreground use")
            return
        if (
            not policy.unattended_allowed
            or policy.max_unattended_materializations_per_execution < 1
        ):
            raise ProtectedValueStoreError("protected value forbids unattended use")

    def _store(self, profile: str) -> ProtectedValueBackend:
        self._ensure_open()
        self._require_profile(profile)
        store = self._stores.get(profile)
        if store is None:
            store = (
                self._backend_provider.backend(profile)
                if self._backend_provider is not None
                else self._backend_factory(self._settings, profile)
            )
            self._stores[profile] = store
        return store

    def _require_profile(self, profile: str) -> None:
        profile = validate_profile_name(profile)
        if not self.scope.includes(profile):
            raise ProtectedValueNotFoundError(f"protected-value profile is unavailable: {profile}")

    def _require_ref(self, ref: ProfileResourceRef) -> None:
        self._require_profile(ref.profile)

    def _ensure_open(self) -> None:
        if self._closed:
            raise ProtectedValueStoreError("protected-value broker is closed")
        if self._backend_provider is not None:
            self._backend_provider.ensure_open()


class suppress_store_conflict:
    """Suppress only a second finalization attempt while preserving stronger errors."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        return isinstance(exc, ProtectedValueConflictError)


def canonical_protected_origin(value: str, *, allow_private: bool) -> str:
    """Return one exact HTTPS origin, rejecting credentials and non-canonical forms."""

    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ProtectedValueStoreError("protected-value destination origin is invalid") from exc
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise ProtectedValueStoreError("protected values require an exact HTTPS origin")
    try:
        host = parts.hostname.encode("idna").decode("ascii").casefold().rstrip(".")
    except UnicodeError as exc:
        raise ProtectedValueStoreError("protected-value destination origin is invalid") from exc
    if not allow_private and _private_host(host):
        raise ProtectedValueStoreError("secure-Web protected use requires a public HTTPS origin")
    rendered_host = f"[{host}]" if ":" in host else host
    origin = f"https://{rendered_host}"
    if port is not None and port != 443:
        origin += f":{port}"
    return origin


def _private_host(host: str) -> bool:
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return _non_public_address(address)


def _non_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return address.is_multicast or not address.is_global


def _canonical_policy(policy: ProtectedDestinationPolicy) -> ProtectedDestinationPolicy:
    origins = tuple(
        canonical_protected_origin(
            origin,
            allow_private=policy.mode != "secure_web",
        )
        for origin in policy.authored_origins
    )
    canonical = policy.model_copy(update={"authored_origins": origins})
    return ProtectedDestinationPolicy.model_validate(canonical.model_dump(), strict=True)
