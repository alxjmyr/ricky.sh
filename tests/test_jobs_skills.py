"""Job authoring-skill tests: the bundled skills must match the live contract."""

from __future__ import annotations

import tomllib

import pytest

from ricky.jobs.spec import JobSpec, validate_job_name
from ricky.profiles import ProfileScope
from ricky.skills.registry import Skill, discover_skills

# Every test here asserts against the skills Ricky actually ships.
pytestmark = pytest.mark.real_bundled_resources


def _skill(name: str) -> Skill:
    skill = discover_skills(profile_scope=ProfileScope.create("personal")).get(name)
    assert skill is not None, f"skill '{name}' was not discovered"
    assert skill.name == name
    return skill


def _toml_blocks(body: str, *, after: str) -> list[str]:
    section = body.split(after, 1)
    assert len(section) == 2, f"missing section heading: {after}"
    blocks: list[str] = []
    remainder = section[1]
    while "```toml" in remainder:
        block, remainder = remainder.split("```toml", 1)[1].split("```", 1)
        blocks.append(block.strip())
    return blocks


def test_author_job_skill_uses_the_current_bundle_contract() -> None:
    skill = _skill("author-job")

    assert "version = 3" in skill.body
    assert "ricky job validate" in skill.body
    assert "allow_mutating" in skill.body
    assert "record_item_disposition" in skill.body
    assert "<resolved-user-data-dir>/profiles/<owner>/jobs/<name>" in skill.body
    assert "<owner>/<name> <scope-flags>" in skill.body
    assert "Do not add a `profile` key" in skill.body
    assert "uv run ricky config" in skill.body
    assert "exactly one of `goal` or `INSTRUCTIONS.md`" in skill.body
    assert "create" in skill.description.lower()


@pytest.mark.parametrize("skeleton_index", [0, 1])
def test_author_job_skeletons_validate_as_job_specs(skeleton_index: int) -> None:
    skill = _skill("author-job")
    agent_examples = skill.body.split("## Workflow-backed skeleton", 1)[0]
    blocks = _toml_blocks(agent_examples, after="## Minimal read-only skeleton")
    assert len(blocks) == 2, "author-job must document a read-only and a recurring skeleton"

    spec = JobSpec.model_validate(tomllib.loads(blocks[skeleton_index]))

    assert spec.version == 3
    assert validate_job_name(spec.name) == spec.name
    assert spec.goal is not None
    assert spec.context.lineage == spec.context.revision == 1
    assert set(spec.permissions.allow_mutating).issubset(set(spec.tools.allow))


def test_author_job_recurring_skeleton_declares_accountable_sources() -> None:
    skill = _skill("author-job")
    recurring = _toml_blocks(skill.body, after="## Minimal read-only skeleton")[1]

    spec = JobSpec.model_validate(tomllib.loads(recurring))

    assert [source.adapter for source in spec.stream_sources] == ["slack_channel"]
    assert spec.task_sources and spec.task_sources[0].reconsider_after_hours > 0
    assert spec.budget.effect_calls > 0
    assert "record_item_disposition" not in spec.tools.allow
    assert "record_candidate_disposition" not in spec.tools.allow
    assert "create_durable_task" not in spec.tools.allow
    assert "run_shell" not in spec.tools.allow


def test_author_job_workflow_skeleton_uses_derived_tools() -> None:
    skill = _skill("author-job")
    blocks = _toml_blocks(skill.body, after="## Workflow-backed skeleton")
    assert len(blocks) == 1

    spec = JobSpec.model_validate(tomllib.loads(blocks[0]))

    assert spec.version == 3
    assert spec.workflow is not None
    assert spec.workflow.args == {"account": "personal/personal"}
    assert spec.context.lineage == spec.context.revision == 1
    assert spec.tools.allow == []
    assert spec.permissions.allow_mutating == ["park_for_review"]


def test_update_job_skill_preserves_current_authority_invariants() -> None:
    author = _skill("author-job")
    update = _skill("update-job")

    assert "existing `version = 3` job" in update.body
    assert "ricky job validate <owner>/<name> <scope-flags>` before" in update.body
    assert "context.lineage" in update.body
    assert "context.revision" in update.body
    assert "schedule refresh" in update.body
    assert "validation_required" in update.body
    assert "lineage_required" in update.body
    assert "approval_required" in update.body
    assert "Approval envelope" in update.body
    assert "<user_data_dir>/profiles/<owner>/jobs/" in update.body
    assert "Profile ownership or scope" in update.body
    assert "--dry-run" in update.body
    assert "cursor" in update.body
    assert "create" in author.description.lower()
    assert "existing" in update.description.lower()


def test_workflow_update_skills_cover_referencing_job_lineage_and_schedule_gates() -> None:
    author = _skill("author-workflow")
    update = _skill("update-workflow")

    assert "update-job" in author.body
    assert "context lineage" in author.body
    assert "context.revision" in update.body
    assert "context.lineage" in update.body
    assert "schedule refresh" in update.body
    assert "approval_required" in update.body
