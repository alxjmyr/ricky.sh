"""Protected-value encryption, scope, policy, approval, and materialization tests."""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import threading
from pathlib import Path

import pytest
from pydantic import SecretStr

from ricky.config import ProtectedValuesSettings, RickySettings
from ricky.profiles import ProfileResourceRef, ProfileScope
from ricky.protected_values import (
    DestinationApprovalResponse,
    ProfileVaultStore,
    ProtectedCommitRequest,
    ProtectedDestinationPolicy,
    ProtectedFieldDescriptor,
    ProtectedUseRequest,
    ProtectedValueBroker,
    ProtectedValueConflictError,
    ProtectedValueNotFoundError,
    ProtectedValueStoreError,
    VaultLockedError,
    canonical_protected_origin,
)
from ricky.protected_values.store import SCHEMA_VERSION as PROTECTED_SCHEMA_VERSION
from ricky.protected_values.types import MaterializationMode
from ricky.protected_values.upgrade import ProtectedValuesUpgradeAdapter

SENTINEL = "phase5-protected-sentinel-7319"
PASSPHRASE = "phase5-vault-passphrase"
ORIGIN = "https://example.com"


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings(
        user_data_dir=str(tmp_path / "user"),
        project_data_dir=str(tmp_path / "project" / ".ricky"),
        protected_values=ProtectedValuesSettings(
            enabled=True,
            argon2_iterations=1,
            argon2_lanes=1,
            argon2_memory_kib=8_192,
        ),
    )


def _fields(
    *, password_mode: MaterializationMode = "stored"
) -> tuple[ProtectedFieldDescriptor, ...]:
    return (
        ProtectedFieldDescriptor(
            name="username",
            label="Account username",
            mode="stored",
            compatible_controls=("username",),
        ),
        ProtectedFieldDescriptor(
            name="password",
            label="Account password",
            mode=password_mode,
            compatible_controls=("password",),
        ),
    )


async def _broker(
    tmp_path: Path,
    *,
    policy: ProtectedDestinationPolicy | None = None,
    password_mode: MaterializationMode = "stored",
    unlock: bool = True,
    destination_responder=None,
    secure_value_responder=None,
) -> tuple[ProtectedValueBroker, ProfileResourceRef]:
    settings = _settings(tmp_path)
    kwargs = {}
    if destination_responder is not None:
        kwargs["destination_responder"] = destination_responder
    if secure_value_responder is not None:
        kwargs["secure_value_responder"] = secure_value_responder
    broker = ProtectedValueBroker(
        settings,
        scope=ProfileScope.create("personal"),
        consumer_ids=frozenset({"browser.fill"}),
        **kwargs,
    )
    await broker.initialize("personal", SecretStr(PASSPHRASE))
    if unlock:
        await broker.unlock("personal", SecretStr(PASSPHRASE))
    descriptor = await broker.create(
        profile="personal",
        name="example-login",
        kind="credential",
        label="Example login",
        description="Account used on the example fixture.",
        fields=_fields(password_mode=password_mode),
        policy=policy or ProtectedDestinationPolicy(mode="strict", authored_origins=(ORIGIN,)),
        values={
            "username": SecretStr("ricky-user"),
            **({"password": SecretStr(SENTINEL)} if password_mode == "stored" else {}),
        },
    )
    return broker, descriptor.ref


def _request(ref: ProfileResourceRef, *, field: str = "password") -> ProtectedUseRequest:
    return ProtectedUseRequest(
        ref=ref,
        field=field,
        consumer_id="browser.fill",
        control_kind="password" if field == "password" else "username",
        top_level_origin=ORIGIN,
        frame_origin=ORIGIN,
        occurrence="browser-session/page/snapshot/ref",
    )


@pytest.mark.asyncio
async def test_encrypted_vault_catalog_unlock_and_materialization(tmp_path: Path) -> None:
    broker, ref = await _broker(tmp_path)
    descriptor = (await broker.catalog(ref=ref))[0]

    assert descriptor.ref == ref
    assert descriptor.field("password").mode == "stored"
    material = await broker.prepare(_request(ref))
    assert material.value.get_secret_value() == SENTINEL
    assert material.use.disposition == "materialized"
    assert SENTINEL not in material.use.model_dump_json()
    assert PASSPHRASE not in material.use.model_dump_json()

    vault = tmp_path / "user" / "profiles" / "personal" / "protected-values"
    bodies = b"".join(path.read_bytes() for path in vault.iterdir() if path.is_file())
    assert SENTINEL.encode() not in bodies
    assert PASSPHRASE.encode() not in bodies
    assert not (tmp_path / "project").exists()

    await broker.aclose()


