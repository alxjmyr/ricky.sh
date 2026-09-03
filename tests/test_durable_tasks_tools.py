"""Agent tool, authority, policy, and session-snapshot tests."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from rich.console import Console

from ricky.agent.session import AgentSession
from ricky.config import RickySettings
from ricky.durable_tasks.artifacts import TaskArtifactStore
from ricky.durable_tasks.scoped import ScopedDurableTaskStore
from ricky.durable_tasks.store import DurableTaskStore
from ricky.durable_tasks.tools import (
    COORDINATION_TOOL_NAMES,
    TaskResult,
    durable_task_policy,
    durable_task_tools,
)
from ricky.interfaces.cli.chat import ChatController
from ricky.interfaces.cli.render import CliRenderer
from ricky.permissions import PermissionEngine, Policy, PolicyRule
from ricky.tools import ToolContext, ToolRegistry


async def _context(tmp_path: Path):
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    store = await DurableTaskStore.create(settings, profile="personal")
    artifacts = TaskArtifactStore(store)
    registry = ToolRegistry(durable_task_tools(store, artifacts))
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="synthetic",
    )
    return store, registry, ToolContext(cwd=tmp_path, settings=settings, session=session)


async def test_tools_create_claim_complete_user_owned_task_on_direct_instruction(
    tmp_path: Path,
) -> None:
    store, registry, ctx = await _context(tmp_path)
    created = await registry.dispatch(
        "create_durable_task",
        {
            "title": "Submit reimbursement",
            "objective": "Submit the reimbursement form",
            "closure_criteria": "A receipt is recorded",
            "execution_mode": "user",
        },
        ctx,
    )
    assert not created.is_error
    task_id = TaskResult.model_validate(created.data).task.id
    claimed = await registry.dispatch("claim_durable_task", {"task_id": task_id}, ctx)
    completed = await registry.dispatch(
        "complete_durable_task",
        {"task_id": task_id, "completion_summary": "Alex reported it submitted"},
        ctx,
    )
    assert not claimed.is_error
    assert not completed.is_error
    assert task_id not in ctx.session.active_task_leases
    activity = await store.activities(task_id)
    assert {item.authority for item in activity} == {"direct_user_instruction"}


async def test_coordination_is_allowed_but_artifact_writes_still_ask(tmp_path: Path) -> None:
    _, registry, ctx = await _context(tmp_path)
    engine = PermissionEngine(durable_task_policy())
    for name in COORDINATION_TOOL_NAMES:
        tool = registry.get(name)
        assert tool is not None
        assert (
            engine.decide(ctx.session, tool_name=name, risk=tool.risk, params={}).decision
            == "allow"
        )
    artifact_tool = registry.get("write_task_artifact")
    assert artifact_tool is not None
    assert (
        engine.decide(
            ctx.session,
            tool_name=artifact_tool.name,
            risk=artifact_tool.risk,
            params={"task_id": "task_x", "path": "draft.md"},
        ).decision
        == "ask"
    )


def test_explicit_deny_caps_default_coordination_allow() -> None:
    base = Policy(
        rules=[
            PolicyRule(
                tool_name="complete_durable_task",
                decision="deny",
                reason="locked by user policy",
            )
        ]
    )
    engine = PermissionEngine(durable_task_policy(base))
    session = AgentSession(
        provider="openrouter",
        model="synthetic",
        profile_scope=RickySettings().resolve_profile_scope(),
    )
    result = engine.decide(
        session,
        tool_name="complete_durable_task",
        risk="mutating",
        params={"task_id": "task_x"},
    )
    assert result.decision == "deny"
    assert result.reason == "locked by user policy"


async def test_agent_tools_are_fixed_to_the_session_profile(tmp_path: Path) -> None:
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    work = await DurableTaskStore.create(settings, profile="work")
    work_task = await work.create_task(
        title="Confidential work",
        objective="Keep work in the work profile",
        closure_criteria="Done",
        execution_mode="agent",
        authority="agent_autonomy",
        executor_id="work",
    )
    personal = await DurableTaskStore.create(settings, profile="personal")
    registry = ToolRegistry(durable_task_tools(personal, TaskArtifactStore(personal)))
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="synthetic",
    )
    result = await registry.dispatch(
        "read_durable_task",
        {"task_id": work_task.id},
        ToolContext(cwd=tmp_path, settings=settings, session=session),
    )
    assert result.is_error
    assert work_task.title not in result.content


async def test_artifact_tools_return_copy_ready_logical_attachment_reference(
    tmp_path: Path,
) -> None:
    store, registry, ctx = await _context(tmp_path)
    task = await store.create_task(
        title="Values map",
        objective="Keep the values map",
        closure_criteria="Map is retained",
        execution_mode="agent",
        authority="agent_autonomy",
        executor_id=ctx.session.id,
    )
    artifact = store.artifact_root / task.id / "personal_values_force_map_v2.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("<html>values</html>")

    listed = await registry.dispatch(
        "list_task_artifacts",
        {"task_id": task.id},
        ctx,
    )
    detail = await registry.dispatch(
        "read_durable_task",
        {"task_id": task.id},
        ctx,
    )
    expected = (
        f'{{"task_id":"{task.id}",'
        '"task_artifact_path":"personal_values_force_map_v2.html",'
        '"profile":"personal"}'
    )

    assert not listed.is_error
    assert expected in listed.content
    assert "never construct a storage path" in listed.content
    assert str(store.artifact_root) not in listed.content
    assert expected in detail.content


async def test_clear_releases_old_session_leases(tmp_path: Path) -> None:
    settings = RickySettings(user_data_dir=str(tmp_path / "user"))
    store = await DurableTaskStore.create(settings, profile="personal")
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="synthetic",
    )
    task = await store.create_task(
        title="Clear probe",
        objective="Release on session replacement",
        closure_criteria="The lease is gone",
        execution_mode="agent",
        authority="agent_autonomy",
        executor_id=session.id,
    )
    claimed = await store.claim(
        task.id,
        holder_session_id=session.id,
        authority="agent_autonomy",
        executor_id=session.id,
    )
    assert claimed.lease is not None
    session.active_task_leases[task.id] = claimed.lease
    output = StringIO()
    scoped_store = await ScopedDurableTaskStore.create(
        settings,
        scope=session.profile_scope,
    )
    controller = ChatController(
        agent_loop=None,  # type: ignore[arg-type]
        session=session,
        settings=settings,
        renderer=CliRenderer(console=Console(file=output, force_terminal=False, color_system=None)),
        skill_registry=None,  # type: ignore[arg-type]
        durable_tasks=scoped_store,
    )

    assert await controller._handle_slash_command("/clear")
    assert controller.session.id != session.id
    assert (await store.get_task(task.id)).lease is None
