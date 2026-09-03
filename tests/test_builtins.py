"""Tests for the resources distributed with Ricky and their discovery roots."""

from __future__ import annotations

import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ricky.builtins import bundled_jobs_dir, bundled_root, bundled_skills_dir, bundled_workflows_dir
from ricky.config import RickySettings
from ricky.jobs.registry import JobRegistry
from ricky.llm import Usage
from ricky.profiles import BUNDLED_OWNER, ProfileResourceRef, ProfileScope, validate_profile_name
from ricky.profiles.types import validate_profile_compartment
from ricky.skills.registry import discover_skills
from ricky.tools import ToolRegistry
from ricky.workflows.registry import discover_workflows, find_workflow_bundle
from ricky.workflows.run import WorkflowRun, WorkflowSourceIdentity
from ricky.workflows.run_store import WorkflowRunStore

SCOPE = ProfileScope.create("personal")

_SKILL = "---\nname: {name}\ndescription: {name} skill\n---\nBody.\n"
_WORKFLOW = (
    'version = 2\nname = "{name}"\ndescription = "Bundled."\n'
    '[[steps]]\nid = "done"\nkind = "message"\nmessage = "done"\n'
)
_JOB = """version = 3
name = "{name}"
description = "Bundled job."
provider = "openrouter"
model = "test-model"
goal = "Report."
[budget]
wall_clock_seconds = 10
iterations = 2
max_completion_tokens_per_request = 100
effect_calls = 0
[tools]
allow = []
"""


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings.model_validate({"user_data_dir": str(tmp_path / "user")})


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.real_bundled_resources
def test_bundled_helpers_resolve_below_one_package_root() -> None:
    root = bundled_root()

    assert root.is_dir()
    assert (root / "__init__.py").is_file()
    for resolved in (bundled_skills_dir(), bundled_workflows_dir(), bundled_jobs_dir()):
        assert resolved.parent == root


def test_bundled_owner_is_a_valid_identity_but_never_a_data_compartment() -> None:
    # A bundled resource must be able to carry the qualified identity.
    assert validate_profile_name(BUNDLED_OWNER) == BUNDLED_OWNER
    assert ProfileResourceRef(profile=BUNDLED_OWNER, name="demo").qualified == "bundled/demo"

    # It must never name a profile scope, a label, or a profile data directory.
    with pytest.raises(ValueError, match="reserved"):
        validate_profile_compartment(BUNDLED_OWNER)
    with pytest.raises(ValueError, match="reserved"):
        ProfileScope.create(BUNDLED_OWNER)
    assert ProfileScope.create("personal").includes(BUNDLED_OWNER) is False


@pytest.mark.parametrize("marker", ["pyproject.toml", ".git"])
def test_discovery_ignores_a_project_directory_and_its_markers(
    tmp_path: Path,
    bundled_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker: str,
) -> None:
    """A stray checkout marker must not change which resources exist."""

    project = tmp_path / "checkout"
    if marker == ".git":
        (project / ".git").mkdir(parents=True)
    else:
        _write(project / marker, "[project]\nname='stray'\n")
    _write(project / ".ricky" / "skills" / "stray" / "SKILL.md", _SKILL.format(name="stray"))
    _write(
        project / ".ricky" / "workflows" / "stray" / "workflow.toml", _WORKFLOW.format(name="stray")
    )
    _write(project / ".ricky" / "jobs" / "stray" / "job.toml", _JOB.format(name="stray"))
    # Discovery must not consult the working directory, so stand inside it.
    monkeypatch.chdir(project)
    settings = _settings(tmp_path)

    skills = discover_skills(settings=settings, profile_scope=SCOPE)
    workflows = discover_workflows(
        settings=settings,
        profile_scope=SCOPE,
        skill_names=set(),
        tool_registry=ToolRegistry([]),
    )
    jobs = JobRegistry(settings, profile_scope=SCOPE)

    assert skills.get("stray") is None
    assert workflows.get("stray") is None
    assert jobs.find("stray") is None
    assert find_workflow_bundle("stray", settings=settings, profile_scope=SCOPE) is None


