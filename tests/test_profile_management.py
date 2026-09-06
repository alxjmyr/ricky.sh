"""Profile scaffold and deletion lifecycle coverage."""

from __future__ import annotations

import json
import shutil
import stat
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import tomlkit
from typer.testing import CliRunner

import ricky.profiles.management as profile_management
from ricky.config import load_settings
from ricky.installation import (
    InstallationError,
    bootstrap_file,
    initialize_installation,
    write_private_file,
)
from ricky.interfaces.cli.app import app
from ricky.profiles import ProfileScope
from ricky.profiles.management import (
    ProfileManagementError,
    _configured_profile_references,
    add_profile,
    delete_profile,
    set_default_profile,
)
from ricky.schedules.store import ScheduleStore
from ricky.schedules.types import ScheduleSpec
from ricky.upgrades.non_sql import confined_profile_roots

runner = CliRunner()


def _initialize(tmp_path: Path) -> Path:
    root = tmp_path / "user-data"
    initialize_installation(root)
    return root


def test_profile_add_without_installation_does_not_create_lifecycle_state() -> None:
    bootstrap_root = bootstrap_file().parent

    with pytest.raises(InstallationError, match="not initialized"):
        add_profile("personal")

    assert not bootstrap_root.exists()


def _update_root_config(root: Path, update: Callable[[Any], None]) -> None:
    path = root / "ricky.toml"
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    update(document)
    write_private_file(path, tomlkit.dumps(document))


def test_add_profile_creates_only_minimal_private_scaffold(tmp_path: Path) -> None:
    root = _initialize(tmp_path)

    result = add_profile("personal")

    profile_root = root / "profiles" / "personal"
    assert result.profile == "personal"
    assert result.profile_dir == str(profile_root)
    assert result.config_path == str(profile_root / "ricky.toml")
    assert result.default_profile == "shared"
    assert [path.name for path in profile_root.iterdir()] == ["ricky.toml"]
    assert tomllib.loads((profile_root / "ricky.toml").read_text(encoding="utf-8")) == {
        "profile": {}
    }
    assert not (profile_root / ".secrets.toml").exists()
    assert stat.S_IMODE(profile_root.stat().st_mode) == 0o700
    assert stat.S_IMODE((profile_root / "ricky.toml").stat().st_mode) == 0o600

    root_config = tomllib.loads((root / "ricky.toml").read_text(encoding="utf-8"))
    assert root_config["profiles"]["enabled"] == ["shared", "personal"]
    assert root_config["profiles"]["default"] == "shared"
    assert root_config["profiles"]["definitions"]["personal"] == {}
    assert load_settings().profiles.enabled == ["shared", "personal"]


@pytest.mark.parametrize("name", ["shared", "bundled", "Work", "../work"])
def test_add_profile_rejects_reserved_or_invalid_names_without_changes(
    tmp_path: Path,
    name: str,
) -> None:
    root = _initialize(tmp_path)
    before = (root / "ricky.toml").read_bytes()

    with pytest.raises((ProfileManagementError, ValueError)):
        add_profile(name)

    assert (root / "ricky.toml").read_bytes() == before
    assert [path.name for path in (root / "profiles").iterdir()] == ["shared"]


def test_add_profile_refuses_registered_and_unregistered_existing_paths(tmp_path: Path) -> None:
    root = _initialize(tmp_path)
    add_profile("personal")

    with pytest.raises(ProfileManagementError, match="already exists"):
        add_profile("personal")

    stray = root / "profiles" / "work"
    stray.mkdir()
    marker = stray / "keep"
    marker.write_text("unchanged", encoding="utf-8")
    with pytest.raises(ProfileManagementError, match="unregistered profile path"):
        add_profile("work")
    assert marker.read_text(encoding="utf-8") == "unchanged"


