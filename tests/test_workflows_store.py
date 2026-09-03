"""Atomic Workflow run-store tests."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from ricky.agent.session import AgentSession
from ricky.agent.workflow import WorkflowRunner
from ricky.config import RickySettings, WorkflowSettings
from ricky.profiles import ProfileResourceRef, ProfileScope
from ricky.tools import ToolRegistry
from ricky.workflows.compile import compile_workflow
from ricky.workflows.run import (
    EffectJournalEntry,
    StepRecord,
    WorkflowError,
    WorkflowRun,
    WorkflowSourceIdentity,
)
from ricky.workflows.run_store import WorkflowRunStore
from ricky.workflows.spec import WorkflowSpec


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings(
        project_data_dir=str(tmp_path / "project-data"),
        user_data_dir=str(tmp_path / "user-data"),
    )


def _run(*, scope: Literal["project", "user"] = "project") -> WorkflowRun:
    return WorkflowRun(
        profile_scope=ProfileScope.create("personal"),
        id="workflow_abc123",
        workflow_name="synthetic",
        source=WorkflowSourceIdentity(
            resource=ProfileResourceRef(profile="personal", name="fixture"),
            path="/synthetic/workflow.toml",
            scope="fixture",
            content_digest="digest",
        ),
        provider="scripted",
        model="synthetic",
        storage_scope=scope,
        graph_fingerprint="fingerprint",
        steps={
            "effect": StepRecord(
                step_id="effect",
                execution_address="effect",
                kind="tool",
            )
        },
    )


async def test_run_store_round_trip_and_user_permissions(tmp_path: Path) -> None:
    store = WorkflowRunStore(_settings(tmp_path), project_root=tmp_path)
    run = _run(scope="user")

    await store.save(run)
    loaded = await store.load(run.id, profile_scope=run.profile_scope, scope="user")

    assert loaded == run
    assert store.path(run.id, "user").stat().st_mode & 0o777 == 0o600
    assert store.root("user").stat().st_mode & 0o777 == 0o700
    assert store.root("project") == tmp_path / "user-data" / "workflow-runs"
    assert not (tmp_path / "project-data" / "workflow-runs").exists()


async def test_failed_atomic_replace_preserves_prior_checkpoint(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = WorkflowRunStore(settings, project_root=tmp_path)
    run = _run()
    await store.save(run)
    original = store.path(run.id, "project").read_bytes()

    def fail_replace(source: str | Path, target: str | Path) -> None:
        _ = source, target
        raise OSError("injected replace failure")

    failing = WorkflowRunStore(settings, project_root=tmp_path, replace=fail_replace)
    run.status = "running"
    with pytest.raises(OSError, match="injected"):
        await failing.save(run)

    assert store.path(run.id, "project").read_bytes() == original
    assert (await store.load(run.id, profile_scope=run.profile_scope)).status == "pending"


async def test_corrupt_checkpoint_fails_closed(tmp_path: Path) -> None:
    store = WorkflowRunStore(_settings(tmp_path), project_root=tmp_path)
    run = _run()
    await store.save(run)
    store.path(run.id, "project").write_text("{bad", encoding="utf-8")

    with pytest.raises(ValueError, match="corrupt"):
        await store.load(run.id, profile_scope=run.profile_scope)


async def test_reconcile_in_doubt_effect_uses_explicit_user_fact(tmp_path: Path) -> None:
    store = WorkflowRunStore(_settings(tmp_path), project_root=tmp_path)
    run = _run()
    run.status = "in_doubt"
    run.steps["effect"].status = "in_doubt"
    run.steps["effect"].error = WorkflowError(category="in_doubt", message="unknown result")
    run.effect_journal.append(
        EffectJournalEntry(
            step_id="effect",
            execution_address="effect",
            tool_name="synthetic_effect",
            risk="mutating",
            status="in_doubt",
        )
    )
    await store.save(run)

    reconciled = await store.reconcile(
        run.id,
        "effect",
        completed=True,
        profile_scope=run.profile_scope,
    )

    assert reconciled.steps["effect"].status == "completed"
    assert reconciled.effect_journal[0].status == "reconciled"
    assert reconciled.status == "pending"


def test_store_rejects_path_like_run_ids(tmp_path: Path) -> None:
    store = WorkflowRunStore(_settings(tmp_path), project_root=tmp_path)

    with pytest.raises(ValueError, match="invalid"):
        store.path("workflow_../../escape", "project")


async def test_user_workflow_source_uses_user_run_storage(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = WorkflowRunStore(settings, project_root=tmp_path)
    spec = WorkflowSpec.model_validate(
        {
            "version": 2,
            "name": "user-workflow",
            "description": "Test user run storage.",
            "steps": [{"id": "done", "kind": "message", "message": "done"}],
        }
    )
    registry = ToolRegistry([])
    compiled = compile_workflow(spec, tool_registry=registry, settings=settings.workflow)
    assert compiled.graph is not None, compiled.errors
    runner = WorkflowRunner(
        graph=compiled.graph,
        provider=None,
        tool_registry=registry,
        settings=settings,
        session=AgentSession.create(
            settings,
            profile_scope=settings.resolve_profile_scope(),
            provider="openrouter",
            model="synthetic",
        ),
        source=WorkflowSourceIdentity(
            resource=ProfileResourceRef(profile="personal", name="fixture"),
            path="/user/workflow.toml",
            scope="user",
            content_digest="digest",
        ),
        checkpoint=store.save,
    )

    run = await runner.start({})

    assert run.storage_scope == "user"
    assert store.path(run.id, "user").is_file()
    assert store.path(run.id, "project") == store.path(run.id, "user")
    with pytest.raises(ValueError, match="mismatched storage scope"):
        await store.load(
            run.id,
            profile_scope=run.profile_scope,
            scope="project",
        )


@pytest.mark.parametrize("run_dir", ["/tmp/runs", "../runs", ".", "runs/../../out"])
def test_run_directory_cannot_escape_configured_data_root(run_dir: str) -> None:
    with pytest.raises(ValueError, match="must stay below"):
        WorkflowSettings(run_dir=run_dir)


async def test_store_refuses_to_abandon_completed_run(tmp_path: Path) -> None:
    store = WorkflowRunStore(_settings(tmp_path), project_root=tmp_path)
    run = _run()
    run.status = "completed"
    await store.save(run)

    with pytest.raises(ValueError, match="cannot abandon completed"):
        await store.abandon(run.id, profile_scope=run.profile_scope)

    assert (await store.load(run.id, profile_scope=run.profile_scope)).status == "completed"
