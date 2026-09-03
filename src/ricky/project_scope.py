"""Explicit project discovery scope for runtime composition."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ricky.config import find_project_root


@dataclass(frozen=True)
class ProjectScope:
    """Distinguish intentional projectless operation from implicit discovery."""

    enabled: bool
    root: Path | None

    def __post_init__(self) -> None:
        if self.enabled != (self.root is not None):
            raise ValueError(
                "enabled project scope requires one root; disabled scope requires none"
            )

    @classmethod
    def discover(cls, root: Path | None = None) -> ProjectScope:
        return cls(enabled=True, root=find_project_root(root).resolve())

    @classmethod
    def disabled(cls) -> ProjectScope:
        return cls(enabled=False, root=None)

    @classmethod
    def bound(cls, root: Path) -> ProjectScope:
        return cls(enabled=True, root=root.expanduser().resolve())