@pytest.mark.asyncio
async def test_locked_catalog_is_safe_and_wrong_passphrase_fails(tmp_path: Path) -> None:
    broker, ref = await _broker(tmp_path)
    await broker.aclose()

    locked = ProtectedValueBroker(
        _settings(tmp_path),
        scope=ProfileScope.create("personal"),
        consumer_ids=frozenset({"browser.fill"}),
    )
    assert [item.ref for item in await locked.catalog()] == [ref]
    with pytest.raises(VaultLockedError, match="unlock failed"):
        await locked.unlock("personal", SecretStr("wrong-passphrase"))


@pytest.mark.asyncio
async def test_passphrase_rotation_rewraps_key_without_reencrypting_payload(tmp_path: Path) -> None:
    broker, ref = await _broker(tmp_path)
    path = (
        tmp_path
        / "user"
        / "profiles"
        / "personal"
        / "protected-values"
        / "protected-values.sqlite3"
    )
    with sqlite3.connect(path) as connection:
        before = connection.execute(
            "SELECT payload FROM protected_resources WHERE name = ?", (ref.name,)
        ).fetchone()[0]
    await broker.rotate_passphrase("personal", SecretStr("new-passphrase"))
    await broker.aclose()
    with sqlite3.connect(path) as connection:
        after = connection.execute(
            "SELECT payload FROM protected_resources WHERE name = ?", (ref.name,)
        ).fetchone()[0]
    assert before == after

    reopened = ProtectedValueBroker(
        _settings(tmp_path),
        scope=ProfileScope.create("personal"),
        consumer_ids=frozenset({"browser.fill"}),
    )
    with pytest.raises(VaultLockedError):
        await reopened.unlock("personal", SecretStr(PASSPHRASE))
    await reopened.unlock("personal", SecretStr("new-passphrase"))
    assert (await reopened.prepare(_request(ref))).value.get_secret_value() == SENTINEL


@pytest.mark.asyncio
async def test_vault_permissions_slot_collection_and_delete_preserve_audit(
    tmp_path: Path,
) -> None:
    broker, ref = await _broker(tmp_path)
    await broker.prepare(_request(ref))
    descriptor = (await broker.catalog(ref=ref))[0]
    await broker.delete(ref, expected_revision=descriptor.revision)

    assert await broker.catalog() == []
    uses = await broker.uses("personal")
    assert len(uses) == 1
    assert uses[0].request.ref == ref
    path = (
        tmp_path
        / "user"
        / "profiles"
        / "personal"
        / "protected-values"
        / "protected-values.sqlite3"
    )
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM unlock_slots").fetchone()[0] == 1
        assert connection.execute("SELECT resource_name FROM protected_uses").fetchone()[0] is None
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_copied_profile_corrupt_payload_and_unknown_schema_fail_closed(
    tmp_path: Path,
) -> None:
    broker, ref = await _broker(tmp_path)
    source = (
        tmp_path
        / "user"
        / "profiles"
        / "personal"
        / "protected-values"
        / "protected-values.sqlite3"
    )
    copied = (
        tmp_path / "user" / "profiles" / "work" / "protected-values" / "protected-values.sqlite3"
    )
    copied.parent.mkdir(parents=True)
    shutil.copy2(source, copied)
    wrong_owner = ProtectedValueBroker(
        _settings(tmp_path),
        scope=ProfileScope.create("work"),
        consumer_ids=frozenset({"browser.fill"}),
    )
    with pytest.raises(ProtectedValueStoreError, match="owner does not match"):
        await wrong_owner.unlock("work", SecretStr(PASSPHRASE))

    with sqlite3.connect(source) as connection:
        connection.execute(
            "UPDATE protected_resources SET payload = ? WHERE name = ?",
            (b"corrupt-authenticated-envelope", ref.name),
        )
        connection.commit()
    with pytest.raises(ProtectedValueStoreError, match="failed authentication"):
        await broker.prepare(_request(ref))
    await broker.aclose()

    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA user_version = 999")
    incompatible = ProtectedValueBroker(
        _settings(tmp_path),
        scope=ProfileScope.create("personal"),
    )
    with pytest.raises(ProtectedValueStoreError, match="unsupported.*schema"):
        await incompatible.catalog()


