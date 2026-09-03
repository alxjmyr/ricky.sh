"""Strict serialized contracts for released-installation upgrade planning."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal, Self
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ricky.upgrades.versions import ReleaseVersion

RELEASE_DESCRIPTOR_FORMAT_VERSION = 1
RELEASE_REPOSITORY = "alxjmyr/ricky.sh"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
_PYTHON_REQUIREMENT_PATTERN = re.compile(
    r">=(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*))?"
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ReleaseArtifact(_StrictModel):
    """One exact, bounded HTTPS release artifact."""

    name: str = Field(min_length=1, max_length=255)
    url: str = Field(min_length=1, max_length=2_048)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    size: int = Field(gt=0, le=16 * 1024 * 1024 * 1024)

    @field_validator("name")
    @classmethod
    def _safe_basename(cls, value: str) -> str:
        if value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
            raise ValueError("release artifact name must be a plain filename")
        return value

    @field_validator("url")
    @classmethod
    def _safe_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        https = parsed.scheme == "https" and bool(parsed.hostname)
        local_file = (
            parsed.scheme == "file"
            and parsed.hostname in {None, ""}
            and parsed.path.startswith("/")
        )
        if not (https or local_file) or any(
            (
                parsed.username is not None,
                parsed.password is not None,
                bool(parsed.fragment),
                bool(parsed.query),
            )
        ):
            raise ValueError(
                "release artifact URL must be credential-free HTTPS or an absolute local file"
            )
        return value


class ReleaseDescriptor(_StrictModel):
    """Authenticated-by-digest metadata for one stable Ricky release."""

    format_version: Literal[1] = RELEASE_DESCRIPTOR_FORMAT_VERSION
    repository: Literal["alxjmyr/ricky.sh"] = RELEASE_REPOSITORY
    channel: Literal["stable"] = "stable"
    source: Literal["github_release", "local_drill"] = "github_release"
    software_version: ReleaseVersion
    supported_source_data_generations: tuple[int, ...] = Field(min_length=1)
    target_data_generation: int = Field(ge=1)
    python_requirement: str = Field(min_length=5, max_length=40)
    minimum_uv_version: ReleaseVersion
    wheel: ReleaseArtifact
    constraints: ReleaseArtifact

    @field_validator("supported_source_data_generations")
    @classmethod
    def _canonical_source_generations(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(item < 1 for item in value):
            raise ValueError("source data generations must be positive")
        if tuple(sorted(set(value))) != value:
            raise ValueError("source data generations must be sorted and unique")
        return value

    @field_validator("python_requirement")
    @classmethod
    def _bounded_python_requirement(cls, value: str) -> str:
        if _PYTHON_REQUIREMENT_PATTERN.fullmatch(value) is None:
            raise ValueError("python_requirement must use >=MAJOR.MINOR or >=MAJOR.MINOR.PATCH")
        return value

    @model_validator(mode="after")
    def _bound_release_assets(self) -> Self:
        version = str(self.software_version)
        expected_wheel = f"ricky-{version}-py3-none-any.whl"
        expected_constraints = f"ricky-{version}-constraints.txt"
        if self.wheel.name != expected_wheel:
            raise ValueError(f"wheel name must be {expected_wheel}")
        if self.constraints.name != expected_constraints:
            raise ValueError(f"constraints name must be {expected_constraints}")
        for artifact in (self.wheel, self.constraints):
            parsed = urlsplit(artifact.url)
            if self.source == "local_drill":
                if parsed.scheme != "file":
                    raise ValueError("local-drill artifacts must use absolute file URLs")
                continue
            expected_path = f"/{RELEASE_REPOSITORY}/releases/download/v{version}/{artifact.name}"
            if (
                parsed.hostname != "github.com"
                or parsed.port not in {None, 443}
                or unquote(parsed.path) != expected_path
                or parsed.query
            ):
                raise ValueError(
                    "release artifact URL must identify the descriptor's exact repository, "
                    "tag, and filename"
                )
        return self

    @property
    def minimum_python_parts(self) -> tuple[int, int, int]:
        """Return the descriptor's minimum Python version as three integers."""

        matched = _PYTHON_REQUIREMENT_PATTERN.fullmatch(self.python_requirement)
        if matched is None:  # pragma: no cover - model validation establishes this.
            raise AssertionError("validated Python requirement no longer matches")
        major, minor, patch = matched.groups()
        return int(major), int(minor), int(patch or 0)


