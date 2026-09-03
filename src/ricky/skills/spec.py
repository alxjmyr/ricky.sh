"""Standard Agent Skills models and frontmatter parser."""

from __future__ import annotations

from collections.abc import Hashable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver

from ricky.profiles import ProfileName, validate_profile_name


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects ambiguous duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, Hashable):
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            )
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


class SkillCatalogEntry(BaseModel):
    """Compact routing metadata derived from one skill definition."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    description: str = Field(min_length=1, max_length=1024)
    profile: ProfileName
    source_path: str
    bundle_path: str | None = None

    @field_validator("description")
    @classmethod
    def _description_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("description must not be blank")
        return value

    @field_validator("profile")
    @classmethod
    def _profile_name(cls, value: str) -> str:
        return validate_profile_name(value)

    @property
    def qualified_name(self) -> str:
        """Return the profile-qualified registry identity."""

        return f"{self.profile}/{self.name}"


class Skill(SkillCatalogEntry):
    """A complete discoverable Agent Skills definition."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    license: str | None = None
    compatibility: str | None = Field(default=None, min_length=1, max_length=500)
    metadata: dict[str, str] = Field(default_factory=dict)
    allowed_tools: str | None = Field(default=None, alias="allowed-tools", min_length=1)
    body: str

    @property
    def catalog_entry(self) -> SkillCatalogEntry:
        """Return model-routing metadata without instructions or optional metadata."""
        return SkillCatalogEntry(
            name=self.name,
            description=self.description,
            profile=self.profile,
            source_path=self.source_path,
            bundle_path=self.bundle_path,
        )


class SkillLoadError(BaseModel):
    """A malformed skill file reported during discovery."""

    source_path: str
    message: str


class ActiveSkill(BaseModel):
    """A skill currently shaping a session."""

    name: str
    profile: ProfileName
    args: str = ""
    description: str
    body: str
    source_path: str
    bundle_path: str | None = None

    @property
    def qualified_name(self) -> str:
        """Return the profile-qualified active identity."""

        return f"{self.profile}/{self.name}"


def parse_skill_markdown(
    path: Path,
    *,
    profile: str,
    bundle_path: Path | None = None,
) -> Skill:
    """Parse one standard Agent Skills ``SKILL.md`` definition."""
    text = path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(text, path)
    raw = _parse_frontmatter(frontmatter, path)
    try:
        return Skill.model_validate(
            {
                **raw,
                "profile": validate_profile_name(profile),
                "body": body.strip(),
                "source_path": str(path),
                "bundle_path": str(bundle_path.resolve()) if bundle_path is not None else None,
            }
        )
    except ValidationError as exc:
        raise ValueError(f"invalid skill metadata: {exc}") from exc


def _split_frontmatter(text: str, path: Path) -> tuple[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{path.name} must start with frontmatter delimiter '---'")
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    raise ValueError(f"{path.name} is missing closing frontmatter delimiter '---'")


def _parse_frontmatter(frontmatter: str, path: Path) -> dict[str, Any]:
    try:
        parsed = yaml.load(frontmatter, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path.name} has invalid YAML frontmatter: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{path.name} frontmatter must be a YAML mapping")
    if not all(isinstance(key, str) for key in parsed):
        raise ValueError(f"{path.name} frontmatter keys must be strings")
    return parsed
