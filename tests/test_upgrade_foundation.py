"""Phase-one contracts for Ricky's released-installation upgrade foundation."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from ricky.installation import (
    INSTALLATION_FILENAME,
    InstallationError,
    InstallationManifest,
    MigrationState,
    OperationLockMode,
    bootstrap_config_dir,
    initialize_installation,
    installation_operation_lock,
    require_compatible_installation,
    transition_installation_manifest,
)
from ricky.upgrades import (
    MigrationRegistry,
    ReleaseDescriptor,
    ReleaseVersion,
    check_upgrade,
    plan_upgrade,
)


def _legacy_manifest_document() -> dict[str, Any]:
    """Return the exact format-1 shape written before upgrade support existed."""

    return {
        "format_version": 1,
        "installation_id": "a" * 32,
        "created_by_version": "0.5",
        "last_lifecycle_version": "0.5",
        "created_at": "2026-09-01T00:00:00Z",
        "migration_state": "clean",
    }


def _manifest(root: Path) -> InstallationManifest:
    return InstallationManifest.model_validate_json(
        (root / INSTALLATION_FILENAME).read_text(encoding="utf-8")
    )


def _parse_manifest_document(document: dict[str, Any]) -> InstallationManifest:
    """Parse a document through the durable JSON boundary, not Python coercion."""

    return InstallationManifest.model_validate_json(json.dumps(document))


def _tree_snapshot(root: Path) -> dict[str, tuple[str, bytes | None, int, int]]:
    return {
        str(path.relative_to(root)): (
            "directory" if path.is_dir() else "file",
            path.read_bytes() if path.is_file() else None,
            path.stat().st_mode,
            path.stat().st_mtime_ns,
        )
        for path in (root, *sorted(root.rglob("*")))
    }


def _release_descriptor_document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "format_version": 1,
        "repository": "alxjmyr/ricky.sh",
        "channel": "stable",
        "software_version": "0.6.1",
        "supported_source_data_generations": (1,),
        "target_data_generation": 1,
        "python_requirement": ">=3.12",
        "minimum_uv_version": "0.8.0",
        "wheel": {
            "name": "ricky-0.6.1-py3-none-any.whl",
            "url": (
                "https://github.com/alxjmyr/ricky.sh/releases/download/"
                "v0.6.1/ricky-0.6.1-py3-none-any.whl"
            ),
            "sha256": "a" * 64,
            "size": 100,
        },
        "constraints": {
            "name": "ricky-0.6.1-constraints.txt",
            "url": (
                "https://github.com/alxjmyr/ricky.sh/releases/download/"
                "v0.6.1/ricky-0.6.1-constraints.txt"
            ),
            "sha256": "b" * 64,
            "size": 100,
        },
    }
    document.update(overrides)
    return document


def test_existing_format_one_manifest_gets_upgrade_defaults_without_rewrite() -> None:
    document = _legacy_manifest_document()

    manifest = _parse_manifest_document(document)

    assert manifest.data_generation == 1
    assert manifest.migration_state == "clean"
    assert manifest.operation_id is None
    assert "data_generation" not in document
    assert "operation_id" not in document


def test_fresh_initialization_persists_generation_one_and_clean_gate(tmp_path: Path) -> None:
    root = tmp_path / "ricky-data"

    initialize_installation(root, now=datetime(2026, 9, 2, tzinfo=UTC))

    raw = json.loads((root / INSTALLATION_FILENAME).read_text(encoding="utf-8"))
    assert raw["data_generation"] == 1
    assert raw["migration_state"] == "clean"
    assert raw["operation_id"] is None
    assert _manifest(root).data_generation == 1


@pytest.mark.parametrize(
    "migration_state",
    [
        "prepared",
        "software_replaced",
        "migrating",
        "failed",
        "rolling_back",
    ],
)
def test_nonclean_manifest_requires_an_operation_id(migration_state: MigrationState) -> None:
    document = _legacy_manifest_document() | {"migration_state": migration_state}

    with pytest.raises(ValidationError, match="operation_id"):
        _parse_manifest_document(document)

    parsed = _parse_manifest_document(document | {"operation_id": "b" * 32})
    assert parsed.operation_id == "b" * 32


def test_clean_manifest_refuses_an_operation_id() -> None:
    with pytest.raises(ValidationError, match="operation_id"):
        _parse_manifest_document(_legacy_manifest_document() | {"operation_id": "b" * 32})


@pytest.mark.parametrize(
    "value",
    ["0.6", "1.0", "0.6.0rc1", "0.6.0+local", "v0.6.0", "0.6.0.1", "01.2.3"],
)
def test_release_version_refuses_every_noncanonical_shape(value: str) -> None:
    with pytest.raises(ValueError):
        ReleaseVersion.parse(value)


def test_release_version_parses_and_orders_three_integer_components() -> None:
    versions = [ReleaseVersion.parse(value) for value in ("1.0.0", "0.10.0", "0.9.9")]

    assert str(ReleaseVersion.parse("0.6.0")) == "0.6.0"
    assert sorted(versions) == [
        ReleaseVersion.parse("0.9.9"),
        ReleaseVersion.parse("0.10.0"),
        ReleaseVersion.parse("1.0.0"),
    ]


def test_release_descriptor_is_strict_and_json_round_trip_safe() -> None:
    descriptor = ReleaseDescriptor.model_validate(_release_descriptor_document())

    assert descriptor.software_version == ReleaseVersion.parse("0.6.1")
    assert ReleaseDescriptor.model_validate_json(descriptor.model_dump_json()) == descriptor

    with pytest.raises(ValidationError):
        ReleaseDescriptor.model_validate(_release_descriptor_document(unexpected=True))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "someone-else/ricky"),
        ("software_version", "0.6.1rc1"),
        (
            "wheel",
            {
                "name": "other-0.6.1-py3-none-any.whl",
                "url": "https://github.com/alxjmyr/ricky.sh/releases/download/v0.6.1/other.whl",
                "sha256": "a" * 64,
                "size": 100,
            },
        ),
        ("supported_source_data_generations", []),
        ("target_data_generation", 0),
        ("python_requirement", "any"),
        ("minimum_uv_version", "0.8"),
    ],
)
def test_release_descriptor_refuses_ambiguous_or_incoherent_values(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        ReleaseDescriptor.model_validate(_release_descriptor_document(**{field: value}))


def test_release_descriptor_refuses_an_invalid_artifact_digest() -> None:
    document = _release_descriptor_document()
    document["wheel"] = {**document["wheel"], "sha256": "not-a-sha256"}

    with pytest.raises(ValidationError):
        ReleaseDescriptor.model_validate(document)


def test_empty_current_generation_plan_is_deterministic() -> None:
    registry = MigrationRegistry()

    first = plan_upgrade(registry=registry, source_generation=1, target_generation=1)
    second = plan_upgrade(registry=registry, source_generation=1, target_generation=1)

    assert first == second
    assert first.steps == ()
    assert first.plan_digest == second.plan_digest
    assert len(first.plan_digest) == 64


@pytest.mark.asyncio
async def test_check_without_a_descriptor_source_is_read_only_and_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ricky-data"
    initialize_installation(root)
    authored = root / "profiles" / "shared" / "SOUL.md"
    authored.write_text("preserve exactly\n", encoding="utf-8")
    before_data = _tree_snapshot(root)
    before_bootstrap = _tree_snapshot(bootstrap_config_dir())

    def network_forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("upgrade check attempted network access without a descriptor source")

    monkeypatch.setattr("httpx.Client.request", network_forbidden)
    monkeypatch.setattr("httpx.AsyncClient.request", network_forbidden)

    result = await check_upgrade(descriptor_source=None)

    assert _tree_snapshot(root) == before_data
    assert _tree_snapshot(bootstrap_config_dir()) == before_bootstrap
    assert result.current_data_generation == 1
    assert result.selected_release is None
    assert result.status == "no_update"
    assert result.compatibility.compatible is True
    assert not (root / "upgrades").exists()


def test_two_shared_operation_locks_can_coexist(tmp_path: Path) -> None:
    directory = tmp_path / "locks"

    with (
        installation_operation_lock(
            mode="shared",
            timeout_seconds=0.1,
            operation="test-shared-one",
            directory=directory,
        ),
        installation_operation_lock(
            mode="shared",
            timeout_seconds=0.1,
            operation="test-shared-two",
            directory=directory,
        ),
    ):
        pass


@pytest.mark.parametrize(
    ("held", "requested"),
    [
        ("shared", "exclusive"),
        ("exclusive", "shared"),
        ("exclusive", "exclusive"),
    ],
)
def test_conflicting_operation_lock_refuses_within_a_bounded_timeout(
    tmp_path: Path, held: OperationLockMode, requested: OperationLockMode
) -> None:
    directory = tmp_path / "locks"
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with installation_operation_lock(
            mode=held,
            timeout_seconds=0.2,
            operation="test-holder",
            directory=directory,
        ):
            entered.set()
            assert release.wait(timeout=2)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert entered.wait(timeout=1)
    started = time.monotonic()
    try:
        with (
            pytest.raises(InstallationError, match="operation|lock|active|timed out"),
            installation_operation_lock(
                mode=requested,
                timeout_seconds=0.03,
                operation="test-contender",
                directory=directory,
            ),
        ):
            pytest.fail("a conflicting operation lock was acquired")
    finally:
        release.set()
        thread.join(timeout=2)
    elapsed = time.monotonic() - started
    assert not thread.is_alive()
    assert elapsed < 1


def test_compatibility_gate_accepts_only_clean_current_generation(tmp_path: Path) -> None:
    root = tmp_path / "ricky-data"
    initialize_installation(root)

    pointer, manifest = require_compatible_installation()

    assert pointer.user_data_dir == str(root)
    assert manifest.data_generation == 1
    assert manifest.migration_state == "clean"


@pytest.mark.parametrize(
    ("state", "generation"),
    [("failed", 1), ("clean", 2)],
)
def test_compatibility_gate_refuses_nonclean_or_future_data(
    tmp_path: Path, state: MigrationState, generation: int
) -> None:
    root = tmp_path / "ricky-data"
    initialized = initialize_installation(root)
    path = root / INSTALLATION_FILENAME
    manifest = _manifest(root).model_copy(
        update={
            "migration_state": state,
            "data_generation": generation,
            "operation_id": None if state == "clean" else "c" * 32,
        }
    )
    path.write_text(manifest.model_dump_json(), encoding="utf-8")

    with pytest.raises(InstallationError, match="upgrade|migration|generation|compatible"):
        require_compatible_installation()

    # The refusal is diagnostic only and cannot alter the identity or manifest.
    assert initialized.installation_id == _manifest(root).installation_id
    assert _manifest(root) == manifest


def test_manifest_transition_requires_live_exclusive_lock_authority(tmp_path: Path) -> None:
    root = tmp_path / "ricky-data"
    initialize_installation(root)
    current = _manifest(root)

    with (
        installation_operation_lock(
            mode="shared",
            timeout_seconds=0.1,
            operation="test_shared_transition",
        ) as shared,
        pytest.raises(InstallationError, match="exclusive operation lock"),
    ):
        transition_installation_manifest(
            root,
            current,
            lock=shared,
            migration_state="prepared",
            operation_id="d" * 32,
            lifecycle_version="0.6.0",
        )

    with installation_operation_lock(
        mode="exclusive",
        timeout_seconds=0.1,
        operation="test_closed_transition",
    ) as released:
        pass
    with pytest.raises(InstallationError, match="no longer active"):
        transition_installation_manifest(
            root,
            current,
            lock=released,
            migration_state="prepared",
            operation_id="d" * 32,
            lifecycle_version="0.6.0",
        )
    assert _manifest(root) == current


def test_manifest_transition_allows_forward_and_rollback_clean_generation_changes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ricky-data"
    initialize_installation(root)
    forward_id = "d" * 32

    with installation_operation_lock(
        mode="exclusive",
        timeout_seconds=0.1,
        operation="test_forward_transition",
        operation_id=forward_id,
    ) as lock:
        clean_one = _manifest(root)
        prepared = transition_installation_manifest(
            root,
            clean_one,
            lock=lock,
            migration_state="prepared",
            operation_id=forward_id,
            lifecycle_version="0.6.0",
        )
        assert prepared.data_generation == 1
        replaced = transition_installation_manifest(
            root,
            prepared,
            lock=lock,
            migration_state="software_replaced",
            operation_id=forward_id,
            lifecycle_version="0.6.1",
        )
        migrating = transition_installation_manifest(
            root,
            replaced,
            lock=lock,
            migration_state="migrating",
            operation_id=forward_id,
            lifecycle_version="0.6.1",
        )
        clean_two = transition_installation_manifest(
            root,
            migrating,
            lock=lock,
            migration_state="clean",
            operation_id=None,
            data_generation=2,
            lifecycle_version="0.6.1",
        )

    assert clean_two.migration_state == "clean"
    assert clean_two.operation_id is None
    assert clean_two.data_generation == 2
    assert clean_two.last_lifecycle_version == "0.6.1"

    rollback_id = "e" * 32
    with installation_operation_lock(
        mode="exclusive",
        timeout_seconds=0.1,
        operation="test_rollback_transition",
        operation_id=rollback_id,
    ) as lock:
        prepared_rollback = transition_installation_manifest(
            root,
            clean_two,
            lock=lock,
            migration_state="prepared",
            operation_id=rollback_id,
            lifecycle_version="0.6.1",
        )
        rolling_back = transition_installation_manifest(
            root,
            prepared_rollback,
            lock=lock,
            migration_state="rolling_back",
            operation_id=rollback_id,
            lifecycle_version="0.6.1",
        )
        restored = transition_installation_manifest(
            root,
            rolling_back,
            lock=lock,
            migration_state="clean",
            operation_id=None,
            data_generation=1,
            lifecycle_version="0.6.0",
        )

    assert restored.migration_state == "clean"
    assert restored.operation_id is None
    assert restored.data_generation == 1
    assert _manifest(root) == restored


def test_manifest_transition_rejects_invalid_edges_operation_swap_and_early_generation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ricky-data"
    initialize_installation(root)
    operation_id = "d" * 32

    with installation_operation_lock(
        mode="exclusive",
        timeout_seconds=0.1,
        operation="test_invalid_transition",
        operation_id=operation_id,
    ) as lock:
        clean = _manifest(root)
        with pytest.raises(InstallationError, match="clean -> migrating"):
            transition_installation_manifest(
                root,
                clean,
                lock=lock,
                migration_state="migrating",
                operation_id=operation_id,
                lifecycle_version="0.6.0",
            )
        prepared = transition_installation_manifest(
            root,
            clean,
            lock=lock,
            migration_state="prepared",
            operation_id=operation_id,
            lifecycle_version="0.6.0",
        )
        with pytest.raises(InstallationError, match="change its operation_id"):
            transition_installation_manifest(
                root,
                prepared,
                lock=lock,
                migration_state="software_replaced",
                operation_id="e" * 32,
                lifecycle_version="0.6.1",
            )
        with pytest.raises(InstallationError, match="generation can change"):
            transition_installation_manifest(
                root,
                prepared,
                lock=lock,
                migration_state="software_replaced",
                operation_id=operation_id,
                data_generation=2,
                lifecycle_version="0.6.1",
            )
        with pytest.raises(InstallationError, match="prepared -> clean"):
            transition_installation_manifest(
                root,
                prepared,
                lock=lock,
                migration_state="clean",
                operation_id=None,
                lifecycle_version="0.6.1",
            )
        with pytest.raises(InstallationError, match="prepared -> failed"):
            transition_installation_manifest(
                root,
                prepared,
                lock=lock,
                migration_state="failed",
                operation_id=operation_id,
                lifecycle_version="0.6.0",
            )

    assert _manifest(root) == prepared


@pytest.mark.parametrize("lifecycle_version", ["0.0.0", "0.6", "0.6.0rc1"])
def test_manifest_transition_refuses_fallback_or_nonrelease_lifecycle_version(
    tmp_path: Path, lifecycle_version: str
) -> None:
    root = tmp_path / "ricky-data"
    initialize_installation(root)
    current = _manifest(root)

    with (
        installation_operation_lock(
            mode="exclusive",
            timeout_seconds=0.1,
            operation="test_version_transition",
        ) as lock,
        pytest.raises(InstallationError, match="installed Ricky release"),
    ):
        transition_installation_manifest(
            root,
            current,
            lock=lock,
            migration_state="prepared",
            operation_id="d" * 32,
            lifecycle_version=lifecycle_version,
        )
    assert _manifest(root) == current


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_operation_lock_rejects_nonfinite_timeout_without_writing(
    tmp_path: Path, timeout: float
) -> None:
    directory = tmp_path / "locks"

    with (
        pytest.raises(InstallationError, match="finite and non-negative"),
        installation_operation_lock(
            directory=directory,
            mode="exclusive",
            timeout_seconds=timeout,
            operation="test_invalid_timeout",
        ),
    ):
        pytest.fail("a non-finite lock timeout was accepted")
    assert not directory.exists()


def test_operation_lock_rejects_invalid_mode_without_writing(tmp_path: Path) -> None:
    directory = tmp_path / "locks"

    with (
        pytest.raises(InstallationError, match="shared or exclusive"),
        installation_operation_lock(
            directory=directory,
            mode=cast(Any, "write"),
            timeout_seconds=0,
            operation="test_invalid_mode",
        ),
    ):
        pytest.fail("an invalid lock mode was accepted")
    assert not directory.exists()


def test_stale_operation_metadata_is_not_reported_as_a_live_owner(tmp_path: Path) -> None:
    fcntl = pytest.importorskip("fcntl")
    directory = tmp_path / "locks"
    directory.mkdir()
    lock_path = directory / ".init.lock"
    info_path = directory / ".operation.json"
    stale_pid = 2_147_483_647
    info_path.write_text(
        json.dumps(
            {
                "operation": "stale_upgrade",
                "operation_id": "f" * 32,
                "pid": stale_pid,
                "started_at": "2026-09-02T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with (
            pytest.raises(InstallationError) as refused,
            installation_operation_lock(
                directory=directory,
                mode="exclusive",
                timeout_seconds=0,
                operation="test_contender",
            ),
        ):
            pytest.fail("a contended lock was acquired")
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    message = str(refused.value)
    assert "another Ricky process is active" in message
    assert "stale_upgrade" not in message
    assert str(stale_pid) not in message