def test_add_profile_rolls_back_scaffold_when_registry_commit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _initialize(tmp_path)
    before = (root / "ricky.toml").read_bytes()
    real_write = write_private_file
    root_attempts = 0

    def fail_root_config(path: Path, content: str) -> None:
        nonlocal root_attempts
        if path == root / "ricky.toml":
            root_attempts += 1
            if root_attempts == 1:
                raise OSError("simulated registry failure")
        real_write(path, content)

    monkeypatch.setattr(profile_management, "write_private_file", fail_root_config)

    with pytest.raises(OSError, match="simulated registry failure"):
        add_profile("personal")

    assert (root / "ricky.toml").read_bytes() == before
    assert not (root / "profiles" / "personal").exists()
    assert [path.name for path in (root / "profiles").iterdir()] == ["shared"]
    assert not list(root.glob(".profile-add-*"))


async def test_delete_profile_removes_owned_tree_and_registry_entry(tmp_path: Path) -> None:
    root = _initialize(tmp_path)
    add_profile("personal")
    marker = root / "profiles" / "personal" / "owned.txt"
    marker.write_text("profile data", encoding="utf-8")

    result = await delete_profile("personal")

    assert result.profile == "personal"
    assert result.profile_dir == str(root / "profiles" / "personal")
    assert result.default_profile == "shared"
    assert not marker.exists()
    assert not (root / "profiles" / "personal").exists()
    root_config = tomllib.loads((root / "ricky.toml").read_text(encoding="utf-8"))
    assert root_config["profiles"] == {"enabled": ["shared"], "default": "shared"}
    assert load_settings().profiles.enabled == ["shared"]


async def test_delete_profile_restores_tree_when_registry_commit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _initialize(tmp_path)
    add_profile("personal")
    before = (root / "ricky.toml").read_bytes()
    marker = root / "profiles" / "personal" / "owned.txt"
    marker.write_text("preserve", encoding="utf-8")
    real_write = write_private_file
    root_attempts = 0

    def fail_first_root_config(path: Path, content: str) -> None:
        nonlocal root_attempts
        if path == root / "ricky.toml":
            root_attempts += 1
            if root_attempts == 1:
                raise OSError("simulated registry failure")
        real_write(path, content)

    monkeypatch.setattr(profile_management, "write_private_file", fail_first_root_config)

    with pytest.raises(OSError, match="simulated registry failure"):
        await delete_profile("personal")

    assert (root / "ricky.toml").read_bytes() == before
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert not list(root.glob(".profile-delete-*"))


async def test_delete_profile_names_staged_data_it_cannot_reattach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _initialize(tmp_path)
    add_profile("personal")
    target = root / "profiles" / "personal"
    marker = target / "owned.txt"
    marker.write_text("preserve", encoding="utf-8")
    real_write = write_private_file
    root_attempts = 0

    def occupy_target_then_fail(path: Path, content: str) -> None:
        nonlocal root_attempts
        if path == root / "ricky.toml":
            root_attempts += 1
            if root_attempts == 1:
                target.mkdir()
                raise OSError("simulated registry failure")
        real_write(path, content)

    monkeypatch.setattr(profile_management, "write_private_file", occupy_target_then_fail)

    with pytest.raises(OSError, match="simulated registry failure") as failure:
        await delete_profile("personal")

    staged = list(root.glob(".profile-delete-*"))
    assert len(staged) == 1
    assert (staged[0] / "owned.txt").read_text(encoding="utf-8") == "preserve"
    assert any(str(staged[0]) in note for note in failure.value.__notes__)


async def test_delete_profile_refuses_a_replaced_profile_symlink(tmp_path: Path) -> None:
    root = _initialize(tmp_path)
    add_profile("personal")
    target = root / "profiles" / "personal"
    (target / "ricky.toml").unlink()
    target.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep"
    marker.write_text("unchanged", encoding="utf-8")
    target.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProfileManagementError, match="missing or invalid"):
        await delete_profile("personal")

    assert target.is_symlink()
    assert marker.read_text(encoding="utf-8") == "unchanged"


