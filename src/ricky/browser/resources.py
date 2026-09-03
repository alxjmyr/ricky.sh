"""Profile-scoped configured browser resource resolution and confined paths."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ricky.config import (
    BrowserResourceSettings,
    RickySettings,
    profile_data_path,
    profile_data_subpath,
)
from ricky.profiles import ProfileResourceRef, ProfileScope


@dataclass(frozen=True)
class ResolvedBrowserResource:
    """One local configured resource already constrained to an issued scope."""

    ref: ProfileResourceRef
    settings: BrowserResourceSettings


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
    """Resolve one persistent Chromium profile below its owning Ricky profile."""

    relative = PurePosixPath(
        settings.browser.persistent_dir,
        browser_resource_digest(ref),
    ).as_posix()
    return _profile_browser_subpath(settings, ref.profile, relative)


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
