"""Version 2 workflow bundle discovery and registry."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from ricky.builtins import bundled_workflows_dir
from ricky.config import RickySettings, WorkflowSettings, profile_data_path
from ricky.profiles import BUNDLED_OWNER, ProfileResourceRef, ProfileScope
from ricky.workflows.compile import compile_workflow
from ricky.workflows.spec import (
    NAME_PATTERN,
    AgentStep,
    ModelTaskBase,
    ToolStep,
    WorkflowSpec,
    iter_steps,
    parse_workflow_toml,
)

if TYPE_CHECKING:
    from ricky.workflows.compile import ToolLookup

USER_WORKFLOWS_DIR = "workflows"


class WorkflowLoadError(BaseModel):
    """One bundle rejected during workflow discovery."""

    source_path: str
    message: str


@dataclass(frozen=True)
class LoadedWorkflow:
    """One validated workflow plus the bundle directory it came from."""

    spec: WorkflowSpec
    bundle_path: Path
    resource: ProfileResourceRef


class WorkflowRegistry:
    """Registry of compiled workflow bundles and load errors."""

    def __init__(
        self,
        workflows: list[LoadedWorkflow] | tuple[LoadedWorkflow, ...] = (),
        *,
        errors: list[WorkflowLoadError] | tuple[WorkflowLoadError, ...] = (),
    ) -> None:
        self._workflows = {loaded.resource.qualified: loaded for loaded in workflows}
        if len(self._workflows) != len(workflows):
            raise ValueError("profile-qualified workflow names must be unique")
        grouped: dict[str, list[LoadedWorkflow]] = {}
        for loaded in workflows:
            grouped.setdefault(loaded.spec.name, []).append(loaded)
        self._aliases: dict[str, str] = {}
        for name, group in grouped.items():
            # A user-owned workflow shadows a bundled workflow of the same bare
            # name. Ambiguity between two enabled profiles removes the alias.
            owned = [item for item in group if item.resource.profile != BUNDLED_OWNER]
            preferred = owned or group
            if len(preferred) == 1:
                self._aliases[name] = preferred[0].resource.qualified
        self._errors = list(errors)

    @property
    def errors(self) -> list[WorkflowLoadError]:
        """Return malformed workflow bundles seen during discovery."""
        return list(self._errors)

    def get(self, name: str) -> WorkflowSpec | None:
        """Return one loaded workflow by name."""
        loaded = self.loaded(name)
        return loaded.spec if loaded is not None else None

    def loaded(self, name: str) -> LoadedWorkflow | None:
        """Return the validated workflow and bundle path for one name."""
        return self._workflows.get(name) or self._workflows.get(self._aliases.get(name, ""))

    def workflows(self) -> list[WorkflowSpec]:
        """Return loaded workflows sorted by name."""
        return [
            loaded.spec
            for loaded in sorted(self._workflows.values(), key=lambda item: item.resource.qualified)
        ]

    def loaded_workflows(self) -> list[LoadedWorkflow]:
        """Return loaded workflows with their qualified profile provenance."""

        return sorted(self._workflows.values(), key=lambda item: item.resource.qualified)

    def identifiers(self) -> set[str]:
        """Return qualified identities plus unambiguous local aliases."""

        return {*self._workflows, *self._aliases}

    def replace_with(self, other: WorkflowRegistry) -> None:
        """Replace this registry snapshot while preserving object identity."""
        self._workflows = dict(other._workflows)
        self._aliases = dict(other._aliases)
        self._errors = list(other._errors)

    def prompt_listing(self) -> str:
        """Return a compact model-visible list with typed invocation args."""
        if not self._workflows:
            return "No workflows are currently available."
        lines: list[str] = []
        for loaded in self.loaded_workflows():
            spec = loaded.spec
            identity = (
                spec.name
                if self._aliases.get(spec.name) == loaded.resource.qualified
                else loaded.resource.qualified
            )
            lines.append(f"- {identity}: {spec.description} [profile: {loaded.resource.profile}]")
            for name, arg in spec.args.items():
                state = "required" if arg.required else f"default: {arg.default!r}"
                lines.append(f"    {name} ({arg.type}, {state}): {arg.description}")
        return "\n".join(lines)

    def resolve_resource(self, spec: WorkflowSpec, resource_path: str) -> Path:
        """Resolve one path beneath a loaded workflow's bundle."""
        loaded = next(
            (item for item in self._workflows.values() if item.spec is spec),
            None,
        )
        if loaded is None:
            raise ValueError(f"Workflow is not loaded: {spec.name}")
        return resolve_bundle_resource(loaded.bundle_path, resource_path)


