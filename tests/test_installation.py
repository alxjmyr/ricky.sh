"""Installation bootstrap, scaffold, and destructive-lifecycle tests."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tomllib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

import ricky.installation as installation
from ricky import __version__
from ricky.config import RickySettings, load_settings
from ricky.gateway.lock import GatewayLock
from ricky.gateway.service_unit import MARKER, GatewayServiceUnit
from ricky.installation import (
    BOOTSTRAP_FORMAT_VERSION,
    INSTALLATION_FORMAT_VERSION,
    BootstrapPointer,
    InstallationError,
    InstallationManifest,
    bootstrap_config_dir,
    bootstrap_file,
    initialize_installation,
    purge_installation_data,
    read_bootstrap_pointer,
    require_installation,
    resolve_bootstrap_user_data_dir,
)
from ricky.schedules.cron import BEGIN_MARKER, END_MARKER, CommandResult, UserCrontabBackend

MANAGED_BLOCK = f"{BEGIN_MARKER}\n{END_MARKER}\n"


class FakeCrontabRunner:
    """Answer ``crontab -l`` in memory so no test depends on a host crontab."""

    def __init__(self, current: str | None = None, *, available: bool = True) -> None:
        self.current = current
        self.available = available
        self.commands: list[tuple[str, ...]] = []

    async def run(self, args: Sequence[str]) -> CommandResult:
        self.commands.append(tuple(str(item) for item in args))
        if not self.available:
            raise FileNotFoundError("crontab")
        if self.current is None:
            return CommandResult(returncode=1, stderr="no crontab for test-user")
        return CommandResult(returncode=0, stdout=self.current)


def _crontab(
    root: Path, current: str | None = None, *, available: bool = True
) -> UserCrontabBackend:
    return UserCrontabBackend(
        RickySettings(user_data_dir=str(root)),
        runner=FakeCrontabRunner(current, available=available),
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _manifest(root: Path) -> InstallationManifest:
    return InstallationManifest.model_validate_json(
        (root / installation.INSTALLATION_FILENAME).read_text(encoding="utf-8")
    )


def test_bootstrap_path_uses_isolated_xdg_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "custom-xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    assert bootstrap_config_dir() == xdg / "ricky"
    assert bootstrap_file() == xdg / "ricky" / "bootstrap.toml"
    assert not bootstrap_config_dir().exists()


def test_bootstrap_resolution_precedence_and_one_shot_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RICKY_USER_DATA_DIR", raising=False)
    assert resolve_bootstrap_user_data_dir() == Path.home() / ".ricky"

    from_env = tmp_path / "from-env"
    monkeypatch.setenv("RICKY_USER_DATA_DIR", str(from_env))
    assert resolve_bootstrap_user_data_dir() == from_env

    explicit = tmp_path / "explicit"
    result = initialize_installation(explicit)
    assert result.user_data_dir == str(explicit)
    assert resolve_bootstrap_user_data_dir(explicit) == explicit

    with pytest.raises(InstallationError, match="conflicts"):
        resolve_bootstrap_user_data_dir(from_env)
    with pytest.raises(InstallationError, match="conflicts"):
        resolve_bootstrap_user_data_dir()


def test_initialize_creates_only_private_minimal_shared_scaffold(
    tmp_path: Path,
) -> None:
    root = tmp_path / "nested" / "ricky-data"
    now = datetime(2026, 9, 1, 12, 30, tzinfo=UTC)

    result = initialize_installation(root, now=now)

    assert result.created is True
    assert result.pointer_created is True
    assert Path(result.user_data_dir) == root
    assert Path(result.bootstrap_path) == bootstrap_file()
    assert set(path.name for path in root.iterdir()) == {
        "installation.json",
        "profiles",
        "ricky.toml",
    }
    assert list((root / "profiles").iterdir()) == [root / "profiles" / "shared"]
    assert list((root / "profiles" / "shared").iterdir()) == []

    config = tomllib.loads((root / "ricky.toml").read_text(encoding="utf-8"))
    assert config == {
        "profiles": {"enabled": ["shared"], "default": "shared"},
    }
    manifest = _manifest(root)
    assert manifest.installation_id == result.installation_id
    assert manifest.format_version == INSTALLATION_FORMAT_VERSION
    assert manifest.created_at == now
    assert manifest.created_by_version == manifest.last_lifecycle_version == __version__
    assert manifest.migration_state == "clean"
    assert InstallationManifest.model_validate_json(manifest.model_dump_json()) == manifest

    pointer = read_bootstrap_pointer()
    assert pointer == BootstrapPointer(
        format_version=BOOTSTRAP_FORMAT_VERSION,
        user_data_dir=str(root),
        installation_id=manifest.installation_id,
    )
    assert _mode(root) == 0o700
    assert _mode(root / "profiles") == 0o700
    assert _mode(root / "profiles" / "shared") == 0o700
    assert _mode(root / "ricky.toml") == 0o600
    assert _mode(root / "installation.json") == 0o600
    assert _mode(bootstrap_config_dir()) == 0o700
    assert _mode(bootstrap_file()) == 0o600


def test_initialized_pointer_drives_normal_configuration_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "custom-root"
    initialize_installation(root)
    monkeypatch.delenv("RICKY_USER_DATA_DIR", raising=False)

    settings = load_settings()

    assert Path(settings.user_data_dir) == root
    assert settings.profiles.enabled == ["shared"]
    assert settings.profiles.default == "shared"
    assert settings.resolve_profile_scope().primary == "shared"


def test_initialize_existing_empty_directory_and_tighten_permissions(tmp_path: Path) -> None:
    root = tmp_path / "existing-empty"
    root.mkdir(mode=0o755)

    result = initialize_installation(root)

    assert result.created is True
    assert _mode(root) == 0o700
    assert list(tmp_path.glob(f".{root.name}-empty-*")) == []


def test_repeated_initialize_is_idempotent_and_preserves_authored_state(tmp_path: Path) -> None:
    root = tmp_path / "ricky-data"
    first = initialize_installation(root)
    authored = root / "profiles" / "shared" / "SOUL.md"
    authored.write_text("Keep this.\n", encoding="utf-8")
    before = {
        path: path.read_bytes()
        for path in (root / "installation.json", root / "ricky.toml", bootstrap_file(), authored)
    }

    second = initialize_installation(root)

    assert second.created is False
    assert second.pointer_created is False
    assert second.installation_id == first.installation_id
    assert {path: path.read_bytes() for path in before} == before


def test_verifying_an_installation_does_not_rewrite_the_lifecycle_version(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ricky-data"
    initialize_installation(root)
    manifest_path = root / installation.INSTALLATION_FILENAME
    aged = _manifest(root).model_copy(update={"last_lifecycle_version": "0.0.1"})
    manifest_path.write_text(aged.model_dump_json(), encoding="utf-8")

    result = initialize_installation(root)

    # Only an operation that rewrites the manifest under the exclusive lock
    # advances this field, so verifying an installation leaves it alone.
    assert result.created is False
    assert _manifest(root).last_lifecycle_version == "0.0.1"


def test_valid_installation_can_rebind_a_missing_pointer(tmp_path: Path) -> None:
    root = tmp_path / "ricky-data"
    first = initialize_installation(root)
    bootstrap_file().unlink()

    rebound = initialize_installation(root)

    assert rebound.created is False
    assert rebound.pointer_created is True
    assert rebound.installation_id == first.installation_id
    pointer, manifest = require_installation()
    assert pointer.installation_id == manifest.installation_id == first.installation_id


def test_pointer_is_written_only_after_scaffold_creation_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ricky-data"

    def fail_scaffold(_root: Path, _manifest: InstallationManifest) -> None:
        raise OSError("injected scaffold failure")

    monkeypatch.setattr(installation, "_create_scaffold", fail_scaffold)
    with pytest.raises(OSError, match="injected scaffold failure"):
        initialize_installation(root)

    assert not root.exists()
    assert not bootstrap_file().exists()


def test_completed_scaffold_is_recoverable_when_pointer_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ricky-data"
    real_write_pointer = installation._write_pointer

    def fail_pointer(_path: Path, _pointer: BootstrapPointer) -> None:
        raise OSError("injected pointer failure")

    monkeypatch.setattr(installation, "_write_pointer", fail_pointer)
    with pytest.raises(OSError, match="injected pointer failure"):
        initialize_installation(root)
    manifest = _manifest(root)
    assert not bootstrap_file().exists()

    monkeypatch.setattr(installation, "_write_pointer", real_write_pointer)
    recovered = initialize_installation(root)
    assert recovered.created is False
    assert recovered.pointer_created is True
    assert recovered.installation_id == manifest.installation_id


def test_reinitializing_after_data_loss_rebinds_the_existing_pointer(tmp_path: Path) -> None:
    root = tmp_path / "ricky-data"
    first = initialize_installation(root)
    shutil.rmtree(root)

    recovered = initialize_installation(root)

    assert recovered.created is True
    assert recovered.pointer_created is False
    assert recovered.installation_id == first.installation_id
    pointer, manifest = require_installation()
    assert pointer.installation_id == manifest.installation_id == first.installation_id


def test_unknown_nonempty_target_is_refused_without_modification(tmp_path: Path) -> None:
    root = tmp_path / "unknown"
    root.mkdir()
    marker = root / "keep.txt"
    marker.write_text("unchanged", encoding="utf-8")

    with pytest.raises(InstallationError, match="non-empty directory"):
        initialize_installation(root)

    assert marker.read_text(encoding="utf-8") == "unchanged"
    assert list(root.iterdir()) == [marker]
    assert not bootstrap_file().exists()


@pytest.mark.parametrize("missing", ["ricky.toml", "profiles/shared"])
def test_partial_installation_is_refused_with_exact_missing_component(
    tmp_path: Path, missing: str
) -> None:
    root = tmp_path / "partial"
    root.mkdir()
    manifest = InstallationManifest(
        installation_id="a" * 32,
        created_by_version="test",
        last_lifecycle_version="test",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    (root / "installation.json").write_text(manifest.model_dump_json(), encoding="utf-8")
    if missing != "ricky.toml":
        (root / "ricky.toml").write_text('user_data_dir = "unused"\n', encoding="utf-8")
    if missing != "profiles/shared":
        (root / "profiles" / "shared").mkdir(parents=True)

    with pytest.raises(InstallationError, match=missing):
        initialize_installation(root)

    assert not bootstrap_file().exists()


def test_invalid_or_future_manifest_is_refused(tmp_path: Path) -> None:
    for name, document in (
        ("malformed", "{not-json"),
        (
            "future",
            json.dumps(
                {
                    "format_version": INSTALLATION_FORMAT_VERSION + 1,
                    "installation_id": "a" * 32,
                    "created_by_version": "future",
                    "last_lifecycle_version": "future",
                    "created_at": "2026-09-01T00:00:00Z",
                    "migration_state": "clean",
                }
            ),
        ),
    ):
        root = tmp_path / name
        (root / "profiles" / "shared").mkdir(parents=True)
        (root / "ricky.toml").write_text('user_data_dir = "unused"\n', encoding="utf-8")
        (root / "installation.json").write_text(document, encoding="utf-8")

        with pytest.raises(InstallationError, match="invalid installation manifest"):
            initialize_installation(root)

    assert not bootstrap_file().exists()


def test_malformed_pointer_and_pointer_manifest_identity_mismatch_are_refused(
    tmp_path: Path,
) -> None:
    bootstrap_config_dir().mkdir(parents=True)
    bootstrap_file().write_text("not = [valid", encoding="utf-8")
    with pytest.raises(InstallationError, match="invalid Ricky bootstrap pointer"):
        read_bootstrap_pointer()

    bootstrap_file().unlink()
    root = tmp_path / "ricky-data"
    initialized = initialize_installation(root)
    pointer_text = bootstrap_file().read_text(encoding="utf-8")
    bootstrap_file().write_text(
        pointer_text.replace(initialized.installation_id, "f" * 32), encoding="utf-8"
    )

    with pytest.raises(InstallationError, match="installation_id"):
        initialize_installation(root)
    with pytest.raises(InstallationError, match="does not match"):
        require_installation()


@pytest.mark.parametrize("target_kind", ["filesystem", "home", "relative", "file"])
def test_initialize_rejects_broad_or_invalid_targets(tmp_path: Path, target_kind: str) -> None:
    if target_kind == "filesystem":
        target: str | Path = Path("/")
    elif target_kind == "home":
        target = Path.home()
    elif target_kind == "relative":
        target = Path("relative-ricky-data")
    else:
        target = tmp_path / "regular-file"
        target.write_text("keep", encoding="utf-8")

    with pytest.raises(InstallationError):
        initialize_installation(target)

    assert not bootstrap_file().exists()


def test_initialize_rejects_symlink_in_target_path_without_touching_destination(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(InstallationError, match="symbolic link"):
        initialize_installation(linked_parent / "ricky-data")

    assert list(outside.iterdir()) == []
    assert not bootstrap_file().exists()


def test_initialize_rejects_a_dangling_symlink_target(tmp_path: Path) -> None:
    hidden = tmp_path / "elsewhere"
    linked_root = tmp_path / "ricky-data"
    linked_root.symlink_to(hidden, target_is_directory=True)

    with pytest.raises(InstallationError, match="symbolic link"):
        initialize_installation(linked_root)

    assert not hidden.exists()
    assert not bootstrap_file().exists()


@pytest.mark.parametrize("target_kind", ["nonempty", "symlink", "dangling", "home", "file"])
def test_refused_target_creates_no_bootstrap_directory(tmp_path: Path, target_kind: str) -> None:
    target: str | Path
    if target_kind == "nonempty":
        unknown = tmp_path / "unknown"
        unknown.mkdir()
        (unknown / "keep.txt").write_text("unchanged", encoding="utf-8")
        target = unknown
    elif target_kind == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        linked = tmp_path / "linked"
        linked.symlink_to(outside, target_is_directory=True)
        target = linked / "ricky-data"
    elif target_kind == "dangling":
        dangling = tmp_path / "ricky-data"
        dangling.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
        target = dangling
    elif target_kind == "file":
        regular = tmp_path / "regular-file"
        regular.write_text("keep", encoding="utf-8")
        target = regular
    else:
        target = Path.home()

    with pytest.raises(InstallationError):
        initialize_installation(target)

    assert not bootstrap_config_dir().exists()


def test_bootstrap_directory_rejects_a_dangling_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "dangling-xdg"
    xdg.mkdir()
    (xdg / "ricky").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    with pytest.raises(InstallationError, match="symbolic link"):
        bootstrap_config_dir()


def test_committed_scaffold_leaves_no_staging_directory_when_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ricky-data"
    root.mkdir()
    real_fsync = installation.fsync_directory

    def fail_parent_fsync(path: Path) -> None:
        if path == root.parent:
            raise OSError("injected fsync failure")
        real_fsync(path)

    monkeypatch.setattr(installation, "fsync_directory", fail_parent_fsync)
    with pytest.raises(OSError, match="injected fsync failure"):
        initialize_installation(root)

    assert (root / installation.INSTALLATION_FILENAME).is_file()
    assert list(tmp_path.glob(f".{root.name}-empty-*")) == []
    assert list(tmp_path.glob(f".{root.name}-init-*")) == []


def test_initialization_lock_refuses_a_concurrent_operation() -> None:
    directory = bootstrap_config_dir()

    with (
        installation._initialization_lock(directory),
        pytest.raises(InstallationError, match="could not start installation within 0s"),
        installation._initialization_lock(directory),
    ):
        pytest.fail("the second operation must not acquire the lock")


async def test_purge_requires_exact_identity_and_removes_pointer_last(tmp_path: Path) -> None:
    root = tmp_path / "ricky-data"
    initialized = initialize_installation(root)

    with pytest.raises(InstallationError, match="confirmation does not match"):
        await purge_installation_data(expected_installation_id="0" * 32, crontab=_crontab(root))
    assert root.is_dir()
    assert bootstrap_file().is_file()

    purged = await purge_installation_data(
        expected_installation_id=initialized.installation_id, crontab=_crontab(root)
    )

    assert purged.user_data_dir == str(root)
    assert purged.installation_id == initialized.installation_id
    assert not root.exists()
    assert not bootstrap_file().exists()


async def test_purge_never_restores_a_partially_removed_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ricky-data"
    initialized = initialize_installation(root)
    tombstone = root.with_name(f".{root.name}-purge-{initialized.installation_id[:8]}")

    real_rmtree = installation.shutil.rmtree

    def partial_delete(path: Path) -> None:
        real_rmtree(Path(path) / "profiles")
        raise OSError("injected delete failure")

    monkeypatch.setattr(installation.shutil, "rmtree", partial_delete)
    with pytest.raises(InstallationError, match="requires manual review"):
        await purge_installation_data(
            expected_installation_id=initialized.installation_id, crontab=_crontab(root)
        )

    assert not root.exists()
    assert (tombstone / installation.INSTALLATION_FILENAME).is_file()
    assert read_bootstrap_pointer() is not None
    with pytest.raises(InstallationError):
        require_installation()


async def test_purge_refuses_stale_staging_path(tmp_path: Path) -> None:
    root = tmp_path / "ricky-data"
    initialized = initialize_installation(root)
    tombstone = root.with_name(f".{root.name}-purge-{initialized.installation_id[:8]}")
    tombstone.mkdir()

    with pytest.raises(InstallationError, match="stale purge staging path"):
        await purge_installation_data(
            expected_installation_id=initialized.installation_id, crontab=_crontab(root)
        )

    assert root.is_dir()
    assert tombstone.is_dir()
    assert bootstrap_file().is_file()


async def test_purge_refuses_an_active_gateway_owner(tmp_path: Path) -> None:
    root = tmp_path / "ricky-data"
    initialized = initialize_installation(root)
    lock = GatewayLock(RickySettings(user_data_dir=str(root)))
    lock.acquire()
    try:
        with pytest.raises(InstallationError, match="gateway.*active|active.*gateway"):
            await purge_installation_data(
                expected_installation_id=initialized.installation_id, crontab=_crontab(root)
            )
    finally:
        lock.release()

    assert root.is_dir()
    assert bootstrap_file().is_file()


async def test_purge_refuses_an_installed_ricky_owned_gateway_service(tmp_path: Path) -> None:
    root = tmp_path / "ricky-data"
    initialized = initialize_installation(root)
    unit_path = GatewayServiceUnit(RickySettings(user_data_dir=str(root))).unit_path
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(f"{MARKER}\n[Service]\nExecStart=/bin/ricky\n", encoding="utf-8")

    # The guard belongs to the API itself: this call never goes through the CLI.
    with pytest.raises(InstallationError, match="gateway service is still installed"):
        await purge_installation_data(
            expected_installation_id=initialized.installation_id, crontab=_crontab(root)
        )
    assert (root / installation.INSTALLATION_FILENAME).is_file()
    assert bootstrap_file().is_file()

    # A unit Ricky does not own is not Ricky's launcher and never blocks purge.
    unit_path.write_text("[Service]\nExecStart=/bin/foreign\n", encoding="utf-8")
    purged = await purge_installation_data(
        expected_installation_id=initialized.installation_id, crontab=_crontab(root)
    )

    assert purged.installation_id == initialized.installation_id
    assert not root.exists()
    assert not bootstrap_file().exists()
    assert unit_path.is_file()


@pytest.mark.parametrize("cron_state_dir", [True, False])
async def test_purge_refuses_an_installed_managed_crontab_block(
    tmp_path: Path, cron_state_dir: bool
) -> None:
    root = tmp_path / "ricky-data"
    initialized = initialize_installation(root)
    # Ricky's cron state directory is not the signal. A restored backup or a
    # hand-deleted directory must not turn an installed block into an absence.
    if cron_state_dir:
        (root / "cron").mkdir()
    assert (root / "cron").exists() is cron_state_dir

    with pytest.raises(InstallationError, match="schedules are still installed"):
        await purge_installation_data(
            expected_installation_id=initialized.installation_id,
            crontab=_crontab(root, MANAGED_BLOCK),
        )

    assert (root / installation.INSTALLATION_FILENAME).is_file()
    assert bootstrap_file().is_file()


@pytest.mark.parametrize("crontab_state", ["unavailable", "no-managed-block"])
async def test_purge_proceeds_when_the_crontab_holds_no_managed_block(
    tmp_path: Path, crontab_state: str
) -> None:
    root = tmp_path / "ricky-data"
    initialized = initialize_installation(root)
    (root / "cron").mkdir()
    available = crontab_state != "unavailable"
    runner = FakeCrontabRunner(
        None if not available else "*/5 * * * * /usr/bin/unrelated\n", available=available
    )
    backend = UserCrontabBackend(RickySettings(user_data_dir=str(root)), runner=runner)

    purged = await purge_installation_data(
        expected_installation_id=initialized.installation_id, crontab=backend
    )

    # An unusable crontab could never have installed the block, so availability
    # is decided by attempting the read rather than by any local state.
    assert runner.commands == [("crontab", "-l")]
    assert purged.user_data_dir == str(root)
    assert not root.exists()
    assert not bootstrap_file().exists()


async def test_purge_rejects_a_root_replaced_by_a_symlink(tmp_path: Path) -> None:
    root = tmp_path / "ricky-data"
    initialized = initialize_installation(root)
    moved = tmp_path / "moved-real-data"
    os.replace(root, moved)
    root.symlink_to(moved, target_is_directory=True)

    with pytest.raises(InstallationError, match="link|bootstrap pointer"):
        await purge_installation_data(
            expected_installation_id=initialized.installation_id, crontab=_crontab(root)
        )

    assert moved.is_dir()
    assert (moved / "installation.json").is_file()
    assert bootstrap_file().is_file()


@pytest.mark.parametrize(
    ("relative_path", "target_is_directory"),
    [
        (Path("installation.json"), False),
        (Path("ricky.toml"), False),
        (Path("profiles"), True),
        (Path("profiles/shared"), True),
    ],
)
def test_require_installation_rejects_symlinked_scaffold_paths(
    tmp_path: Path,
    relative_path: Path,
    target_is_directory: bool,
) -> None:
    root = tmp_path / "ricky-data"
    initialize_installation(root)
    selected = root / relative_path
    outside = tmp_path / f"outside-{relative_path.as_posix().replace('/', '-')}"

    if target_is_directory:
        os.replace(selected, outside)
    else:
        outside.write_text(selected.read_text(encoding="utf-8"), encoding="utf-8")
        selected.unlink()
    selected.symlink_to(outside, target_is_directory=target_is_directory)

    with pytest.raises(InstallationError, match="symbolic link"):
        require_installation()

    assert outside.exists()
    assert bootstrap_file().is_file()
