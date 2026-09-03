"""Canonical host-path handling shared by local file capabilities."""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HostGlob:
    """One canonical glob root plus the pattern evaluated beneath it."""

    root: Path
    pattern: str
    canonical_pattern: str


def resolve_host_path(cwd: Path, path: str | Path) -> Path:
    """Resolve an absolute, home-relative, or workspace-relative host path.

    Resolution follows symlinks so authorization always describes the actual
    destination rather than an attacker-controlled link location.
    """

    raw = Path(path).expanduser()
    candidate = raw if raw.is_absolute() else cwd.resolve() / raw
    return candidate.resolve()


def resolve_host_glob(cwd: Path, pattern: str) -> HostGlob:
    """Split a host glob into a resolved static root and relative pattern."""

    expanded = os.path.expanduser(pattern)
    raw = Path(expanded)
    absolute = raw if raw.is_absolute() else cwd.resolve() / raw
    parts = absolute.parts
    magic_index = next(
        (index for index, part in enumerate(parts) if glob.has_magic(part)),
        None,
    )
    if magic_index is None:
        resolved = absolute.resolve()
        root = resolved.parent
        relative_pattern = resolved.name
    else:
        static = parts[:magic_index]
        root = Path(*static).resolve() if static else cwd.resolve()
        relative_pattern = str(Path(*parts[magic_index:]))
    return HostGlob(
        root=root,
        pattern=relative_pattern,
        canonical_pattern=str(root / relative_pattern),
    )


def is_workspace_path(cwd: Path, path: Path) -> bool:
    """Return whether a resolved path is within the active workspace."""

    return path.resolve().is_relative_to(cwd.resolve())


def resolve_under(cwd: Path, path: str | Path) -> Path:
    """Resolve ``path`` under ``cwd`` or reject a workspace escape.

    Kept for callers that intentionally require confinement. Interactive file
    tools use :func:`resolve_host_path` and apply the permission boundary.
    """

    root = cwd.resolve()
    resolved = resolve_host_path(root, path)
    if not resolved.is_relative_to(root):
        raise ValueError(f"path escapes workspace: {path}")
    return resolved


def display_path(cwd: Path, path: Path) -> str:
    """Return a workspace-relative path or an exact canonical host path."""

    resolved = path.resolve()
    root = cwd.resolve()
    if resolved.is_relative_to(root):
        return str(resolved.relative_to(root))
    return str(resolved)