def resolve_bundle_resource(bundle_root: Path, resource_path: str) -> Path:
    """Resolve one confined bundle-relative resource path."""
    if not resource_path.strip():
        raise ValueError("Workflow resource path cannot be empty.")
    relative = Path(resource_path)
    if relative.is_absolute():
        raise ValueError(f"Workflow resource path must be bundle-relative: {resource_path}")
    if ".." in relative.parts:
        raise ValueError(f"Workflow resource path cannot contain '..': {resource_path}")
    root = bundle_root.resolve()
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"Workflow resource path escapes bundle: {resource_path}")
    if not resolved.exists():
        raise ValueError(f"Workflow resource does not exist: {resource_path}")
    if not resolved.is_file():
        raise ValueError(f"Workflow resource is not a file: {resource_path}")
    return resolved


def find_workflow_bundle(
    name: str,
    *,
    settings: RickySettings | None = None,
    profile_scope: ProfileScope,
) -> Path | None:
    """Find one named bundle beneath the configured workflow roots."""
    requested_profile, separator, local_name = name.partition("/")
    if not separator:
        local_name = name
        requested_profile = ""
    if re.fullmatch(NAME_PATTERN, local_name) is None:
        raise ValueError(
            "workflow name must start with a lowercase letter or digit and contain "
            "only lowercase letters, digits, '-' and '_' (maximum 64 characters)"
        )
    resolved_settings = settings or RickySettings()
    directories: list[tuple[Path, str]] = [
        (profile_data_path(resolved_settings, profile) / USER_WORKFLOWS_DIR, profile)
        for profile in profile_scope.profiles
    ]
    directories.append((bundled_workflows_dir(), BUNDLED_OWNER))
    matches: dict[str, Path] = {}
    for directory, owner_profile in directories:
        if requested_profile and owner_profile != requested_profile:
            continue
        candidate = directory / local_name
        try:
            workflows_root = directory.resolve()
            if not candidate.exists() and not candidate.is_symlink():
                continue
            resolved_bundle = candidate.resolve()
            source = (resolved_bundle / "workflow.toml").resolve()
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"cannot safely resolve workflow bundle: {candidate}") from exc
        if not resolved_bundle.is_relative_to(workflows_root):
            raise ValueError(f"workflow bundle escapes workflows directory: {candidate}")
        if not source.is_relative_to(resolved_bundle):
            raise ValueError(
                f"workflow definition escapes its bundle: {candidate / 'workflow.toml'}"
            )
        if source.is_file():
            matches.setdefault(owner_profile, resolved_bundle)
    if requested_profile:
        return matches.get(requested_profile)
    # A user-owned bundle shadows a bundled bundle of the same bare name.
    owned = {profile: path for profile, path in matches.items() if profile != BUNDLED_OWNER}
    preferred = owned or matches
    if len(preferred) > 1:
        choices = ", ".join(f"{profile}/{local_name}" for profile in sorted(preferred))
        raise ValueError(f"workflow name {name!r} is ambiguous; use one of: {choices}")
    return next(iter(preferred.values()), None)


def discover_workflows(
    *,
    settings: RickySettings | None = None,
    profile_scope: ProfileScope,
    skill_names: set[str],
    tool_registry: ToolLookup,
    workflow_settings: WorkflowSettings | None = None,
) -> WorkflowRegistry:
    """Discover profile-owned workflows, then workflows distributed with Ricky."""
    resolved_settings = settings or RickySettings()
    limits = workflow_settings or resolved_settings.workflow
    workflows: dict[str, LoadedWorkflow] = {}
    errors: list[WorkflowLoadError] = []
    reserved_names: set[str] = set()
    directories: list[tuple[Path, str]] = [
        (profile_data_path(resolved_settings, profile) / USER_WORKFLOWS_DIR, profile)
        for profile in profile_scope.profiles
    ]
    directories.append((bundled_workflows_dir(), BUNDLED_OWNER))
    for directory, profile in directories:
        bundle_names = {source.parent.name for source in directory.glob("*/workflow.toml")}
        loaded, load_errors = _load_dir(
            directory,
            skill_names=skill_names,
            limits=limits,
            tool_registry=tool_registry,
            profile=profile,
        )
        errors.extend(load_errors)
        for item in loaded:
            if item.resource.qualified not in workflows:
                workflows[item.resource.qualified] = item
        reserved_names.update(bundle_names)
    return WorkflowRegistry(list(workflows.values()), errors=errors)