async def test_delete_default_requires_explicit_enabled_replacement(tmp_path: Path) -> None:
    root = _initialize(tmp_path)
    add_profile("personal")
    add_profile("work")

    def make_default(document: Any) -> None:
        assert isinstance(document, dict)
        document["profiles"]["default"] = "personal"

    _update_root_config(root, make_default)

    with pytest.raises(ProfileManagementError, match="--new-default"):
        await delete_profile("personal")
    with pytest.raises(ProfileManagementError, match="another enabled profile"):
        await delete_profile("personal", new_default="missing")

    result = await delete_profile("personal", new_default="work")
    assert result.default_profile == "work"
    assert load_settings().profiles.default == "work"
    assert (root / "profiles" / "work").is_dir()


async def test_delete_profile_reports_configuration_references_without_mutating(
    tmp_path: Path,
) -> None:
    root = _initialize(tmp_path)
    add_profile("work")

    def add_reference(document: Any) -> None:
        assert isinstance(document, dict)
        authority = tomlkit.table()
        capabilities = tomlkit.table()
        capability = tomlkit.table()
        capability["allowed_profiles"] = ["shared", "work"]
        capabilities["browser_commit"] = capability
        authority["capabilities"] = capabilities
        document["authority"] = authority

    _update_root_config(root, add_reference)
    before = (root / "ricky.toml").read_bytes()

    with pytest.raises(
        ProfileManagementError,
        match=r"authority\.capabilities\.browser_commit\.allowed_profiles",
    ):
        await delete_profile("work")

    assert (root / "ricky.toml").read_bytes() == before
    assert (root / "profiles" / "work").is_dir()


async def test_delete_profile_finds_every_current_operational_reference(tmp_path: Path) -> None:
    root = _initialize(tmp_path)
    add_profile("work")
    secrets = root / "profiles" / "work" / ".secrets.toml"
    write_private_file(
        secrets,
        '[messaging.telegram_accounts.bot]\nbot_token = "test-token"\n',
    )

    def add_references(document: Any) -> None:
        document.update(
            tomlkit.parse(
                """
[messaging.transports.inbox]
type = "telegram"
account = "work/bot"

[messaging.telegram_accounts."work/legacy"]
bot_token = "legacy-test-token"

[messaging.routes.inbox]
transport = "inbox"
destination = "123"
owner_profile = "work"
accepted_profiles = ["shared", "work"]

[gateway.routes.inbox]
provider = "claude_code"
model = "sonnet"
primary_profile = "work"
access_profiles = ["work"]

[authority.capabilities.browser_commit]
allowed_profiles = ["shared", "work"]
"""
            )
        )

    _update_root_config(root, add_references)
    settings = load_settings()

    assert _configured_profile_references(settings, "work") == (
        "authority.capabilities.browser_commit.allowed_profiles",
        "gateway.routes.inbox.access_profiles",
        "gateway.routes.inbox.primary_profile",
        "messaging.routes.inbox.accepted_profiles",
        "messaging.routes.inbox.owner_profile",
        "messaging.telegram_accounts.work/legacy",
        "messaging.transports.inbox.account",
    )
    with pytest.raises(ProfileManagementError, match="messaging.transports.inbox.account"):
        await delete_profile("work")


async def test_delete_profile_reports_profile_owned_authority_ceilings(tmp_path: Path) -> None:
    root = _initialize(tmp_path)
    add_profile("personal")
    add_profile("work")
    write_private_file(
        root / "profiles" / "personal" / "ricky.toml",
        "[profile]\n\n"
        "[authority.capabilities.browser_commit]\n"
        "enabled = true\n"
        'allowed_profiles = ["work"]\n',
    )

    with pytest.raises(
        ProfileManagementError,
        match=r"profiles\.personal\.authority\.capabilities\.browser_commit\.allowed_profiles",
    ):
        await delete_profile("work")

    assert (root / "profiles" / "work").is_dir()
    assert "work" in load_settings().profiles.enabled


