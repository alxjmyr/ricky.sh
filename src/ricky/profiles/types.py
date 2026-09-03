"""Canonical JSON-safe profile identity, scope, and provenance types."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

SHARED_PROFILE = "shared"
# Reserved owner for resources distributed with Ricky. It qualifies bundled
# identity as ``bundled/<name>``; it is never an enabled profile and never
# names a data compartment, so it resolves to no profile data directory.
BUNDLED_OWNER = "bundled"
PROFILE_NAME_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
_PROFILE_NAME_RE = re.compile(PROFILE_NAME_PATTERN)

ProfileName = Annotated[str, StringConstraints(pattern=PROFILE_NAME_PATTERN)]


def validate_profile_name(value: str) -> str:
    """Return one canonical profile name or raise a bounded validation error."""

    normalized = value.strip()
    if _PROFILE_NAME_RE.fullmatch(normalized) is None:
        raise ValueError(
            "profile names must start with a lowercase letter and contain only "
            "lowercase letters, digits, '-' and '_' (maximum 64 characters)"
        )
    return normalized


def validate_profile_compartment(value: str) -> str:
    """Return one profile name that may name a real data compartment.

    ``BUNDLED_OWNER`` qualifies resources distributed with Ricky. It is a valid
    resource-identity owner but never an enabled profile, so it must not enter a
    profile scope, a profile label, or a profile data path.
    """

    normalized = validate_profile_name(value)
    if normalized == BUNDLED_OWNER:
        raise ValueError(
            f"{BUNDLED_OWNER!r} is reserved for resources distributed with Ricky "
            "and cannot name a profile"
        )
    return normalized


def _canonical_profiles(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    normalized = tuple(validate_profile_compartment(value) for value in values)
    if len(normalized) != len(set(normalized)):
        raise ValueError("profile lists must not contain duplicates")
    return tuple(sorted(normalized, key=lambda item: (item != SHARED_PROFILE, item)))


class ProfileScope(BaseModel):
    """Immutable set of profiles issued to one agent runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    primary: ProfileName
    profiles: tuple[ProfileName, ...]

    @field_validator("primary")
    @classmethod
    def _primary_name(cls, value: str) -> str:
        return validate_profile_compartment(value)

    @field_validator("profiles", mode="before")
    @classmethod
    def _profile_names(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("profiles must be a list or tuple")
        return _canonical_profiles(list(value))

    @model_validator(mode="after")
    def _complete_scope(self) -> ProfileScope:
        if SHARED_PROFILE not in self.profiles:
            raise ValueError("every profile scope must include shared")
        if self.primary not in self.profiles:
            raise ValueError("primary profile must be present in the profile scope")
        return self

    @classmethod
    def create(
        cls,
        primary: str,
        *,
        access_profiles: tuple[str, ...] | list[str] = (),
    ) -> ProfileScope:
        """Construct a canonical scope, adding the mandatory shared profile."""

        primary = validate_profile_compartment(primary)
        requested = list(dict.fromkeys((SHARED_PROFILE, primary, *access_profiles)))
        return cls(
            primary=primary,
            profiles=_canonical_profiles(requested),
        )

    def includes(self, profile: str) -> bool:
        """Return whether one profile is available to this runtime."""

        return validate_profile_name(profile) in self.profiles

    def permits(self, label: ProfileLabel) -> bool:
        """Return whether this scope contains every profile required by a label."""

        return set(label.required_profiles).issubset(self.profiles)

    def narrow(
        self,
        primary: str,
        *,
        access_profiles: tuple[str, ...] | list[str] = (),
    ) -> ProfileScope:
        """Issue a same-or-narrower child scope and reject widening."""

        narrowed = ProfileScope.create(primary, access_profiles=access_profiles)
        unknown = sorted(set(narrowed.profiles) - set(self.profiles))
        if unknown:
            raise ValueError("child profile scope cannot add profiles: " + ", ".join(unknown))
        return narrowed

    def label(self) -> ProfileLabel:
        """Label derived runtime state with the complete available scope."""

        return ProfileLabel(required_profiles=self.profiles)

    def digest(self) -> str:
        """Return a stable digest suitable for pinned route/runtime evidence."""

        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ProfileLabel(BaseModel):
    """Profiles a runtime must possess to read one durable derived record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    required_profiles: tuple[ProfileName, ...]

    @field_validator("required_profiles", mode="before")
    @classmethod
    def _required_profile_names(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("required_profiles must be a list or tuple")
        if not value:
            raise ValueError("profile labels require at least one profile")
        return _canonical_profiles(list(value))

    @classmethod
    def owned_by(cls, profile: str) -> ProfileLabel:
        """Label data owned by exactly one authored profile."""

        return cls(required_profiles=(validate_profile_name(profile),))


class ProfileResourceRef(BaseModel):
    """Stable qualified identity for a resource owned by one profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: ProfileName
    name: str = Field(min_length=1, max_length=200)

    @field_validator("profile")
    @classmethod
    def _profile_name(cls, value: str) -> str:
        return validate_profile_name(value)

    @field_validator("name")
    @classmethod
    def _resource_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "/" in normalized or "\x00" in normalized:
            raise ValueError("profile resource names must be non-empty plain identifiers")
        return normalized

    @property
    def qualified(self) -> str:
        """Return the inspectable canonical external form."""

        return f"{self.profile}/{self.name}"

    @classmethod
    def from_qualified(cls, value: str) -> ProfileResourceRef:
        """Parse the canonical ``profile/name`` external form."""

        normalized = value.strip()
        profile, separator, name = normalized.partition("/")
        if not separator or "/" in name:
            raise ValueError("profile resource references must use profile/name")
        return cls(profile=profile, name=name)


class ProfileRoutingDecision(BaseModel):
    """Inspectable semantic routing evidence within an already-issued scope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profiles: tuple[ProfileName, ...]
    reason: str = Field(min_length=1, max_length=1_000)

    @field_validator("profiles", mode="before")
    @classmethod
    def _routed_profile_names(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("routing decisions require at least one profile")
        return _canonical_profiles(list(value))

    @field_validator("reason")
    @classmethod
    def _trim_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("profile routing reason cannot be empty")
        return normalized
