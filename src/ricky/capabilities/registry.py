"""Derived, inspectable registry of installed tools and skills."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, cast

from ricky.builtins import bundled_skills_dir
from ricky.capabilities.types import (
    CapabilityDefinition,
    CapabilityResource,
    CapabilityRisk,
    CapabilitySpec,
)
from ricky.config import RickySettings, user_data_path
from ricky.profiles import BUNDLED_OWNER
from ricky.skills.registry import SkillRegistry
from ricky.tool_contracts import ToolContractError, inspect_tool_contract
from ricky.tools.base import (
    StateGuardRegistry,
    Tool,
)


class CapabilityRegistryError(RuntimeError):
    pass


_PROJECT_ROOT_TOOL_IDS = frozenset(
    {
        "read_file",
        "list_dir",
        "glob_search",
        "grep_search",
        "write_file",
        "edit_file",
        "run_shell",
    }
)


class CapabilityRegistry:
    def __init__(self, definitions: Iterable[CapabilityDefinition] = ()) -> None:
        self._definitions: dict[str, CapabilityDefinition] = {}
        self._resource_owner: dict[tuple[str, str], str] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: CapabilityDefinition) -> None:
        if definition.id in self._definitions:
            raise CapabilityRegistryError(f"duplicate capability id: {definition.id}")
        for resource in definition.resources:
            key = (resource.kind, resource.id)
            existing = self._resource_owner.get(key)
            if existing is not None:
                raise CapabilityRegistryError(
                    f"resource {resource.id!r} belongs to two primary capabilities: "
                    f"{existing}, {definition.id}"
                )
        self._definitions[definition.id] = definition
        for resource in definition.resources:
            self._resource_owner[(resource.kind, resource.id)] = definition.id

    def get(self, capability_id: str) -> CapabilityDefinition | None:
        return self._definitions.get(capability_id)

    def require(self, capability_id: str) -> CapabilityDefinition:
        definition = self.get(capability_id)
        if definition is None:
            raise CapabilityRegistryError(f"unknown or inactive capability: {capability_id}")
        return definition

    def definitions(self) -> tuple[CapabilityDefinition, ...]:
        return tuple(self._definitions[key] for key in sorted(self._definitions))

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))

    def for_resource(self, kind: str, resource_id: str) -> CapabilityDefinition | None:
        owner = self._resource_owner.get((kind, resource_id))
        return self._definitions.get(owner) if owner is not None else None

    def digest(self) -> str:
        payload = [item.model_dump(mode="json") for item in self.definitions()]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_capability_registry(
    tools: Iterable[Tool],
    skills: SkillRegistry,
    *,
    capability_specs: Iterable[CapabilitySpec],
    external_tool_owners: Mapping[str, str] | None = None,
    skill_owners: Mapping[str, str] | None = None,
    state_guards: StateGuardRegistry | None = None,
) -> CapabilityRegistry:
    """Derive the inventory from exact registered resources and provenance."""

    tool_list = tuple(tools)
    tool_by_name = {tool.name: tool for tool in tool_list}
    if len(tool_by_name) != len(tool_list):
        raise CapabilityRegistryError("tool names must be unique before capability mapping")
    spec_list = tuple(capability_specs)
    specs = {spec.id: spec for spec in spec_list}
    if len(specs) != len(spec_list):
        raise CapabilityRegistryError("capability spec ids must be unique")
    owners = dict(external_tool_owners or {})
    grouped: dict[str, list[Tool]] = {}
    direct: list[tuple[Tool, str]] = []
    for tool in tool_list:
        owner = owners.get(tool.name)
        is_builtin = owner is None
        if is_builtin:
            owner = "builtin"
        metadata = validate_tool_contract(tool, state_guards=state_guards)
        capability_id = metadata["capability_id"]
        assert isinstance(owner, str)
        if capability_id is None:
            if is_builtin:
                raise CapabilityRegistryError(
                    f"registered built-in tool has no primary capability: {tool.name}"
                )
            direct.append((tool, owner))
            continue
        if owner == "builtin":
            if not capability_id.startswith("builtin."):
                raise CapabilityRegistryError(
                    f"built-in tool {tool.name} cannot claim another owner namespace: "
                    f"{capability_id}"
                )
        elif capability_id != owner and not capability_id.startswith(f"{owner}."):
            raise CapabilityRegistryError(
                f"tool {tool.name} cannot claim another owner namespace: {capability_id}"
            )
        grouped.setdefault(capability_id, []).append(tool)

    definitions: list[CapabilityDefinition] = []
    for capability_id, members in sorted(grouped.items()):
        spec = specs.get(capability_id)
        if spec is None:
            raise CapabilityRegistryError(
                f"declared capability has no shared spec: {capability_id}"
            )
        resources = tuple(
            _tool_resource(tool) for tool in sorted(members, key=lambda item: item.name)
        )
        risks = [tool.risk for tool in members]
        risk = cast(
            CapabilityRisk,
            max(
                risks,
                key={"read_only": 0, "mutating": 1, "destructive": 2}.__getitem__,
            ),
        )
        if capability_id.endswith(".read") and risk != "read_only":
            offenders = ", ".join(sorted(tool.name for tool in members if tool.risk != "read_only"))
            raise CapabilityRegistryError(
                f"read capability {capability_id} contains mutating tools: {offenders}"
            )
        blockers = tuple(
            f"{tool.name}: unattended forbidden"
            for tool in sorted(members, key=lambda item: item.name)
            if cast(Any, tool).unattended == "forbidden"
        )
        definitions.append(
            CapabilityDefinition(
                id=capability_id,
                version=spec.version,
                kind="tool_group",
                description=spec.description,
                owner=spec.owner,
                resources=resources,
                risk_class=risk,
                guardrail_schema_id=spec.guardrail_schema_id,
                authority_capability=spec.authority_capability,
                unattended_eligible=not blockers,
                unattended_blockers=blockers,
            )
        )

    for tool, owner in sorted(direct, key=lambda item: item[0].name):
        capability_id = _direct_capability_id(owner, tool.name)
        blockers = (
            (f"{tool.name}: unattended forbidden",)
            if cast(Any, tool).unattended == "forbidden"
            else ()
        )
        definitions.append(
            CapabilityDefinition(
                id=capability_id,
                kind="direct_tool",
                description=tool.description,
                owner=owner,
                resources=(_tool_resource(tool),),
                risk_class=tool.risk,
                unattended_eligible=not blockers,
                unattended_blockers=blockers,
            )
        )

    owners_by_skill = dict(skill_owners or {})
    for skill in skills.skills():
        scope = owners_by_skill.get(
            skill.qualified_name,
            BUNDLED_OWNER if skill.profile == BUNDLED_OWNER else "user",
        )
        if scope not in {BUNDLED_OWNER, "user"}:
            raise CapabilityRegistryError(
                f"invalid skill capability owner for {skill.name}: {scope}"
            )
        # A bundled skill owns no profile, so its identifier carries no profile
        # segment. A user skill stays qualified by its owning profile.
        capability_id = (
            f"{BUNDLED_OWNER}.skill.{skill.name}"
            if scope == BUNDLED_OWNER
            else f"{scope}.skill.{skill.profile}.{skill.name}"
        )
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", skill.name) is None:
            raise CapabilityRegistryError(
                f"skill name cannot form a stable capability id: {skill.name}"
            )
        digest = skill_bundle_digest(skill.source_path, skill.bundle_path)
        definitions.append(
            CapabilityDefinition(
                id=capability_id,
                kind="skill",
                description=skill.description,
                owner=scope,
                resources=(
                    CapabilityResource(
                        kind="skill",
                        id=skill.qualified_name,
                        contract_version=1,
                        digest=digest,
                        provenance=skill.source_path,
                    ),
                ),
                risk_class="read_only",
            )
        )
    return CapabilityRegistry(definitions)


def tool_contract_digest(tool: Tool) -> str:
    payload = {
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.Params.model_json_schema(),
        "risk": tool.risk,
        "contract_version": int(getattr(tool, "contract_version", 1)),
        # Version-1 callable digests included this historical field. Preserve
        # those immutable snapshots without using it as live safety metadata.
        "never_unattended": bool(getattr(tool, "legacy_contract_never_unattended", False)),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def capability_requires_project_root(definition: CapabilityDefinition) -> bool:
    """Whether selecting this capability requires an exact project binding.

    Only filesystem tools bind a project root. No skill does, because skills are
    discovered from the bundled root and profile roots, never from a project.
    """

    return any(
        resource.kind == "tool" and resource.id in _PROJECT_ROOT_TOOL_IDS
        for resource in definition.resources
    )


def derive_skill_owners(
    skills: SkillRegistry,
    *,
    settings: RickySettings,
) -> dict[str, str]:
    """Resolve skill ownership from configured roots, never path spelling."""

    bundled_root = bundled_skills_dir().resolve()
    owners: dict[str, str] = {}
    for skill in skills.skills():
        source = Path(skill.source_path).resolve()
        if source.is_relative_to(bundled_root):
            owners[skill.qualified_name] = BUNDLED_OWNER
        elif skill.profile != BUNDLED_OWNER and source.is_relative_to(
            (user_data_path(settings) / "profiles" / skill.profile / "skills").resolve()
        ):
            owners[skill.qualified_name] = "user"
        else:
            raise CapabilityRegistryError(f"skill source is outside configured roots: {skill.name}")
    return owners


def registered_skill_owners(registry: CapabilityRegistry) -> dict[str, str]:
    """Project exact skill ownership from an already validated inventory."""

    return {
        resource.id: definition.owner
        for definition in registry.definitions()
        if definition.kind == "skill"
        for resource in definition.resources
    }


def _tool_resource(tool: Tool) -> CapabilityResource:
    declared = cast(Any, tool)
    return CapabilityResource(
        kind="tool",
        id=tool.name,
        contract_version=int(getattr(tool, "contract_version", 1)),
        digest=tool_contract_digest(tool),
        provenance=f"{type(tool).__module__}.{type(tool).__qualname__}",
        risk_class=tool.risk,
        effect_kind=declared.effect_kind,
        unattended=declared.unattended,
        state_guard_id=declared.state_guard_id,
        review_mode=getattr(declared, "review_mode", "policy"),
    )


def validate_tool_contract(
    tool: Tool,
    *,
    state_guards: StateGuardRegistry | None = None,
) -> dict[str, str | None]:
    """Validate one complete execution-neutral tool metadata declaration."""
    try:
        metadata = inspect_tool_contract(tool, state_guards=state_guards)
    except ToolContractError as exc:
        raise CapabilityRegistryError(str(exc)) from exc
    return metadata.model_dump(mode="python")


def _direct_capability_id(owner: str, tool_name: str) -> str:
    if re.fullmatch(r"[a-z][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)*", owner) is None:
        raise CapabilityRegistryError(f"invalid external capability owner: {owner}")
    if owner == "builtin" or owner.startswith("builtin."):
        raise CapabilityRegistryError("external tools cannot claim the builtin namespace")
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", tool_name) is None:
        raise CapabilityRegistryError(f"invalid direct tool capability name: {tool_name}")
    return f"{owner}.{tool_name}"


def skill_bundle_digest(source_path: str, bundle_path: str | None) -> str:
    """Digest one declarative skill body and every passive bundle resource."""

    digest = hashlib.sha256()
    source = Path(source_path).resolve()
    root = Path(bundle_path).resolve() if bundle_path is not None else source.parent
    candidates = sorted(path for path in root.rglob("*") if path.is_file())
    if bundle_path is None:
        candidates = [source]
    for path in candidates:
        if not path.resolve().is_relative_to(root):
            raise CapabilityRegistryError("skill resource escapes its bundle")
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