async def test_delete_profile_keeps_undeletable_data_out_of_profile_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _initialize(tmp_path)
    add_profile("personal")

    def refuse(path: Path) -> None:
        raise OSError("device or resource busy")

    monkeypatch.setattr(profile_management.shutil, "rmtree", refuse)

    with pytest.raises(ProfileManagementError, match="staged data remains"):
        await delete_profile("personal")

    # ``profiles/`` must still contain only real profile roots, so upgrade
    # inventory and every other profile discovery keeps working.
    assert [path.name for path in (root / "profiles").iterdir()] == ["shared"]
    assert confined_profile_roots(root) == ((root / "profiles" / "shared").resolve(),)


async def test_delete_profile_reports_schedule_references_without_mutating(
    tmp_path: Path,
) -> None:
    root = _initialize(tmp_path)
    add_profile("work")
    settings = load_settings()
    scope = ProfileScope.create("work")
    now = datetime.now(UTC)
    schedule = ScheduleSpec(
        id="sched_" + "1" * 24,
        job_name="work/daily",
        cron="0 8 * * 1",
        project_root=str(tmp_path.resolve()),
        profile_scope=scope,
        approved_spec_digest="a" * 64,
        approved_runtime_policy_digest="b" * 64,
        created_at=now,
        updated_at=now,
    )
    await ScheduleStore(settings, scope=scope).create(schedule)

    with pytest.raises(ProfileManagementError, match=schedule.id):
        await delete_profile("work")

    assert (root / "profiles" / "work").is_dir()
    assert "work" in load_settings().profiles.enabled


async def test_delete_profile_reports_schedules_pinned_to_a_removed_profile(
    tmp_path: Path,
) -> None:
    root = _initialize(tmp_path)
    add_profile("personal")
    add_profile("work")
    settings = load_settings()
    scope = ProfileScope.create("work", access_profiles=["personal"])
    now = datetime.now(UTC)
    schedule = ScheduleSpec(
        id="sched_" + "2" * 24,
        job_name="work/daily",
        cron="0 8 * * 1",
        project_root=str(tmp_path.resolve()),
        profile_scope=scope,
        approved_spec_digest="a" * 64,
        approved_runtime_policy_digest="b" * 64,
        created_at=now,
        updated_at=now,
    )
    await ScheduleStore(settings, scope=scope).create(schedule)

    # Removing ``personal`` by hand puts the schedule outside every runtime
    # scope, which is exactly when a scope-filtered read goes blind.
    def disable_personal(document: Any) -> None:
        document["profiles"]["enabled"] = ["shared", "work"]
        del document["profiles"]["definitions"]["personal"]

    _update_root_config(root, disable_personal)
    shutil.rmtree(root / "profiles" / "personal")

    with pytest.raises(ProfileManagementError, match=schedule.id):
        await delete_profile("work")

    assert (root / "profiles" / "work").is_dir()
    assert "work" in load_settings().profiles.enabled


def test_profile_cli_add_and_delete_json(tmp_path: Path) -> None:
    root = _initialize(tmp_path)

    added = runner.invoke(app, ["profile", "add", "personal", "--json"])
    deleted = runner.invoke(app, ["profile", "delete", "personal", "--yes", "--json"])

    assert added.exit_code == deleted.exit_code == 0
    assert json.loads(added.stdout)["profile"] == "personal"
    assert json.loads(deleted.stdout)["profile"] == "personal"
    assert not (root / "profiles" / "personal").exists()


def test_profile_cli_delete_can_be_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _initialize(tmp_path)
    add_profile("personal")
    from ricky.interfaces.cli import profiles as profile_cli

    renderer = profile_cli.CliRenderer()
    renderer.input_session._interactive = True
    monkeypatch.setattr(profile_cli, "CliRenderer", lambda: renderer)

    result = runner.invoke(app, ["profile", "delete", "personal"], input="n\n")

    assert result.exit_code == 0
    assert "cancelled" in result.stdout.lower()
    assert (root / "profiles" / "personal").is_dir()


