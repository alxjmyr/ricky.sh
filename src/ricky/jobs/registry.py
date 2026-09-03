"""Safe user and bundled job discovery with exact snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from ricky.builtins import bundled_jobs_dir
from ricky.config import RickySettings, profile_data_path
from ricky.jobs.spec import JobSpec, validate_job_name
from ricky.jobs.types import JobValidationError, JobValidationReport
from ricky.jobs.workflow import configured_workflow_bundle, workflow_bundle_digest
from ricky.profiles import BUNDLED_OWNER, ProfileResourceRef, ProfileScope


@dataclass(frozen=True)
class LoadedJob:
    """Validated job plus exact execution inputs and provenance."""

    spec: JobSpec
    goal: str
    bundle_path: Path
    source_files: tuple[tuple[str, bytes], ...]
    digest: str
    resource: ProfileResourceRef


class JobRegistry:
    """Discover strict job bundles with user-over-bundled precedence."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        profile_scope: ProfileScope,
    ) -> None:
        self.settings = settings
        self.profile_scope = profile_scope
        self.user_dirs = tuple(
            (
                profile_data_path(settings, profile) / settings.jobs.spec_dir,
                profile,
            )
            for profile in profile_scope.profiles
        )
        self.bundled_dir = bundled_jobs_dir()

    def _roots(self) -> list[tuple[Path, str]]:
        """Return every discovery root, user-owned roots first."""

        return [*self.user_dirs, (self.bundled_dir, BUNDLED_OWNER)]

    def find(self, name: str) -> Path | None:
        """Find one confined bundle by stable name."""

        requested_profile, separator, local_name = name.partition("/")
        if not separator:
            requested_profile = ""
            local_name = name
        validate_job_name(local_name)
        matches: dict[str, Path] = {}
        for root, owner_profile in self._roots():
            if requested_profile and requested_profile != owner_profile:
                continue
            candidate = root / local_name
            if not candidate.exists() and not candidate.is_symlink():
                continue
            bundle = self._confined_bundle(root, candidate)
            source = (bundle / "job.toml").resolve()
            if not source.is_relative_to(bundle):
                raise ValueError(f"job definition escapes its bundle: {source}")
            if source.is_file():
                matches.setdefault(owner_profile, bundle)
        if not matches:
            return None
        if requested_profile:
            return matches[requested_profile]
        # A user-owned bundle shadows a bundled bundle of the same bare name.
        owned = {profile: path for profile, path in matches.items() if profile != BUNDLED_OWNER}
        preferred = owned or matches
        if len(preferred) > 1:
            choices = ", ".join(f"{profile}/{local_name}" for profile in sorted(preferred))
            raise ValueError(f"ambiguous job name {name!r}; use one of: {choices}")
        return next(iter(preferred.values()))

    def load(self, name: str) -> LoadedJob:
        """Load and fully validate one named job."""

        bundle = self.find(name)
        if bundle is None:
            raise ValueError(f"no job bundle named '{name}'")
        _, _, local_name = name.rpartition("/")
        return self.load_bundle(bundle, expected_name=local_name or name)

    def load_bundle(
        self,
        bundle: Path,
        *,
        expected_name: str | None = None,
        owner_profile: str | None = None,
    ) -> LoadedJob:
        """Load a safely resolved bundle and compute its execution digest."""

        resolved = bundle.resolve()
        source = (resolved / "job.toml").resolve()
        if not source.is_relative_to(resolved) or not source.is_file():
            raise ValueError(f"job.toml is missing or escapes bundle: {bundle}")
        raw = source.read_bytes()
        try:
            document = tomllib.loads(raw.decode("utf-8"))
            spec = JobSpec.model_validate(document)
        except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValidationError) as exc:
            raise ValueError(f"invalid job definition {source}: {exc}") from exc
        bundle_name = expected_name or resolved.name
        validate_job_name(bundle_name)
        if spec.name != bundle_name:
            raise ValueError(f"job name {spec.name!r} does not match bundle name {bundle_name!r}")
        instructions = (resolved / "INSTRUCTIONS.md").resolve()
        if not instructions.is_relative_to(resolved):
            raise ValueError(f"job instructions escape bundle: {instructions}")
        has_instructions = instructions.is_file()
        if (spec.goal is None) == (not has_instructions):
            raise ValueError("job must define exactly one of goal or INSTRUCTIONS.md")
        files: list[tuple[str, bytes]] = [("job.toml", raw)]
        workflow = configured_workflow_bundle(
            spec,
            settings=self.settings,
            profile_scope=self.profile_scope,
        )
        if workflow is not None:
            files.extend(
                (
                    ("workflow.identity", workflow.resource.qualified.encode("utf-8")),
                    ("workflow.digest", workflow_bundle_digest(workflow).encode("ascii")),
                )
            )
        if has_instructions:
            instruction_bytes = instructions.read_bytes()
            try:
                goal = instruction_bytes.decode("utf-8").strip()
            except UnicodeDecodeError as exc:
                raise ValueError("INSTRUCTIONS.md must be UTF-8") from exc
            if not goal:
                raise ValueError("INSTRUCTIONS.md cannot be blank")
            if len(goal) > 50_000:
                raise ValueError("INSTRUCTIONS.md exceeds 50000 characters")
            files.append(("INSTRUCTIONS.md", instruction_bytes))
        else:
            goal = spec.goal or ""
        spec_manifest = spec.model_dump(mode="json")
        if spec.result_notification == "always":
            # Keep omitted/default notification policy out of the resolved
            # manifest so adding this optional field does not drift existing
            # job and schedule digests.
            spec_manifest.pop("result_notification")
        if spec.browser is None:
            # Preserve version-3 bundle digests from before the optional Phase 7 field.
            spec_manifest.pop("browser")
        resolved_manifest = json.dumps(
            {"spec": spec_manifest, "goal": goal},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        files.append(("resolved.json", resolved_manifest))
        digest = _digest(files)
        return LoadedJob(
            spec=spec,
            goal=goal,
            bundle_path=resolved,
            source_files=tuple(files),
            digest=digest,
            resource=ProfileResourceRef(
                profile=owner_profile or self._owner_profile(resolved),
                name=spec.name,
            ),
        )

    def discover(self) -> tuple[list[LoadedJob], list[JobValidationError]]:
        """Discover all bundles, reserving user names over bundled names."""

        loaded: list[LoadedJob] = []
        errors: list[JobValidationError] = []
        reserved: set[str] = set()
        for root, owner_profile in self._roots():
            if not root.is_dir():
                continue
            for source in sorted(root.glob("*/job.toml")):
                name = source.parent.name
                qualified = f"{owner_profile}/{name}"
                if qualified in reserved:
                    continue
                reserved.add(qualified)
                try:
                    bundle = self._confined_bundle(root, source.parent)
                    loaded.append(
                        self.load_bundle(
                            bundle,
                            expected_name=name,
                            owner_profile=owner_profile,
                        )
                    )
                except (OSError, ValueError) as exc:
                    errors.append(
                        JobValidationError(source_path=str(source), message=str(exc)[:2_000])
                    )
        return sorted(loaded, key=lambda item: item.resource.qualified), errors

    def validate(self, name: str) -> JobValidationReport:
        """Return a shape-only report without constructing a provider."""

        source = self.find(name)
        source_path = str(source / "job.toml") if source is not None else name
        try:
            self.load(name)
        except (OSError, ValueError) as exc:
            return JobValidationReport(
                name=name,
                source_path=source_path,
                spec_valid=False,
                errors=[JobValidationError(source_path=source_path, message=str(exc))],
            )
        return JobValidationReport(name=name, source_path=source_path, spec_valid=True)

    @staticmethod
    def _confined_bundle(root: Path, candidate: Path) -> Path:
        try:
            resolved_root = root.resolve()
            bundle = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"cannot safely resolve job bundle: {candidate}") from exc
        if not bundle.is_relative_to(resolved_root):
            raise ValueError(f"job bundle escapes jobs directory: {candidate}")
        return bundle

    def snapshot(self, loaded: LoadedJob) -> Path:
        """Persist the exact files that affected execution under the digest."""

        # A bundled job owns no profile data root, so its snapshot belongs to
        # the primary profile that ran it.
        owner = (
            self.profile_scope.primary
            if loaded.resource.profile == BUNDLED_OWNER
            else loaded.resource.profile
        )
        root = profile_data_path(self.settings, owner) / self.settings.jobs.run_dir / "specs"
        target = root / loaded.digest
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        target.mkdir(mode=0o700, exist_ok=True)
        os.chmod(target, 0o700)
        for name, content in loaded.source_files:
            path = target / name
            if path.exists():
                if path.read_bytes() != content:
                    raise ValueError(f"spec snapshot digest collision: {loaded.digest}") from None
                continue
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                if path.read_bytes() != content:
                    raise ValueError(f"spec snapshot digest collision: {loaded.digest}") from None
                continue
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        return target

    def _owner_profile(self, bundle: Path) -> str:
        """Resolve bundle ownership from configured roots without path guessing."""

        resolved = bundle.resolve()
        for root, profile in self._roots():
            if resolved.is_relative_to(root.resolve()):
                return profile
        raise ValueError(f"job bundle is outside the configured job roots: {bundle}")


def _digest(files: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for name, content in sorted(files):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def context_definition_digest(loaded: LoadedJob) -> str:
    """Digest only authored meaning that requires an explicit context decision."""

    workflow_digest = dict(loaded.source_files).get("workflow.digest")
    workflow_identity = dict(loaded.source_files).get("workflow.identity")
    payload = {
        "goal": loaded.goal,
        "workflow": (
            {
                "identity": (
                    workflow_identity.decode("utf-8")
                    if workflow_identity is not None
                    else loaded.spec.workflow.name
                ),
                "bundle_digest": workflow_digest.decode("ascii") if workflow_digest else None,
            }
            if loaded.spec.workflow is not None
            else None
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
