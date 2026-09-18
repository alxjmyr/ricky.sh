"""Profile-scoped configured browser resource resolution and confined paths."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ricky.config import (
    BrowserResourceSettings,
    RickySettings,
    ensure_private_user_data_root,
    profile_data_path,
    profile_data_subpath,
)
from ricky.profiles import ProfileResourceRef, ProfileScope


@dataclass(frozen=True)
class ResolvedBrowserResource:
    """One local configured resource already constrained to an issued scope."""

    ref: ProfileResourceRef
    settings: BrowserResourceSettings


class BrowserResourceSelectionError(ValueError):
    """A user must choose between browsers or configure an eligible browser."""


def select_browser_resource(
    settings: RickySettings,
    *,
    scope: ProfileScope,
    name: str | None = None,
    background: bool = False,
) -> ResolvedBrowserResource:
    """Resolve a scoped name or primary-profile default without widening access."""

    visible = resolve_browser_resources(settings, scope=scope)
    eligible = tuple(
        item
        for item in visible
        if not background or (item.settings.kind == "persistent" and item.settings.headless)
    )
    if name:
        name = name.strip()
        if "/" in name:
            matches = [item for item in visible if item.ref.qualified == name]
        else:
            matches = [item for item in visible if item.ref.name == name]
            primary = [item for item in matches if item.ref.profile == scope.primary]
            matches = primary or matches
        if len(matches) == 1:
            if matches[0] in eligible:
                return matches[0]
            raise ValueError(
                "browser resource is unavailable in the current scope or execution mode"
            )
        matches = [item for item in matches if item in eligible]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(
                "browser resource is unavailable in the current scope or execution mode"
            )
    else:
        matches = [item for item in eligible if item.ref.profile == scope.primary]
        profile = settings.profile_configs.get(scope.primary)
        default = profile.browser.default_resource if profile and profile.browser else None
        if default is not None:
            return select_browser_resource(
                settings,
                scope=scope,
                name=f"{scope.primary}/{default}",
                background=background,
            )
        if len(matches) == 1:
            return matches[0]
    choices = ", ".join(item.ref.qualified for item in matches[:10])
    if len(matches) > 10:
        choices += f", and {len(matches) - 10} more"
    raise BrowserResourceSelectionError(
        f"Which browser should I use? Available choices: {choices}."
        if choices
        else "No eligible browser is configured in your profile. Which browser should I use?"
    )


def resolve_browser_resources(
    settings: RickySettings,
    *,
    scope: ProfileScope,
) -> tuple[ResolvedBrowserResource, ...]:
    """Return only profile-qualified resources owned by the issued scope."""

    resources: list[ResolvedBrowserResource] = []
    for profile in scope.profiles:
        configured = settings.profile_configs.get(profile)
        if configured is None or configured.browser is None:
            continue
        for name, resource_settings in configured.browser.resources.items():
            resources.append(
                ResolvedBrowserResource(
                    ref=ProfileResourceRef(profile=profile, name=name),
                    settings=resource_settings,
                )
            )
    return tuple(sorted(resources, key=lambda item: item.ref.qualified))


def require_browser_resource(
    settings: RickySettings,
    *,
    scope: ProfileScope,
    ref: ProfileResourceRef,
) -> ResolvedBrowserResource | None:
    """Resolve one exact resource without allowing arguments to widen scope."""

    if ref.profile not in scope.profiles:
        return None
    configured = settings.profile_configs.get(ref.profile)
    if configured is None or configured.browser is None:
        return None
    resource_settings = configured.browser.resources.get(ref.name)
    if resource_settings is None:
        return None
    return ResolvedBrowserResource(ref=ref, settings=resource_settings)


def browser_resource_digest(ref: ProfileResourceRef) -> str:
    """Return a stable filesystem-safe key without interpolating authored names."""

    return hashlib.sha256(ref.qualified.encode("utf-8")).hexdigest()


def browser_resource_configuration_digest(resource: ResolvedBrowserResource) -> str:
    """Pin one exact non-secret configured resource revision."""

    payload = {
        "resource": resource.ref.model_dump(mode="json"),
        "settings": resource.settings.model_dump(mode="json"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def persistent_browser_path(settings: RickySettings, ref: ProfileResourceRef) -> Path:
    """Resolve one persistent Chrome profile below its owning Ricky profile."""

    relative = PurePosixPath(
        settings.browser.persistent_dir,
        browser_resource_digest(ref),
    ).as_posix()
    return _profile_browser_subpath(settings, ref.profile, relative)


def prepare_persistent_browser(settings: RickySettings, ref: ProfileResourceRef) -> Path:
    """Create private profile state after the caller acquires its resource lease."""

    ensure_private_user_data_root(settings)
    path = persistent_browser_path(settings, ref)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = persistent_browser_path(settings, ref)
    if current != path:
        raise ValueError("persistent browser path changed before creation")
    current.mkdir(exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(current.parent, 0o700)
        os.chmod(current, 0o700)
    return current


def browser_lease_path(settings: RickySettings, ref: ProfileResourceRef) -> Path:
    """Resolve one private advisory-lock path below the resource owner."""

    relative = PurePosixPath(
        settings.browser.lease_dir,
        f"{browser_resource_digest(ref)}.lock",
    ).as_posix()
    return _profile_browser_subpath(settings, ref.profile, relative)


def _profile_browser_subpath(settings: RickySettings, profile: str, relative: str) -> Path:
    """Confine a browser path and reject aliases through existing symlinks."""

    resolved = profile_data_subpath(settings, profile, relative)
    logical = profile_data_path(settings, profile).joinpath(*PurePosixPath(relative).parts)
    if resolved != logical:
        raise ValueError("browser resource paths must not contain symlinks")
    return resolved