def test_profile_cli_delete_requires_yes_when_unattended(tmp_path: Path) -> None:
    root = _initialize(tmp_path)
    add_profile("personal")

    result = runner.invoke(app, ["profile", "delete", "personal"])

    assert result.exit_code == 1
    assert "unattended profile deletion requires --yes" in result.stdout
    assert (root / "profiles" / "personal").is_dir()


def test_profile_commands_are_discoverable_without_initializing() -> None:
    root_help = runner.invoke(app, ["--help"])
    profile_help = runner.invoke(app, ["profile", "--help"])

    assert root_help.exit_code == profile_help.exit_code == 0
    assert "profile" in root_help.stdout
    assert "add" in profile_help.stdout
    assert "delete" in profile_help.stdout
    assert "set-default" in profile_help.stdout


def test_set_default_preserves_configuration_and_profile_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _initialize(tmp_path)
    add_profile("personal")
    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.chdir(project_root)
    project_data = project_root / ".ricky"
    project_data.mkdir()
    marker = project_data / "keep"
    marker.write_text("project data", encoding="utf-8")
    config = root / "ricky.toml"
    config.write_text(config.read_text(encoding="utf-8") + "\n# Keep this comment.\n")
    original = tomllib.loads(config.read_text(encoding="utf-8"))
    owned = {path: path.read_bytes() for path in (root / "profiles").rglob("*") if path.is_file()}

    result = set_default_profile("personal")

    assert result.model_dump() == {
        "previous_default": "shared",
        "default_profile": "personal",
        "changed": True,
    }
    assert type(result).model_validate_json(result.model_dump_json()) == result
    expected = original
    expected["profiles"]["default"] = "personal"
    assert tomllib.loads(config.read_text(encoding="utf-8")) == expected
    assert "# Keep this comment." in config.read_text(encoding="utf-8")
    settings = load_settings()
    assert settings.profiles.default == "personal"
    assert settings.resolve_profile_scope().primary == "personal"
    assert settings.resolve_profile_scope(primary="shared").primary == "shared"
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert {path: path.read_bytes() for path in owned} == owned
    assert list(project_data.iterdir()) == [marker]
    assert marker.read_text(encoding="utf-8") == "project data"

    assert set_default_profile("shared").default_profile == "shared"
    assert load_settings().profiles.default == "shared"


def test_set_default_is_idempotent_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _initialize(tmp_path)
    before = (root / "ricky.toml").stat()

    def refuse_write(path: Path, content: str) -> None:
        pytest.fail("an unchanged default must not rewrite configuration")

    monkeypatch.setattr(profile_management, "write_private_file", refuse_write)
    result = set_default_profile("shared")
    assert not result.changed
    assert result.previous_default == result.default_profile == "shared"
    assert (root / "ricky.toml").stat() == before


@pytest.mark.parametrize("name", ["missing", "bundled", "Work", "../work"])
def test_set_default_rejects_unavailable_or_invalid_profile(tmp_path: Path, name: str) -> None:
    root = _initialize(tmp_path)
    before = (root / "ricky.toml").read_bytes()
    with pytest.raises((ProfileManagementError, ValueError)):
        set_default_profile(name)
    assert (root / "ricky.toml").read_bytes() == before
    assert [path.name for path in (root / "profiles").iterdir()] == ["shared"]


@pytest.mark.parametrize("symlink", [False, True])
def test_set_default_refuses_missing_or_symlinked_profile_root(
    tmp_path: Path, symlink: bool
) -> None:
    root = _initialize(tmp_path)
    add_profile("personal")
    target = root / "profiles" / "personal"
    shutil.rmtree(target)
    if symlink:
        target.symlink_to(root / "profiles" / "shared", target_is_directory=True)
    before = (root / "ricky.toml").read_bytes()
    with pytest.raises(ProfileManagementError, match="missing or invalid"):
        set_default_profile("personal")
    assert (root / "ricky.toml").read_bytes() == before