@pytest.mark.asyncio
async def test_phase7_protected_commit_schema_migrates_additively(tmp_path: Path) -> None:
    broker, _ = await _broker(tmp_path)
    await broker.aclose()
    path = (
        tmp_path
        / "user"
        / "profiles"
        / "personal"
        / "protected-values"
        / "protected-values.sqlite3"
    )
    with sqlite3.connect(path) as database:
        database.execute("DROP TABLE protected_commits")
        database.execute("PRAGMA user_version = 1")

    restarted = ProtectedValueBroker(
        _settings(tmp_path),
        scope=ProfileScope.create("personal"),
        consumer_ids=frozenset({"browser.fill"}),
    )
    before = path.read_bytes()
    with pytest.raises(ProtectedValueStoreError, match="unsupported.*schema"):
        await restarted.catalog()
    assert path.read_bytes() == before

    adapter = ProtectedValuesUpgradeAdapter((path,))
    target = adapter.discover(user_data_dir=tmp_path / "user")[0]
    inspection = adapter.inspect(target)
    assert inspection.state == "migration_required"
    assert adapter.preflight(inspection).backup_paths == (str(path),)
    (step,) = adapter.plan_steps(source_data_generation=1, target_data_generation=1)
    adapter.apply(step)
    assert adapter.verify(target).state == "current"

    assert await restarted.catalog()
    await restarted.aclose()

    with sqlite3.connect(path) as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] == PROTECTED_SCHEMA_VERSION
        assert (
            database.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='protected_commits'"
            ).fetchone()
            is not None
        )


