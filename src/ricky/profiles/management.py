"""Atomic lifecycle operations for profile compartments."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import uuid4

import tomlkit
from pydantic import BaseModel, ConfigDict
from tomlkit import TOMLDocument

from ricky.config import RickySettings, config_file, load_settings, user_data_path
from ricky.installation import (
    fsync_directory,
    installation_operation_lock,
    require_compatible_installation,
    write_private_file,
)
from ricky.profiles.types import (
    SHARED_PROFILE,
    ProfileName,
    ProfileResourceRef,
    ProfileScope,
    validate_profile_compartment,
)
from ricky.schedules.store import ScheduleStore, ScheduleStoreError

_PROFILE_CONFIG = """# Profile-specific configuration and policy.
# Add credentials to .secrets.toml only when they are needed.

[profile]
"""


class ProfileManagementError(RuntimeError):
    """A profile lifecycle operation could not complete safely."""


class _StrictResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ProfileAddResult(_StrictResult):
    """Serializable result of creating one profile compartment."""

    profile: ProfileName
    profile_dir: str
    config_path: str
    default_profile: ProfileName


class ProfileDeletionPlan(_StrictResult):
    """Serializable advisory description of one deletion that may proceed."""

    profile: ProfileName
    profile_dir: str


class ProfileDeleteResult(_StrictResult):
    """Serializable result of deleting one profile compartment."""

    profile: ProfileName
    profile_dir: str
    default_profile: ProfileName


def add_profile(name: str) -> ProfileAddResult:
    """Create and register one minimal private profile scaffold."""

    profile = validate_profile_compartment(name)
    if profile == SHARED_PROFILE:
        raise ProfileManagementError("the reserved shared profile already exists")
    expected_pointer, expected_manifest = require_compatible_installation()

    with installation_operation_lock(
        mode="exclusive",
        timeout_seconds=5.0,
        operation="profile_add",
    ):
        pointer, manifest = require_compatible_installation()
        if pointer != expected_pointer or manifest != expected_manifest:
            raise ProfileManagementError("Ricky installation changed while adding the profile")
        settings = load_settings()
        root = user_data_path(settings)
        if root != Path(pointer.user_data_dir):
            raise ProfileManagementError("resolved configuration does not match the installation")
        profiles_root = root / "profiles"
        target = profiles_root / profile
        if profile in settings.profiles.enabled:
            raise ProfileManagementError(f"profile already exists: {profile}")
        if target.exists() or target.is_symlink():
            raise ProfileManagementError(f"refusing existing unregistered profile path: {target}")

        document, original = _root_config_document(root)
        _add_registry_entry(document, profile, settings)
        # Lifecycle staging stays out of ``profiles/``, whose every entry must
        # be a real profile root. A crash or an undeletable tree would
        # otherwise leave a name that profile discovery has to reject.
        stage = _staging_path(root, profile, "add")

        published = False
        config_attempted = False
        try:
            stage.mkdir(mode=0o700)
            if os.name == "posix":
                os.chmod(stage, 0o700)
            write_private_file(stage / "ricky.toml", _PROFILE_CONFIG)
            os.replace(stage, target)
            published = True
            fsync_directory(profiles_root)
            fsync_directory(root)
            config_attempted = True
            write_private_file(config_file(root), tomlkit.dumps(document))
            _validate_committed_settings(root)
        except BaseException as exc:
            if config_attempted:
                _restore_root_config(root, original, failure=exc)
            # The scaffold must go even when the registry could not be
            # restored, so this runs on its own rather than after a call that
            # can raise.
            if published:
                _remove_staged_tree(target, profiles_root, failure=exc)
            else:
                _remove_staged_tree(stage, root, failure=exc)
            raise

        return ProfileAddResult(
            profile=profile,
            profile_dir=str(target),
            config_path=str(target / "ricky.toml"),
            default_profile=settings.profiles.default,
        )


async def check_profile_deletion(
    name: str,
    *,
    new_default: str | None = None,
) -> ProfileDeletionPlan:
    """Describe one deletion that may proceed, or refuse it with the reason.

    This check is advisory because no lock is held yet. It exists so an
    interface refuses a missing, reserved, default, or still-referenced profile
    before it asks for irreversible confirmation. :func:`delete_profile`
    recomputes the same refusals authoritatively under its exclusive lock.
    """

    profile, replacement = _validate_deletion_names(name, new_default)
    require_compatible_installation()
    settings = load_settings()
    await _refuse_unsafe_deletion(settings, profile, replacement)
    return ProfileDeletionPlan(
        profile=profile,
        profile_dir=str(user_data_path(settings) / "profiles" / profile),
    )


async def delete_profile(name: str, *, new_default: str | None = None) -> ProfileDeleteResult:
    """Delete one profile after proving its remaining references are safe."""

    profile, replacement = _validate_deletion_names(name, new_default)
    expected_pointer, expected_manifest = require_compatible_installation()

    with installation_operation_lock(
        mode="exclusive",
        timeout_seconds=5.0,
        operation="profile_delete",
    ):
        pointer, manifest = require_compatible_installation()
        if pointer != expected_pointer or manifest != expected_manifest:
            raise ProfileManagementError("Ricky installation changed while deleting the profile")
        settings = load_settings()
        root = user_data_path(settings)
        if root != Path(pointer.user_data_dir):
            raise ProfileManagementError("resolved configuration does not match the installation")

        await _refuse_unsafe_deletion(settings, profile, replacement)

        profiles_root = root / "profiles"
        target = profiles_root / profile
        if target.is_symlink() or not target.is_dir():
            raise ProfileManagementError(f"profile directory is missing or invalid: {target}")

        document, original = _root_config_document(root)
        _delete_registry_entry(document, profile, replacement, settings)
        tombstone = _staging_path(root, profile, "delete")

        os.replace(target, tombstone)
        try:
            fsync_directory(profiles_root)
            fsync_directory(root)
            write_private_file(config_file(root), tomlkit.dumps(document))
            committed = _validate_committed_settings(root)
        except BaseException as exc:
            _restore_root_config(root, original, failure=exc)
            _reattach_staged_tree(tombstone, target, profiles_root, root, failure=exc)
            raise

        try:
            shutil.rmtree(tombstone)
            fsync_directory(root)
        except OSError as exc:
            raise ProfileManagementError(
                f"profile was disabled and detached, but staged data remains at {tombstone}"
            ) from exc

        return ProfileDeleteResult(
            profile=profile,
            profile_dir=str(target),
            default_profile=committed.profiles.default,
        )


def _validate_deletion_names(name: str, new_default: str | None) -> tuple[str, str | None]:
    profile = validate_profile_compartment(name)
    if profile == SHARED_PROFILE:
        raise ProfileManagementError("the shared profile cannot be deleted")
    replacement = None if new_default is None else validate_profile_compartment(new_default)
    return profile, replacement


async def _refuse_unsafe_deletion(
    settings: RickySettings,
    profile: str,
    replacement: str | None,
) -> None:
    if profile not in settings.profiles.enabled:
        raise ProfileManagementError(f"profile does not exist: {profile}")
    _validate_default_replacement(settings, profile, replacement)
    blockers = list(_configured_profile_references(settings, profile))
    blockers.extend(await _schedule_profile_references(settings, profile))
    if blockers:
        rendered = ", ".join(sorted(blockers))
        raise ProfileManagementError(f"profile {profile!r} is still referenced by: {rendered}")


def _staging_path(root: Path, profile: str, operation: str) -> Path:
    path = root / f".profile-{operation}-{profile}-{uuid4().hex[:8]}"
    if path.exists() or path.is_symlink():  # pragma: no cover - opaque collision
        raise ProfileManagementError(f"profile staging path already exists: {path}")
    return path


def _remove_staged_tree(path: Path, parent: Path, *, failure: BaseException) -> None:
    """Remove one lifecycle tree without replacing the failure being reported."""

    if path.is_symlink() or not path.is_dir():
        return
    try:
        shutil.rmtree(path)
        fsync_directory(parent)
    except OSError as exc:
        failure.add_note(f"could not remove the staged profile tree at {path}: {exc}")


def _reattach_staged_tree(
    tombstone: Path,
    target: Path,
    profiles_root: Path,
    root: Path,
    *,
    failure: BaseException,
) -> None:
    """Put one detached profile tree back without replacing the reported failure."""

    if target.exists() or tombstone.is_symlink() or not tombstone.is_dir():
        if tombstone.exists() or tombstone.is_symlink():
            failure.add_note(f"staged profile data remains at {tombstone}")
        return
    try:
        os.replace(tombstone, target)
        fsync_directory(profiles_root)
        fsync_directory(root)
    except OSError as exc:
        failure.add_note(f"could not reattach the profile tree staged at {tombstone}: {exc}")


def _root_config_document(root: Path) -> tuple[TOMLDocument, str]:
    path = config_file(root)
    if path.is_symlink() or not path.is_file():
        raise ProfileManagementError(f"installation configuration is missing or invalid: {path}")
    original = path.read_text(encoding="utf-8")
    try:
        document = tomlkit.parse(original)
    except (RuntimeError, ValueError) as exc:
        raise ProfileManagementError(f"invalid installation configuration: {path}") from exc
    return document, original


def _profiles_table(document: TOMLDocument) -> dict[str, object]:
    profiles = document.get("profiles")
    if not isinstance(profiles, dict):
        raise ProfileManagementError("installation configuration requires a profiles table")
    return profiles


def _add_registry_entry(document: TOMLDocument, profile: str, settings: RickySettings) -> None:
    profiles = _profiles_table(document)
    profiles["enabled"] = sorted(
        [*settings.profiles.enabled, profile],
        key=lambda item: (item != SHARED_PROFILE, item),
    )
    definitions = profiles.get("definitions")
    if not isinstance(definitions, dict):
        definitions = tomlkit.table()
        profiles["definitions"] = definitions
    definitions[profile] = tomlkit.table()


def _delete_registry_entry(
    document: TOMLDocument,
    profile: str,
    replacement: str | None,
    settings: RickySettings,
) -> None:
    profiles = _profiles_table(document)
    profiles["enabled"] = [item for item in settings.profiles.enabled if item != profile]
    definitions = profiles.get("definitions")
    if isinstance(definitions, dict):
        definitions.pop(profile, None)
        if not definitions:
            profiles.pop("definitions", None)
    if replacement is not None:
        profiles["default"] = replacement


def _validate_default_replacement(
    settings: RickySettings,
    profile: str,
    replacement: str | None,
) -> None:
    if settings.profiles.default == profile:
        if replacement is None:
            raise ProfileManagementError(
                f"profile {profile!r} is the default; pass --new-default with another "
                "enabled profile"
            )
        if replacement == profile or replacement not in settings.profiles.enabled:
            raise ProfileManagementError("the replacement default must be another enabled profile")
    elif replacement is not None:
        raise ProfileManagementError(
            "--new-default is valid only when deleting the current default profile"
        )


def _configured_profile_references(settings: RickySettings, profile: str) -> tuple[str, ...]:
    references: list[str] = []
    for name in settings.messaging.telegram_accounts:
        if ProfileResourceRef.from_qualified(name).profile == profile:
            references.append(f"messaging.telegram_accounts.{name}")
    for name, transport in settings.messaging.transports.items():
        if transport.account_ref.profile == profile:
            references.append(f"messaging.transports.{name}.account")
    for name, route in settings.messaging.routes.items():
        if route.owner_profile == profile:
            references.append(f"messaging.routes.{name}.owner_profile")
        if profile in route.accepted_profiles:
            references.append(f"messaging.routes.{name}.accepted_profiles")
    for name, route in settings.gateway.routes.items():
        if route.primary_profile == profile:
            references.append(f"gateway.routes.{name}.primary_profile")
        if profile in route.access_profiles:
            references.append(f"gateway.routes.{name}.access_profiles")
    for name, ceiling in settings.authority.capabilities.items():
        if profile in ceiling.allowed_profiles:
            references.append(f"authority.capabilities.{name}.allowed_profiles")
    # A profile-owned configuration may also lower an authority ceiling onto
    # another profile, and no settings validator rejects a stale name there.
    for owner, configured in settings.profile_configs.items():
        if owner == profile or configured.authority is None:
            continue
        for name, ceiling in configured.authority.capabilities.items():
            if profile in ceiling.allowed_profiles:
                references.append(
                    f"profiles.{owner}.authority.capabilities.{name}.allowed_profiles"
                )
    return tuple(sorted(references))


async def _schedule_profile_references(
    settings: RickySettings,
    profile: str,
) -> tuple[str, ...]:
    scope = ProfileScope.create(
        settings.profiles.default,
        access_profiles=settings.profiles.enabled,
    )
    try:
        # A safety refusal reads the whole registry: a scope-filtered listing
        # hides every schedule pinned to a profile that is no longer enabled.
        references = await ScheduleStore(settings, scope=scope).list_references()
    except ScheduleStoreError as exc:
        raise ProfileManagementError(
            "profile deletion could not inspect the schedule registry"
        ) from exc
    # ``ScheduleSpec`` requires the qualified job owner to be inside the
    # schedule's pinned scope, so the pinned scope is the complete criterion.
    return tuple(
        f"schedules.{reference.id}"
        for reference in references
        if profile in reference.profile_scope.profiles
    )


def _validate_committed_settings(root: Path) -> RickySettings:
    settings = load_settings()
    if user_data_path(settings) != root:
        raise ProfileManagementError("committed profile registry resolved another data root")
    return settings


def _restore_root_config(root: Path, original: str, *, failure: BaseException) -> None:
    """Put the previous registry back without replacing the reported failure."""

    try:
        write_private_file(config_file(root), original)
    except OSError as exc:
        failure.add_note(f"could not restore the installation registry: {exc}")