def test_bundled_resources_resolve_without_any_project_directory(
    tmp_path: Path,
    bundled_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    _write(bundled_root / "skills" / "shipped" / "SKILL.md", _SKILL.format(name="shipped"))
    _write(
        bundled_root / "workflows" / "shipped" / "workflow.toml", _WORKFLOW.format(name="shipped")
    )
    _write(bundled_root / "jobs" / "shipped" / "job.toml", _JOB.format(name="shipped"))
    settings = _settings(tmp_path)

    skills = discover_skills(settings=settings, profile_scope=SCOPE)
    workflows = discover_workflows(
        settings=settings,
        profile_scope=SCOPE,
        skill_names=set(),
        tool_registry=ToolRegistry([]),
    )
    loaded_jobs, errors = JobRegistry(settings, profile_scope=SCOPE).discover()

    assert errors == []
    skill = skills.get("shipped")
    assert skill is not None and skill.profile == BUNDLED_OWNER
    assert skills.get("bundled/shipped") is skill
    loaded_workflow = workflows.loaded("shipped")
    assert loaded_workflow is not None
    assert loaded_workflow.resource.qualified == "bundled/shipped"
    assert [item.resource.qualified for item in loaded_jobs] == ["bundled/shipped"]


def test_a_bundled_job_snapshots_under_the_primary_profile(
    tmp_path: Path, bundled_root: Path
) -> None:
    """A bundled job owns no profile root, so its snapshot follows the runtime."""

    _write(bundled_root / "jobs" / "shipped" / "job.toml", _JOB.format(name="shipped"))
    settings = _settings(tmp_path)
    registry = JobRegistry(settings, profile_scope=SCOPE)

    loaded = registry.load("shipped")
    snapshot = registry.snapshot(loaded)

    assert loaded.resource.qualified == "bundled/shipped"
    assert snapshot.is_relative_to(Path(settings.user_data_dir) / "profiles" / "personal")
    assert not snapshot.is_relative_to(bundled_root)
    assert (snapshot / "job.toml").is_file()


async def test_a_checkpoint_written_before_bundled_discovery_still_loads(
    tmp_path: Path,
) -> None:
    """Runs recorded as project-scoped must stay readable and resumable."""

    settings = _settings(tmp_path)
    store = WorkflowRunStore(settings)
    run = WorkflowRun(
        workflow_name="legacy",
        source=WorkflowSourceIdentity(
            path=str(tmp_path / "legacy" / "workflow.toml"),
            scope="project",
            content_digest="a" * 64,
            resource=ProfileResourceRef(profile="personal", name="legacy"),
        ),
        provider="openrouter",
        model="test-model",
        profile_scope=SCOPE,
        storage_scope="project",
        graph_fingerprint="b" * 64,
        cumulative_usage=Usage(),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    await store.save(run)

    reloaded = await store.load(run.id, profile_scope=SCOPE, scope="project")

    assert reloaded.storage_scope == "project"
    assert reloaded.source.scope == "project"


def test_a_new_run_records_the_user_storage_scope(tmp_path: Path) -> None:
    run = WorkflowRun(
        workflow_name="current",
        source=WorkflowSourceIdentity(
            path=str(tmp_path / "current" / "workflow.toml"),
            scope="bundled",
            content_digest="c" * 64,
            resource=ProfileResourceRef(profile=BUNDLED_OWNER, name="current"),
        ),
        provider="openrouter",
        model="test-model",
        profile_scope=SCOPE,
        graph_fingerprint="d" * 64,
    )

    assert run.storage_scope == "user"


@pytest.mark.real_bundled_resources
def test_the_shipped_bundles_are_well_formed_and_match_their_directories() -> None:
    for source in sorted(bundled_skills_dir().glob("*/SKILL.md")):
        body = source.read_text(encoding="utf-8")
        assert body.startswith("---\n"), source
        assert f"name: {source.parent.name}\n" in body, source

    for source in sorted(bundled_workflows_dir().glob("*/workflow.toml")):
        document = tomllib.loads(source.read_text(encoding="utf-8"))
        assert document["version"] == 2, source
        assert document["name"] == source.parent.name, source

    for source in sorted(bundled_jobs_dir().glob("*/job.toml")):
        document = tomllib.loads(source.read_text(encoding="utf-8"))
        assert document["version"] == 3, source
        assert document["name"] == source.parent.name, source


@pytest.mark.real_bundled_resources
def test_the_shipped_authoring_skills_are_discoverable_as_bundled(tmp_path: Path) -> None:
    registry = discover_skills(settings=_settings(tmp_path), profile_scope=SCOPE)

    names = {skill.name for skill in registry.skills()}
    assert {"author-job", "update-job", "author-workflow", "update-workflow"} <= names
    for name in ("author-job", "update-job", "author-workflow", "update-workflow"):
        skill = registry.get(name)
        assert skill is not None
        assert skill.profile == BUNDLED_OWNER
        assert skill.qualified_name == f"bundled/{name}"
