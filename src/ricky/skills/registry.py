"""Skill discovery and activation registry."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ricky.builtins import bundled_skills_dir
from ricky.config import RickySettings, profile_data_path
from ricky.profiles import BUNDLED_OWNER, ProfileScope
from ricky.skills.spec import (
    ActiveSkill,
    Skill,
    SkillCatalogEntry,
    SkillLoadError,
    parse_skill_markdown,
)

USER_SKILLS_DIR = "skills"


class SkillSession(Protocol):
    """Session shape required for skill activation."""

    active_skill: ActiveSkill | None


@dataclass(frozen=True)
class SkillActivation:
    """Result of activating a prompt skill."""

    skill: ActiveSkill | None
    previous_skill: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.skill is not None


class SkillRegistry:
    """Registry of loaded skill definitions and load errors."""

    def __init__(
        self,
        skills: list[Skill] | tuple[Skill, ...] = (),
        *,
        errors: list[SkillLoadError] | tuple[SkillLoadError, ...] = (),
    ) -> None:
        self._skills = {skill.qualified_name: skill for skill in skills}
        if len(self._skills) != len(skills):
            raise ValueError("profile-qualified skill names must be unique")
        grouped: dict[str, list[Skill]] = {}
        for skill in skills:
            grouped.setdefault(skill.name, []).append(skill)
        self._aliases: dict[str, str] = {}
        for name, group in grouped.items():
            # A user-owned skill shadows a bundled skill of the same bare name.
            # Ambiguity between two enabled profiles still removes the alias.
            owned = [item for item in group if item.profile != BUNDLED_OWNER]
            preferred = owned or group
            if len(preferred) == 1:
                self._aliases[name] = preferred[0].qualified_name
        self._errors = list(errors)

    @property
    def errors(self) -> list[SkillLoadError]:
        """Return malformed skill files seen during discovery."""
        return list(self._errors)

    def get(self, name: str) -> Skill | None:
        """Return a skill by name."""
        return self._skills.get(name) or self._skills.get(self._aliases.get(name, ""))

    def skills(self) -> list[Skill]:
        """Return loaded skills sorted by name."""
        return sorted(self._skills.values(), key=lambda skill: skill.qualified_name)

    def identifiers(self) -> set[str]:
        """Return qualified identities plus safe unique local aliases."""

        return {*self._skills, *self._aliases}

    def catalog(self) -> list[SkillCatalogEntry]:
        """Return compact routing metadata sorted by skill name."""
        return [skill.catalog_entry for skill in self.skills()]

    def prompt_listing(self) -> str:
        """Return a compact model-visible list of available skill names."""
        if not self._skills:
            return "No skills are currently available."
        lines = []
        for entry in self.catalog():
            identity = (
                entry.name
                if self._aliases.get(entry.name) == entry.qualified_name
                else entry.qualified_name
            )
            lines.append(f"- {identity}: {entry.description} [profile: {entry.profile}]")
        return "\n".join(lines)

    def activate(self, session: SkillSession, name: str, args: str = "") -> SkillActivation:
        """Activate a prompt skill on a session."""
        skill = self.get(name)
        if skill is None:
            return SkillActivation(skill=None, error=f"Unknown skill: {name}")
        previous = session.active_skill.qualified_name if session.active_skill is not None else None
        active = ActiveSkill(
            name=skill.name,
            profile=skill.profile,
            args=args.strip(),
            description=skill.description,
            body=skill.body,
            source_path=skill.source_path,
            bundle_path=skill.bundle_path,
        )
        session.active_skill = active
        return SkillActivation(skill=active, previous_skill=previous)

    def resolve_resource(self, active_skill: ActiveSkill, resource_path: str) -> Path:
        """Resolve one path beneath the active skill's current bundle."""
        skill = self.get(active_skill.qualified_name)
        if skill is None or skill.source_path != active_skill.source_path:
            raise ValueError(f"Active skill is no longer loaded: {active_skill.name}")
        if skill.bundle_path is None or active_skill.bundle_path is None:
            raise ValueError(
                f"Skill '{active_skill.name}' uses the legacy flat-file format and "
                "has no bundle resources."
            )
        if skill.bundle_path != active_skill.bundle_path:
            raise ValueError(f"Active skill bundle changed: {active_skill.name}")

        relative = Path(resource_path)
        if not resource_path.strip():
            raise ValueError("Skill resource path cannot be empty.")
        if relative.is_absolute():
            raise ValueError(f"Skill resource path must be bundle-relative: {resource_path}")
        if ".." in relative.parts:
            raise ValueError(f"Skill resource path cannot contain '..': {resource_path}")

        bundle_root = Path(active_skill.bundle_path).resolve()
        resolved = (bundle_root / relative).resolve()
        if not resolved.is_relative_to(bundle_root):
            raise ValueError(f"Skill resource path escapes bundle: {resource_path}")
        if not resolved.exists():
            raise ValueError(f"Skill resource does not exist: {resource_path}")
        if not resolved.is_file():
            raise ValueError(f"Skill resource is not a file: {resource_path}")
        return resolved