class MigrationStep(_StrictModel):
    """One stable, ordered adapter-owned migration step identity."""

    adapter_id: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=100)
    step_id: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=100)
    target_id: str = Field(default="installation", pattern=_IDENTIFIER_PATTERN, max_length=200)
    physical_path: str | None = Field(default=None, max_length=4_096)
    source_schema_version: int = Field(ge=0)
    target_schema_version: int = Field(ge=0)
    depends_on: tuple[str, ...] = ()

    @field_validator("physical_path")
    @classmethod
    def _canonical_physical_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        if not path.is_absolute() or path != path.resolve():
            raise ValueError("migration physical_path must be absolute and canonical")
        return value

    @field_validator("depends_on")
    @classmethod
    def _canonical_dependencies(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("migration dependencies must be sorted and unique")
        return value


class AdapterTarget(_StrictModel):
    """One existing or potentially absent subsystem-owned durable target."""

    adapter_id: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=100)
    target_id: str = Field(pattern=_IDENTIFIER_PATTERN, max_length=200)
    path: str = Field(min_length=1, max_length=4_096)
    physical_path: str = Field(min_length=1, max_length=4_096)
    kind: Literal["sqlite", "file", "tree", "managed_integration"]

    @field_validator("path", "physical_path")
    @classmethod
    def _canonical_path(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or path != path.resolve():
            raise ValueError("upgrade target paths must be absolute and canonical")
        return value


class AdapterInspection(_StrictModel):
    """Read-only structural and integrity result for one adapter target."""

    target: AdapterTarget
    state: Literal["absent", "current", "migration_required", "unsupported", "corrupt"]
    found_schema_version: int | None = Field(default=None, ge=0)
    target_schema_version: int = Field(ge=0)
    integrity_valid: bool
    detail: str = Field(min_length=1, max_length=1_000)


class AdapterPreflight(_StrictModel):
    """Bounded backup and mutation declaration produced without writes."""

    target: AdapterTarget
    estimated_backup_bytes: int = Field(ge=0)
    backup_paths: tuple[str, ...] = ()
    touches_authored_files: bool = False
    touches_encrypted_bytes: bool = False
    touches_managed_integrations: bool = False

    @field_validator("backup_paths")
    @classmethod
    def _canonical_backup_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("backup paths must be sorted and unique")
        for rendered in value:
            path = Path(rendered)
            if not path.is_absolute() or path != path.resolve():
                raise ValueError("backup paths must be absolute and canonical")
        return value


class MigrationPlan(_StrictModel):
    """A complete deterministic migration plan bound to its SHA-256 digest."""

    source_data_generation: int = Field(ge=1)
    target_data_generation: int = Field(ge=1)
    steps: tuple[MigrationStep, ...] = ()
    plan_digest: str = Field(pattern=_SHA256_PATTERN)

    @classmethod
    def create(
        cls,
        *,
        source_data_generation: int,
        target_data_generation: int,
        steps: tuple[MigrationStep, ...] = (),
    ) -> MigrationPlan:
        """Create a plan with a digest over its complete ordered payload."""

        digest = _migration_plan_digest(source_data_generation, target_data_generation, steps)
        return cls(
            source_data_generation=source_data_generation,
            target_data_generation=target_data_generation,
            steps=steps,
            plan_digest=digest,
        )

    @model_validator(mode="after")
    def _verified_digest(self) -> Self:
        expected = _migration_plan_digest(
            self.source_data_generation,
            self.target_data_generation,
            self.steps,
        )
        if self.plan_digest != expected:
            raise ValueError("migration plan digest does not match its ordered steps")
        identities = [(step.adapter_id, step.step_id) for step in self.steps]
        if len(identities) != len(set(identities)):
            raise ValueError("migration plan contains a duplicate step identity")
        return self


class CompatibilityResult(_StrictModel):
    """Read-only compatibility decision for one selected release."""

    compatible: bool
    issues: tuple[str, ...] = ()
    migration_required: bool = False
    managed_reconciliation_required: bool = False

    @model_validator(mode="after")
    def _coherent_decision(self) -> Self:
        if self.compatible == bool(self.issues):
            raise ValueError("compatible must be true exactly when issues is empty")
        return self


class UpgradeCheckRequest(_StrictModel):
    """Safe local facts supplied to a read-only release check."""

    current_software_version: ReleaseVersion
    current_data_generation: int = Field(ge=1)
    requested_version: ReleaseVersion | None = None
    python_version: str = Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
    uv_version: ReleaseVersion | None = None
    inventory: tuple[AdapterInspection, ...] = ()

    @property
    def python_parts(self) -> tuple[int, int, int]:
        major, minor, patch = self.python_version.split(".")
        return int(major), int(minor), int(patch)


class UpgradeCheckResult(_StrictModel):
    """One complete machine-readable outcome from a read-only upgrade check."""

    status: Literal["no_update", "update_available", "incompatible"]
    current_software_version: ReleaseVersion
    current_data_generation: int = Field(ge=1)
    selected_release: ReleaseDescriptor | None = None
    compatibility: CompatibilityResult
    plan: MigrationPlan | None = None
    inventory: tuple[AdapterInspection, ...] = ()
    detail: str = Field(min_length=1, max_length=1_000)


def _migration_plan_digest(
    source_data_generation: int,
    target_data_generation: int,
    steps: tuple[MigrationStep, ...],
) -> str:
    payload = {
        "source_data_generation": source_data_generation,
        "target_data_generation": target_data_generation,
        "steps": [step.model_dump(mode="json") for step in steps],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