@pytest.mark.parametrize("interrupted", [False, True])
def test_set_default_restores_registry_when_post_commit_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupted: bool,
) -> None:
    root = _initialize(tmp_path)
    add_profile("personal")
    before = (root / "ricky.toml").read_bytes()
    failure = KeyboardInterrupt if interrupted else ValueError

    def fail_validation(path: Path) -> None:
        assert load_settings().profiles.default == "personal"
        raise failure("simulated validation interruption")

    monkeypatch.setattr(profile_management, "_validate_committed_settings", fail_validation)
    with pytest.raises(failure, match="simulated validation interruption"):
        set_default_profile("personal")
    assert (root / "ricky.toml").read_bytes() == before
    assert load_settings().profiles.default == "shared"


def test_set_default_without_installation_does_not_create_state() -> None:
    bootstrap_root = bootstrap_file().parent
    with pytest.raises(InstallationError, match="not initialized"):
        set_default_profile("shared")
    assert not bootstrap_root.exists()


def test_set_default_rechecks_installation_identity_under_exclusive_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _initialize(tmp_path)
    add_profile("personal")
    before = (root / "ricky.toml").read_bytes()
    original_check = profile_management.require_compatible_installation
    original_lock = profile_management.installation_operation_lock
    locked = False
    checks = 0

    @contextmanager
    def track_lock(**kwargs: Any) -> Iterator[None]:
        nonlocal locked
        assert kwargs["mode"] == "exclusive"
        assert kwargs["operation"] == "profile_set_default"
        with original_lock(**kwargs):
            locked = True
            try:
                yield
            finally:
                locked = False

    def changed_manifest() -> Any:
        nonlocal checks
        checks += 1
        pointer, manifest = original_check()
        if checks == 2:
            assert locked
            manifest = manifest.model_copy(update={"data_generation": 999})
        else:
            assert not locked
        return pointer, manifest

    monkeypatch.setattr(profile_management, "installation_operation_lock", track_lock)
    monkeypatch.setattr(profile_management, "require_compatible_installation", changed_manifest)
    with pytest.raises(ProfileManagementError, match="installation changed"):
        set_default_profile("personal")
    assert checks == 2
    assert not locked
    assert (root / "ricky.toml").read_bytes() == before


def test_set_default_restores_registry_after_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _initialize(tmp_path)
    add_profile("personal")
    before = (root / "ricky.toml").read_bytes()
    attempts = 0

    def fail_after_write(path: Path, content: str) -> None:
        nonlocal attempts
        attempts += 1
        write_private_file(path, content)
        if attempts == 1:
            raise OSError("simulated post-replacement failure")

    monkeypatch.setattr(profile_management, "write_private_file", fail_after_write)
    with pytest.raises(OSError, match="post-replacement failure"):
        set_default_profile("personal")
    assert attempts == 2
    assert (root / "ricky.toml").read_bytes() == before


def test_profile_cli_set_default_json_and_human(tmp_path: Path) -> None:
    _initialize(tmp_path)
    add_profile("personal")
    result = runner.invoke(app, ["profile", "set-default", "personal", "--json"])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == {
        "previous_default": "shared",
        "default_profile": "personal",
        "changed": True,
    }
    unchanged = runner.invoke(app, ["profile", "set-default", "personal"])
    assert unchanged.exit_code == 0
    assert "already personal" in unchanged.stdout
    reset = runner.invoke(app, ["profile", "set-default", "shared"])
    assert reset.exit_code == 0
    assert "set to shared" in reset.stdout
    missing = runner.invoke(app, ["profile", "set-default", "missing", "--json"])
    assert missing.exit_code == 1
    assert "profile is not enabled" in json.loads(missing.stdout)["error"]