def load_workflow_bundle(
    bundle_path: Path,
    *,
    workflow_settings: WorkflowSettings | None = None,
) -> tuple[WorkflowSpec, list[str]]:
    """Parse one version 2 bundle and run bundle-file checks."""
    limits = workflow_settings or WorkflowSettings()
    bundle_root = bundle_path.resolve()
    source = (bundle_root / "workflow.toml").resolve()
    if not source.is_relative_to(bundle_root):
        raise ValueError(f"workflow definition escapes its bundle: {source}")
    if not source.is_file():
        raise ValueError(f"workflow definition is not a file: {source}")
    spec = parse_workflow_toml(source.read_text(encoding="utf-8"), source=str(source))
    if spec.name != bundle_root.name:
        raise ValueError(
            f"bundle directory '{bundle_root.name}' must match workflow name '{spec.name}'"
        )
    return spec, _bundle_file_errors(spec, bundle_root, limits)


def _load_dir(
    directory: Path,
    *,
    skill_names: set[str],
    limits: WorkflowSettings,
    tool_registry: ToolLookup,
    profile: str,
) -> tuple[list[LoadedWorkflow], list[WorkflowLoadError]]:
    if not directory.is_dir():
        return [], []
    loaded: list[LoadedWorkflow] = []
    errors: list[WorkflowLoadError] = []
    directory_root = directory.resolve()
    for source in sorted(directory.glob("*/workflow.toml")):
        bundle_path = source.parent
        try:
            resolved_bundle = bundle_path.resolve()
            if not resolved_bundle.is_relative_to(directory_root):
                raise ValueError(f"workflow bundle escapes workflows directory: {bundle_path}")
            spec, problems = load_workflow_bundle(bundle_path, workflow_settings=limits)
            compiled = compile_workflow(
                spec,
                tool_registry=tool_registry,
                skill_names=skill_names,
                settings=limits,
            )
            problems = [*compiled.errors, *problems]
            if problems:
                guidance = _unavailable_tools_message(spec, tool_registry)
                detail = "; ".join(problems)
                raise ValueError(f"{guidance}\nDetails: {detail}" if guidance else detail)
        except Exception as exc:  # noqa: BLE001 - malformed bundles become load errors.
            errors.append(WorkflowLoadError(source_path=str(source), message=str(exc)))
            continue
        loaded.append(
            LoadedWorkflow(
                spec=spec,
                bundle_path=resolved_bundle,
                resource=ProfileResourceRef(profile=profile, name=spec.name),
            )
        )
    return loaded, errors


def _unavailable_tools_message(spec: WorkflowSpec, tool_registry: ToolLookup) -> str:
    required: set[str] = set()
    for step in iter_steps(spec.steps):
        if isinstance(step, ToolStep):
            required.add(step.tool)
        elif isinstance(step, AgentStep):
            required.update(step.tools)
    missing = sorted(name for name in required if tool_registry.get(name) is None)
    if not missing:
        return ""
    message = (
        f"Workflow '{spec.name}' is unavailable because required tools are not available "
        f"in the current profile scope: {', '.join(missing)}. "
        "Check integration configuration and profile access. "
        "For custom workflows, also check the tool names."
    )
    if any(name.startswith("gmail_") for name in missing):
        message += (
            " Gmail tools require a configured Google account and matching OAuth credentials "
            "in an accessible profile. Complete Google setup in that profile, then retry "
            "with --profile or --access-profile as needed."
        )
    return message


def _bundle_file_errors(
    spec: WorkflowSpec, bundle_root: Path, limits: WorkflowSettings
) -> list[str]:
    errors: list[str] = []
    for step in iter_steps(spec.steps):
        if not isinstance(step, ModelTaskBase):
            continue
        if step.instruction is not None and len(step.instruction) > limits.instruction_char_limit:
            errors.append(
                f"step '{step.id}' instruction exceeds instruction_char_limit "
                f"({len(step.instruction)} > {limits.instruction_char_limit})"
            )
        if step.instruction_file is None:
            continue
        try:
            path = resolve_bundle_resource(bundle_root, step.instruction_file)
        except ValueError as exc:
            errors.append(f"step '{step.id}' instruction_file: {exc}")
            continue
        size = len(path.read_text(encoding="utf-8"))
        if size > limits.instruction_char_limit:
            errors.append(
                f"step '{step.id}' instruction_file exceeds instruction_char_limit "
                f"({size} > {limits.instruction_char_limit})"
            )
    return errors