@pytest.mark.asyncio
async def test_cancelled_initialization_joins_atomic_publication(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingStore(ProfileVaultStore):
        def _wrap_key(self, data_key, passphrase, salt, *, parameters):
            entered.set()
            release.wait(timeout=2)
            return super()._wrap_key(data_key, passphrase, salt, parameters=parameters)

    settings = _settings(tmp_path)
    store = BlockingStore(settings, "personal")

    def backend_factory(settings: RickySettings, profile: str) -> ProfileVaultStore:
        del settings, profile
        return store

    broker = ProtectedValueBroker(
        settings,
        scope=ProfileScope.create("personal"),
        backend_factory=backend_factory,
    )
    operation = asyncio.create_task(broker.initialize("personal", SecretStr(PASSPHRASE)))
    await asyncio.to_thread(entered.wait, 2)
    operation.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert (await broker.status("personal")).initialized
    assert not list(store.root.glob(".protected-values-*.tmp"))


@pytest.mark.asyncio
async def test_confirm_new_allow_once_and_durable_approval(tmp_path: Path) -> None:
    responses = iter(("allow_once", "approve"))

    async def respond(_request):
        return DestinationApprovalResponse(decision=next(responses))

    policy = ProtectedDestinationPolicy(mode="confirm_new")
    broker, ref = await _broker(tmp_path, policy=policy, destination_responder=respond)
    first = await broker.prepare(_request(ref))
    assert first.authorization == "allow_once"
    assert await broker.approvals(ref) == []
    await broker.revalidate(first)

    second = await broker.prepare(_request(ref))
    assert second.authorization == "approved"
    approvals = await broker.approvals(ref)
    assert [(item.top_level_origin, item.frame_origin) for item in approvals] == [(ORIGIN, ORIGIN)]
    await broker.revalidate(second)
    assert await broker.revoke_approval(ref, top_level_origin=ORIGIN, frame_origin=ORIGIN)
    with pytest.raises(ProtectedValueStoreError, match="revoked"):
        await broker.revalidate(second)


@pytest.mark.asyncio
async def test_unattended_confirm_new_allows_only_one_execution_destination(
    tmp_path: Path,
) -> None:
    requests = []

    async def allow_once(request):
        requests.append(request)
        return DestinationApprovalResponse(decision="allow_once")

    policy = ProtectedDestinationPolicy(
        mode="confirm_new",
        unattended_allowed=True,
        max_unattended_materializations_per_execution=2,
    )
    broker, ref = await _broker(
        tmp_path,
        policy=policy,
        destination_responder=allow_once,
    )
    request = _request(ref).model_copy(
        update={
            "execution_mode": "unattended",
            "execution_id": "execution_11111111111111111111111111111111",
        }
    )
    material = await broker.prepare(request)

    assert material.authorization == "allow_once"
    assert await broker.approvals(ref) == []
    assert len(requests) == 1
    assert requests[0].revision == material.descriptor.revision
    assert requests[0].field == "password"
    assert requests[0].occurrence == request.occurrence
    assert requests[0].execution_mode == "unattended"


@pytest.mark.asyncio
async def test_unattended_confirm_new_cannot_create_durable_approval(tmp_path: Path) -> None:
    async def approve(_request):
        return DestinationApprovalResponse(decision="approve")

    policy = ProtectedDestinationPolicy(
        mode="confirm_new",
        unattended_allowed=True,
        max_unattended_materializations_per_execution=1,
    )
    broker, ref = await _broker(
        tmp_path,
        policy=policy,
        destination_responder=approve,
    )
    request = _request(ref).model_copy(
        update={
            "execution_mode": "unattended",
            "execution_id": "execution_22222222222222222222222222222222",
        }
    )

    with pytest.raises(ProtectedValueStoreError, match="one execution only"):
        await broker.prepare(request)
    assert await broker.approvals(ref) == []


@pytest.mark.asyncio
async def test_unattended_materialization_ceiling_is_durable_per_execution(
    tmp_path: Path,
) -> None:
    policy = ProtectedDestinationPolicy(
        mode="strict",
        authored_origins=(ORIGIN,),
        unattended_allowed=True,
        max_unattended_materializations_per_execution=2,
    )
    broker, ref = await _broker(tmp_path, policy=policy)
    request = _request(ref).model_copy(
        update={
            "execution_mode": "unattended",
            "execution_id": "execution_33333333333333333333333333333333",
        }
    )

    await broker.prepare(request)
    await broker.prepare(request)
    await broker.aclose()

    restarted = ProtectedValueBroker(
        _settings(tmp_path),
        scope=ProfileScope.create("personal"),
        consumer_ids=frozenset({"browser.fill"}),
    )
    await restarted.unlock("personal", SecretStr(PASSPHRASE))
    with pytest.raises(ProtectedValueStoreError, match="materialization ceiling"):
        await restarted.prepare(request)

    other_execution = request.model_copy(
        update={"execution_id": "execution_44444444444444444444444444444444"}
    )
    assert (await restarted.prepare(other_execution)).use.disposition == "materialized"


@pytest.mark.asyncio
async def test_unattended_commit_policy_enforces_revision_amount_currency_and_count(
    tmp_path: Path,
) -> None:
    policy = ProtectedDestinationPolicy(
        mode="strict",
        authored_origins=(ORIGIN,),
        unattended_allowed=True,
        max_unattended_materializations_per_execution=2,
        unattended_commit_allowed=True,
        max_unattended_commits_per_execution=1,
        max_unattended_amount_minor=1_500,
        unattended_currency="USD",
    )
    broker, ref = await _broker(tmp_path, policy=policy)
    request = ProtectedCommitRequest(
        execution_id="execution_55555555555555555555555555555555",
        ref=ref,
        revision=1,
        fields=("password",),
        logical_effect_key="a" * 64,
        envelope_sha256="b" * 64,
        envelope_kind="financial",
        amount_minor=1_375,
        currency="USD",
    )

    record = await broker.reserve_commit(request)
    assert (await broker.finalize_commit(record, disposition="performed")).disposition == (
        "performed"
    )
    with pytest.raises(ProtectedValueStoreError, match="commit ceiling"):
        await broker.reserve_commit(
            request.model_copy(
                update={
                    "logical_effect_key": "c" * 64,
                    "envelope_sha256": "d" * 64,
                }
            )
        )
    with pytest.raises(ProtectedValueStoreError, match="amount or currency"):
        await broker.reserve_commit(
            request.model_copy(
                update={
                    "execution_id": "execution_66666666666666666666666666666666",
                    "amount_minor": 1_501,
                }
            )
        )


@pytest.mark.asyncio
async def test_durable_approval_never_widens_strict_policy(tmp_path: Path) -> None:
    broker, ref = await _broker(
        tmp_path,
        policy=ProtectedDestinationPolicy(mode="strict"),
    )
    await broker.approve(ref, top_level_origin=ORIGIN, frame_origin=ORIGIN)

    with pytest.raises(ProtectedValueStoreError, match="destination policy denied"):
        await broker.prepare(_request(ref))


@pytest.mark.asyncio
async def test_prompt_each_use_value_is_never_persisted(tmp_path: Path) -> None:
    prompts = []

    async def secure_value(request):
        prompts.append(request)
        return SecretStr(SENTINEL)

    broker, ref = await _broker(
        tmp_path,
        password_mode="prompt_each_use",
        secure_value_responder=secure_value,
    )
    material = await broker.prepare(_request(ref))
    assert material.value.get_secret_value() == SENTINEL
    assert len(prompts) == 1
    vault = tmp_path / "user" / "profiles" / "personal" / "protected-values"
    assert SENTINEL.encode() not in b"".join(
        path.read_bytes() for path in vault.iterdir() if path.is_file()
    )


@pytest.mark.asyncio
async def test_scope_consumer_field_and_destination_are_independent_ceilings(
    tmp_path: Path,
) -> None:
    broker, ref = await _broker(tmp_path)
    bad_consumer = _request(ref).model_copy(update={"consumer_id": "unregistered"})
    with pytest.raises(ProtectedValueStoreError, match="not registered"):
        await broker.prepare(bad_consumer)

    bad_control = _request(ref).model_copy(update={"control_kind": "card_number"})
    with pytest.raises(ProtectedValueStoreError, match="incompatible"):
        await broker.prepare(bad_control)

    bad_origin = _request(ref).model_copy(
        update={"top_level_origin": "https://other.example", "frame_origin": ORIGIN}
    )
    with pytest.raises(ProtectedValueStoreError, match="destination policy"):
        await broker.prepare(bad_origin)

    narrow = ProtectedValueBroker(
        _settings(tmp_path),
        scope=ProfileScope.create("work"),
        consumer_ids=frozenset({"browser.fill"}),
    )
    with pytest.raises(ProtectedValueNotFoundError, match="profile is unavailable"):
        await narrow.catalog(ref=ref)
    with pytest.raises(ProtectedValueNotFoundError, match="profile is unavailable"):
        await narrow.prepare(_request(ref))


def test_protected_origin_requires_exact_https_and_public_secure_web() -> None:
    assert canonical_protected_origin("https://example.com:443", allow_private=False) == ORIGIN
    assert canonical_protected_origin("https://127.0.0.1:9443", allow_private=True) == (
        "https://127.0.0.1:9443"
    )
    assert canonical_protected_origin("https://B\u00dcCHER.example.:443", allow_private=True) == (
        "https://xn--bcher-kva.example"
    )
    with pytest.raises(ProtectedValueStoreError, match="exact HTTPS"):
        canonical_protected_origin("http://example.com", allow_private=True)
    with pytest.raises(ProtectedValueStoreError, match="public HTTPS"):
        canonical_protected_origin("https://127.0.0.1", allow_private=False)
    with pytest.raises(ProtectedValueStoreError, match="exact HTTPS"):
        canonical_protected_origin("https://user:password@example.com", allow_private=True)


@pytest.mark.asyncio
async def test_resource_revision_invalidates_prepared_material(tmp_path: Path) -> None:
    broker, ref = await _broker(tmp_path)
    material = await broker.prepare(_request(ref))
    descriptor = (await broker.catalog(ref=ref))[0]
    payload = await broker._store("personal").read_all(descriptor)
    await broker.replace(
        descriptor,
        label=descriptor.label,
        description=descriptor.description,
        fields=descriptor.fields,
        policy=descriptor.policy,
        values=payload.values,
    )
    with pytest.raises(ProtectedValueConflictError, match="changed after review"):
        await broker.revalidate(material)


@pytest.mark.asyncio
async def test_cancelled_prompt_finalizes_without_material(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def secure_value(_request):
        started.set()
        await release.wait()
        return SecretStr(SENTINEL)

    broker, ref = await _broker(
        tmp_path,
        password_mode="prompt_each_use",
        secure_value_responder=secure_value,
    )
    task = asyncio.create_task(broker.prepare(_request(ref)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    uses = await broker.uses("personal")
    assert uses[0].disposition == "cancelled"
    assert SENTINEL not in uses[0].model_dump_json()
