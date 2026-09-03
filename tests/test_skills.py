"""Tests for skill parsing, discovery, activation, and context integration."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from ricky.agent import AgentLoop, AgentSession
from ricky.agent.events import SkillActivatedEvent
from ricky.config import RickySettings
from ricky.llm import CompletionRequest, Message, MessageDone, StreamEvent, TextPart, ToolCallPart
from ricky.profiles import ProfileScope
from ricky.skills.registry import SkillRegistry, discover_skills
from ricky.skills.spec import ActiveSkill, Skill, SkillCatalogEntry, parse_skill_markdown
from ricky.skills.tool import ReadSkillResourceTool, UseSkillTool
from ricky.tools import ToolContext, ToolRegistry, builtin_tools


class FakeProvider:
    name = "fake"

    def __init__(self, scripts: list[list[StreamEvent]]) -> None:
        self.scripts = scripts
        self.requests: list[CompletionRequest] = []

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        for event in self.scripts.pop(0):
            yield event

    async def aclose(self) -> None:
        pass


def _write_skill(
    path: Path,
    *,
    name: str = "commit-helper",
    description: str = "Commit carefully",
    body: str = "Follow the commit sequence.",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
name: {name}
description: {description}
---
{body}
""",
        encoding="utf-8",
    )


def test_parse_skill_markdown_and_reports_malformed_files(
    tmp_path: Path, bundled_root: Path
) -> None:
    valid = tmp_path / "commit.md"
    _write_skill(valid, body="Do the thing.")
    malformed = tmp_path / "bad.md"
    malformed.write_text("name: missing frontmatter\n", encoding="utf-8")

    skill = parse_skill_markdown(valid, profile="personal")
    registry = discover_skills(profile_scope=ProfileScope.create("personal"))

    assert skill.name == "commit-helper"
    assert skill.body == "Do the thing."
    assert registry.skills() == []

    _write_skill(bundled_root / "skills" / "valid.md")
    (bundled_root / "skills" / "bad.md").write_text("not-frontmatter\n")
    registry = discover_skills(profile_scope=ProfileScope.create("personal"))

    assert [skill.name for skill in registry.skills()] == ["commit-helper"]
    assert len(registry.errors) == 1
    assert "frontmatter" in registry.errors[0].message


def test_parse_standard_agent_skills_frontmatter(tmp_path: Path) -> None:
    source = tmp_path / "portable-skill" / "SKILL.md"
    source.parent.mkdir()
    source.write_text(
        """---
name: portable-skill
description: >
  Transform portable skill definitions.
  Use when moving skills between agents.
license: Apache-2.0
compatibility: Requires Python 3.12 or newer
metadata:
  author: example-org
  version: "1.0"
allowed-tools: Read Bash(git:*)
---
# Portable skill

Follow the portable instructions.
""",
        encoding="utf-8",
    )

    skill = parse_skill_markdown(
        source,
        profile="personal",
        bundle_path=source.parent,
    )

    assert skill.name == "portable-skill"
    assert skill.description == (
        "Transform portable skill definitions. Use when moving skills between agents.\n"
    )
    assert skill.license == "Apache-2.0"
    assert skill.compatibility == "Requires Python 3.12 or newer"
    assert skill.metadata == {"author": "example-org", "version": "1.0"}
    assert skill.allowed_tools == "Read Bash(git:*)"
    assert skill.body.startswith("# Portable skill")
    assert Skill.model_validate_json(skill.model_dump_json()) == skill
    assert not hasattr(skill.catalog_entry, "allowed_tools")


