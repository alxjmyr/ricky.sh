"""Regression coverage for protected-value vault storage boundaries."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import SecretStr

from ricky.config import ProtectedValuesSettings, RickySettings
from ricky.protected_values import (
    ProfileVaultStore,
    ProtectedDestinationPolicy,
    ProtectedFieldDescriptor,
    ProtectedValueKind,
    VaultLockedError,
)

_PASSPHRASE = "store-regression-passphrase"
_NEW_PASSPHRASE = "store-regression-new-passphrase"
_VALUE = "store-regression-secret"


def _settings(
    tmp_path: Path,
    *,
    iterations: int = 1,
    lanes: int = 1,
    memory_kib: int = 8_192,
    catalog_limit: int = 2,
) -> RickySettings:
    return RickySettings(
        user_data_dir=str(tmp_path / "user"),
        project_data_dir=str(tmp_path / "project" / ".ricky"),
        protected_values=ProtectedValuesSettings(
            enabled=True,
            catalog_limit=catalog_limit,
            argon2_iterations=iterations,
            argon2_lanes=lanes,
            argon2_memory_kib=memory_kib,
        ),
    )


async def _create(
    store: ProfileVaultStore,
    name: str,
    *,
    kind: ProtectedValueKind = "credential",
) -> None:
    await store.create(
        name=name,
        kind=kind,
        label=name,
        description="",
        fields=(
            ProtectedFieldDescriptor(
                name="password",
                label="Password",
                mode="stored",
                compatible_controls=("password",),
            ),
        ),
        policy=ProtectedDestinationPolicy(mode="strict", authored_origins=("https://example.com",)),
        values={"password": SecretStr(_VALUE)},
    )


def _slot_parameters(path: Path) -> dict[str, object]:
    with sqlite3.connect(path) as connection:
        raw = connection.execute(
            """
            SELECT kdf_parameters_json FROM unlock_slots
            WHERE id = 'interactive-passphrase'
            """
        ).fetchone()[0]
    decoded = json.loads(raw)
    assert isinstance(decoded, dict)
    return decoded


@pytest.mark.asyncio
async def test_unlock_uses_slot_kdf_parameters_after_configuration_changes(
    tmp_path: Path,
) -> None:
    initial = _settings(tmp_path)
    store = ProfileVaultStore(initial, "personal")
    await store.initialize(SecretStr(_PASSPHRASE))
    await store.unlock(SecretStr(_PASSPHRASE))
    await _create(store, "login")
    descriptor = (await store.list(limit=1))[0]
    await store.lock()

    changed = _settings(tmp_path, iterations=2, lanes=2, memory_kib=16_384)
    reopened = ProfileVaultStore(changed, "personal")
    await reopened.unlock(SecretStr(_PASSPHRASE))

    assert (await reopened.read_all(descriptor)).values["password"] == SecretStr(_VALUE)
    assert _slot_parameters(reopened.path) == {
        "algorithm": "argon2id",
        "length": 32,
        "iterations": 1,
        "lanes": 1,
        "memory_kib": 8_192,
    }


@pytest.mark.asyncio
async def test_rotation_atomically_adopts_current_kdf_parameters(tmp_path: Path) -> None:
    initial = _settings(tmp_path)
    original = ProfileVaultStore(initial, "personal")
    await original.initialize(SecretStr(_PASSPHRASE))
    await original.unlock(SecretStr(_PASSPHRASE))
    await _create(original, "login")
    descriptor = (await original.list(limit=1))[0]
    with sqlite3.connect(original.path) as connection:
        payload_before = connection.execute(
            "SELECT payload FROM protected_resources WHERE name = 'login'"
        ).fetchone()[0]
    await original.lock()

    rotation_settings = _settings(tmp_path, iterations=2, lanes=2, memory_kib=16_384)
    rotating = ProfileVaultStore(rotation_settings, "personal")
    await rotating.unlock(SecretStr(_PASSPHRASE))
    await rotating.rotate_passphrase(SecretStr(_NEW_PASSPHRASE))
    await rotating.lock()

    assert _slot_parameters(rotating.path) == {
        "algorithm": "argon2id",
        "length": 32,
        "iterations": 2,
        "lanes": 2,
        "memory_kib": 16_384,
    }
    with sqlite3.connect(rotating.path) as connection:
        payload_after = connection.execute(
            "SELECT payload FROM protected_resources WHERE name = 'login'"
        ).fetchone()[0]
    assert payload_after == payload_before

    reopened = ProfileVaultStore(initial, "personal")
    with pytest.raises(VaultLockedError, match="unlock failed"):
        await reopened.unlock(SecretStr(_PASSPHRASE))
    await reopened.unlock(SecretStr(_NEW_PASSPHRASE))
    assert (await reopened.read_all(descriptor)).values["password"] == SecretStr(_VALUE)


@pytest.mark.asyncio
async def test_count_is_uncapped_and_kind_filter_precedes_limit(tmp_path: Path) -> None:
    store = ProfileVaultStore(_settings(tmp_path, catalog_limit=2), "personal")
    await store.initialize(SecretStr(_PASSPHRASE))
    await store.unlock(SecretStr(_PASSPHRASE))
    await _create(store, "a-generic", kind="generic")
    await _create(store, "b-generic", kind="generic")
    await _create(store, "y-credential")
    await _create(store, "z-credential")

    assert await store.count() == 4
    assert [item.ref.name for item in await store.list(limit=2)] == [
        "a-generic",
        "b-generic",
    ]
    assert [item.ref.name for item in await store.list(limit=2, kind="credential")] == [
        "y-credential",
        "z-credential",
    ]
