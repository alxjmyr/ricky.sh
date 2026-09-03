"""Strict release and whole-installation data-generation identities."""

from __future__ import annotations

import re
from functools import total_ordering
from typing import Any

from pydantic import ConfigDict, RootModel, model_validator

from ricky.installation import CURRENT_DATA_GENERATION

SUPPORTED_DATA_GENERATIONS: tuple[int, ...] = (CURRENT_DATA_GENERATION,)

_RELEASE_VERSION_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
_MAX_VERSION_LENGTH = 62


@total_ordering
class ReleaseVersion(RootModel[str]):
    """One canonical ``MAJOR.MINOR.PATCH`` release version.

    This is deliberately smaller than PEP 440. Ricky's first release channel
    accepts no epoch, suffix, prerelease, postrelease, or local-version syntax.
    The JSON representation is the canonical version string rather than an
    implementation-shaped object.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    @model_validator(mode="before")
    @classmethod
    def _canonical_release_version(cls, value: Any) -> Any:
        if isinstance(value, cls):
            return value.root
        if not isinstance(value, str):
            raise ValueError("release version must be a string")
        if len(value) > _MAX_VERSION_LENGTH or _RELEASE_VERSION_PATTERN.fullmatch(value) is None:
            raise ValueError("release version must use canonical MAJOR.MINOR.PATCH syntax")
        return value

    @classmethod
    def parse(cls, value: str) -> ReleaseVersion:
        """Parse a canonical release version."""

        return cls.model_validate(value)

    @property
    def parts(self) -> tuple[int, int, int]:
        """Return the three numeric components used for release ordering."""

        major, minor, patch = self.root.split(".")
        return int(major), int(minor), int(patch)

    def __str__(self) -> str:
        return self.root

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ReleaseVersion):
            return NotImplemented
        return self.parts < other.parts


def require_installed_release_version(value: str | ReleaseVersion) -> ReleaseVersion:
    """Validate a real installed release identity, excluding the source fallback."""

    parsed = value if isinstance(value, ReleaseVersion) else ReleaseVersion.parse(value)
    # ``0.0.0`` is syntactically canonical, so development detection must not
    # rely on the grammar alone. Ricky reserves it for missing package metadata.
    if parsed.parts == (0, 0, 0):
        raise ValueError("0.0.0 is not an installed Ricky release identity")
    return parsed