@pytest.mark.parametrize(
    ("frontmatter", "expected"),
    [
        ("name: Bad_Name\ndescription: Invalid name", "string_pattern_mismatch"),
        ("name: duplicate\nname: duplicate\ndescription: Duplicate", "duplicate key"),
        ("name: blank-description\ndescription: '   '", "must not be blank"),
        (
            "name: numeric-metadata\ndescription: Invalid metadata\nmetadata:\n  version: 1",
            "string_type",
        ),
        ("name: obsolete\ndescription: Old Ricky format\nkind: prompt", "extra_forbidden"),
    ],
)
def test_invalid_or_obsolete_skill_frontmatter_is_rejected(
    tmp_path: Path,
    frontmatter: str,
    expected: str,
) -> None:
    source = tmp_path / "SKILL.md"
    source.write_text(f"---\n{frontmatter}\n---\nInstructions.\n", encoding="utf-8")

    with pytest.raises(ValueError, match=expected):
        parse_skill_markdown(source, profile="personal")


def test_discovery_user_skills_override_bundled_skills_and_ignore_legacy_root(
    tmp_path: Path,
    bundled_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_data = tmp_path / "custom-user-data"
    legacy_home = tmp_path / "legacy-home"
    monkeypatch.setenv("HOME", str(legacy_home))
    _write_skill(
        user_data / "profiles" / "personal" / "skills" / "shared.md",
        name="shared",
        description="User version",
        body="user body",
    )
    _write_skill(
        bundled_root / "skills" / "shared.md",
        name="shared",
        description="Bundled version",
        body="bundled body",
    )
    _write_skill(
        legacy_home / ".config" / "ricky" / "skills" / "legacy-only.md",
        name="legacy-only",
        description="Ignored legacy root",
    )

    settings = RickySettings(user_data_dir=str(user_data))
    registry = discover_skills(
        settings=settings,
        profile_scope=ProfileScope.create("personal"),
    )

    skill = registry.get("shared")
    assert skill is not None
    assert skill.description == "User version"
    assert skill.body == "user body"
    # The shadowed bundled skill stays reachable by its qualified identity.
    bundled = registry.get("bundled/shared")
    assert bundled is not None
    assert bundled.description == "Bundled version"
    assert registry.get("legacy-only") is None


def test_bundle_discovery_derives_catalog_and_ignores_resources(bundled_root: Path) -> None:
    skills_root = bundled_root / "skills"
    bundle = skills_root / "repo-orient"
    _write_skill(
        bundle / "SKILL.md",
        name="repo-orient",
        description="Explore a repo",
        body="PRIVATE FULL INSTRUCTIONS",
    )
    (bundle / "references").mkdir()
    (bundle / "references" / "notes.md").write_text(
        "PRIVATE REFERENCE CONTENT",
        encoding="utf-8",
    )
    (skills_root / "README.md").write_text("# Human documentation\n", encoding="utf-8")

    registry = discover_skills(profile_scope=ProfileScope.create("personal"))

    assert registry.errors == []
    assert [skill.name for skill in registry.skills()] == ["repo-orient"]
    skill = registry.get("repo-orient")
    assert skill is not None
    assert skill.bundle_path == str(bundle.resolve())
    assert Skill.model_validate_json(skill.model_dump_json()) == skill

    catalog = registry.catalog()
    assert len(catalog) == 1
    assert SkillCatalogEntry.model_validate_json(catalog[0].model_dump_json()) == catalog[0]
    assert not hasattr(catalog[0], "body")
    listing = registry.prompt_listing()
    assert "repo-orient: Explore a repo" in listing
    assert "PRIVATE FULL INSTRUCTIONS" not in listing
    assert "PRIVATE REFERENCE CONTENT" not in listing


def test_bundle_discovery_reports_mismatch_and_symlink_escape(
    tmp_path: Path, bundled_root: Path
) -> None:
    skills_root = bundled_root / "skills"
    _write_skill(
        skills_root / "valid" / "SKILL.md",
        name="valid",
        description="Valid neighbor",
    )
    _write_skill(
        skills_root / "wrong-directory" / "SKILL.md",
        name="different-name",
        description="Mismatched bundle",
    )
    outside_bundle = tmp_path / "outside-bundle"
    _write_skill(
        outside_bundle / "SKILL.md",
        name="escape",
        description="Outside bundle",
    )
    (skills_root / "escape").symlink_to(outside_bundle, target_is_directory=True)

    registry = discover_skills(profile_scope=ProfileScope.create("personal"))

    assert [skill.name for skill in registry.skills()] == ["valid"]
    messages = "\n".join(error.message for error in registry.errors)
    assert "must match skill name" in messages
    assert "escapes skills directory" in messages


def test_bundle_precedence_is_deterministic_and_legacy_stays_compatible(
    tmp_path: Path,
    bundled_root: Path,
) -> None:
    bundled_skills = bundled_root / "skills"
    user_data = tmp_path / "custom-user-data"
    user_skills = user_data / "profiles" / "personal" / "skills"
    _write_skill(
        bundled_skills / "shared.md",
        name="shared",
        description="Bundled legacy",
        body="bundled legacy body",
    )
    _write_skill(
        user_skills / "shared" / "SKILL.md",
        name="shared",
        description="User bundle",
        body="user bundle body",
    )
    _write_skill(
        bundled_skills / "local" / "SKILL.md",
        name="local",
        description="Bundled bundle",
        body="bundled bundle body",
    )
    _write_skill(
        bundled_skills / "local.md",
        name="local",
        description="Bundled legacy duplicate",
        body="legacy duplicate body",
    )

    registry = discover_skills(
        settings=RickySettings(user_data_dir=str(user_data)),
        profile_scope=ProfileScope.create("personal"),
    )

    shared = registry.get("shared")
    local = registry.get("local")
    assert shared is not None
    # The user bundle shadows the bundled legacy definition of the same name.
    assert shared.description == "User bundle"
    assert shared.bundle_path is not None
    assert local is not None
    assert local.description == "Bundled bundle"
    assert local.bundle_path == str((bundled_skills / "local").resolve())
    assert any("shadowed duplicate skill name" in error.message for error in registry.errors)


def test_obsolete_kind_field_is_reported_as_invalid(bundled_root: Path) -> None:
    obsolete = bundled_root / "skills" / "obsolete.md"
    obsolete.parent.mkdir(parents=True, exist_ok=True)
    obsolete.write_text(
        "---\nname: obsolete\ndescription: Old Ricky format\nkind: prompt\n---\nOld.\n",
        encoding="utf-8",
    )
    registry = discover_skills(profile_scope=ProfileScope.create("personal"))

    assert registry.skills() == []
    assert len(registry.errors) == 1
    assert "extra_forbidden" in registry.errors[0].message


@pytest.mark.asyncio
async def test_read_skill_resource_supports_bundled_and_user_bundles(
    tmp_path: Path, bundled_root: Path
) -> None:
    user_data = tmp_path / "custom-user-data"
    bundled_bundle = bundled_root / "skills" / "bundled-skill"
    user_bundle = user_data / "profiles" / "personal" / "skills" / "user-skill"
    _write_skill(
        bundled_bundle / "SKILL.md",
        name="bundled-skill",
        description="Bundled resources",
    )
    _write_skill(
        user_bundle / "SKILL.md",
        name="user-skill",
        description="User resources",
    )
    for bundle, label in ((bundled_bundle, "bundled"), (user_bundle, "user")):
        (bundle / "references").mkdir()
        (bundle / "references" / "guide.md").write_text(
            f"{label} first\n{label} second\n",
            encoding="utf-8",
        )

    settings = RickySettings(user_data_dir=str(user_data))
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    skill_registry = discover_skills(
        settings=settings,
        profile_scope=session.profile_scope,
    )
    registry = ToolRegistry([ReadSkillResourceTool(skill_registry)])
    ctx = ToolContext(cwd=tmp_path, settings=settings, session=session)

    for name, label in (("bundled-skill", "bundled"), ("user-skill", "user")):
        activation = skill_registry.activate(session, name)
        assert activation.ok
        result = await registry.dispatch(
            "read_skill_resource",
            {"path": "references/guide.md", "offset": 2, "limit": 1},
            ctx,
        )
        assert not result.is_error
        assert result.content == f"2: {label} second"

    assert session.active_skill is not None
    assert ActiveSkill.model_validate_json(session.active_skill.model_dump_json()) == (
        session.active_skill
    )


@pytest.mark.asyncio
async def test_read_skill_resource_rejects_invalid_or_unbundled_paths(
    tmp_path: Path, bundled_root: Path
) -> None:
    skills_root = bundled_root / "skills"
    bundle = skills_root / "safe-skill"
    _write_skill(
        bundle / "SKILL.md",
        name="safe-skill",
        description="Safe resources",
    )
    (bundle / "references").mkdir()
    (bundle / "references" / "guide.md").write_text("safe\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (bundle / "references" / "escape.md").symlink_to(outside)
    _write_skill(
        skills_root / "legacy.md",
        name="legacy",
        description="Legacy flat skill",
    )

    settings = RickySettings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    skill_registry = discover_skills(
        settings=settings,
        profile_scope=session.profile_scope,
    )
    registry = ToolRegistry([ReadSkillResourceTool(skill_registry)])
    ctx = ToolContext(cwd=tmp_path, settings=settings, session=session)

    inactive = await registry.dispatch(
        "read_skill_resource",
        {"path": "references/guide.md"},
        ctx,
    )
    assert inactive.is_error
    assert "No skill is active" in inactive.content

    assert skill_registry.activate(session, "legacy").ok
    legacy = await registry.dispatch(
        "read_skill_resource",
        {"path": "references/guide.md"},
        ctx,
    )
    assert legacy.is_error
    assert "legacy flat-file format" in legacy.content

    assert skill_registry.activate(session, "safe-skill").ok
    invalid_paths = {
        str(outside.resolve()): "bundle-relative",
        "../outside.txt": "cannot contain '..'",
        "references": "not a file",
        "references/missing.md": "does not exist",
        "references/escape.md": "escapes bundle",
    }
    for path, expected in invalid_paths.items():
        result = await registry.dispatch("read_skill_resource", {"path": path}, ctx)
        assert result.is_error
        assert expected in result.content


@pytest.mark.asyncio
async def test_use_skill_tool_activates_and_next_context_includes_body(tmp_path: Path) -> None:
    settings = RickySettings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    bundle = tmp_path / "repo-orient"
    bundle.mkdir()
    skill_registry = SkillRegistry(
        [
            Skill(
                name="repo-orient",
                description="Explore a repo",
                profile="personal",
                body="Start by mapping files.",
                source_path=str(bundle / "SKILL.md"),
                bundle_path=str(bundle),
            )
        ]
    )
    provider = FakeProvider(
        [
            [
                MessageDone(
                    message=Message(
                        role="assistant",
                        content=[
                            ToolCallPart(
                                id="call_skill",
                                name="use_skill",
                                args={"name": "repo-orient", "args": "this repo"},
                            )
                        ],
                    ),
                    stop_reason="tool_calls",
                )
            ],
            [MessageDone(message=Message(role="assistant", content=[TextPart(text="ready")]))],
        ]
    )
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(
            [
                *builtin_tools(),
                UseSkillTool(skill_registry),
                ReadSkillResourceTool(skill_registry),
            ]
        ),
        settings=settings,
        skill_registry=skill_registry,
        cwd=tmp_path,
    )

    events = [event async for event in loop.run_turn(session, "orient")]

    skill_events = [event for event in events if isinstance(event, SkillActivatedEvent)]
    assert len(skill_events) == 1
    assert skill_events[0].skill_name == "personal/repo-orient"
    assert session.active_skill is not None
    assert session.active_skill.args == "this repo"
    second_request_parts: list[str] = []
    for message in provider.requests[1].messages:
        for part in message.content:
            if isinstance(part, TextPart):
                second_request_parts.append(part.text)
    second_request_text = "\n".join(second_request_parts)
    assert "Active skill: personal/repo-orient" in second_request_text
    assert "Start by mapping files." in second_request_text
    assert "read_skill_resource" in second_request_text
    first_system_part = provider.requests[0].messages[0].content[0]
    assert isinstance(first_system_part, TextPart)
    assert "repo-orient: Explore a repo" in first_system_part.text
    assert "Start by mapping files." not in first_system_part.text