def discover_skills(
    *,
    settings: RickySettings | None = None,
    profile_scope: ProfileScope,
) -> SkillRegistry:
    """Discover profile-owned skills, then skills distributed with Ricky."""
    resolved_settings = settings or RickySettings()
    directories: list[tuple[Path, str]] = [
        (profile_data_path(resolved_settings, profile) / USER_SKILLS_DIR, profile)
        for profile in profile_scope.profiles
    ]
    directories.append((bundled_skills_dir(), BUNDLED_OWNER))

    skills: dict[str, Skill] = {}
    errors: list[SkillLoadError] = []
    for directory, profile in directories:
        loaded, load_errors = _load_dir(directory, profile=profile)
        errors.extend(load_errors)
        for skill in loaded:
            if skill.qualified_name not in skills:
                skills[skill.qualified_name] = skill

    return SkillRegistry(list(skills.values()), errors=errors)


def _load_dir(directory: Path, *, profile: str) -> tuple[list[Skill], list[SkillLoadError]]:
    if not directory.is_dir():
        return [], []

    skills: list[Skill] = []
    errors: list[SkillLoadError] = []
    seen: set[str] = set()
    bundle_paths = sorted(directory.glob("*/SKILL.md"))
    legacy_paths = sorted(
        path for path in directory.glob("*.md") if path.name.lower() != "readme.md"
    )
    candidates = [
        *((path, path.parent) for path in bundle_paths),
        *((path, None) for path in legacy_paths),
    ]
    directory_root = directory.resolve()

    for path, bundle_path in candidates:
        try:
            resolved_path = path.resolve()
            if not resolved_path.is_relative_to(directory_root):
                raise ValueError(f"skill path escapes skills directory: {path}")
            if not resolved_path.is_file():
                raise ValueError(f"skill definition is not a file: {path}")

            resolved_bundle = bundle_path.resolve() if bundle_path is not None else None
            if resolved_bundle is not None and not resolved_bundle.is_relative_to(directory_root):
                raise ValueError(f"skill bundle escapes skills directory: {bundle_path}")

            skill = parse_skill_markdown(
                path,
                profile=profile,
                bundle_path=resolved_bundle,
            )
            if bundle_path is not None and skill.name != bundle_path.name:
                raise ValueError(
                    f"bundle directory '{bundle_path.name}' must match skill name '{skill.name}'"
                )
        except Exception as exc:  # noqa: BLE001 - malformed files become load errors.
            errors.append(SkillLoadError(source_path=str(path), message=str(exc)))
            continue
        if skill.name in seen:
            errors.append(
                SkillLoadError(
                    source_path=str(path),
                    message=f"shadowed duplicate skill name in {directory}: {skill.name}",
                )
            )
            continue
        seen.add(skill.name)
        skills.append(skill)
    return skills, errors
