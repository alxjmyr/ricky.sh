"""Execution-contract runtime composition tests."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from pydantic import BaseModel, ConfigDict

from ricky.agent.session import AgentSession
from ricky.capabilities import tool_contract_digest
from ricky.config import RickySettings
from ricky.durable_tasks.artifacts import TaskArtifactStore
from ricky.durable_tasks.store import DurableTaskStore
from ricky.durable_tasks.tools import durable_task_tools
from ricky.jobs.effects import GuardedEffectTool
from ricky.jobs.runner import (
    JobConfigurationError,
    validate_execution_contract_tools,
    validate_pinned_execution_runtime,
)
from ricky.jobs.spec import (
    JobBudget,
    JobPermissions,
    JobSpec,
    JobTools,
    PinnedExecutionRuntime,
)
from ricky.jobs.store import JobRunStore
from ricky.jobs.types import JobRun
from ricky.tools import Tool, ToolContext, ToolRegistry, ToolResult
from ricky.tools.base import EffectIdentity, EffectReceipt


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": ".ricky",
            "google": {"accounts": {}},
            "memory": {"enabled": False},
            "workflow": {"enabled": False},
        }
    )


class _EffectParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    target: str


class _EffectTool:
    name: ClassVar[str] = "test_effect"
    description: ClassVar[str] = "Perform one test external effect."
    Params: ClassVar[type[BaseModel]] = _EffectParams
    risk: ClassVar[str] = "mutating"
    capability_id = None
    effect_kind = "external"
    unattended = "allowed"
    state_guard_id = None

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        del ctx
        target = str(args["target"])
        return EffectIdentity(
            operation="test.effect",
            target=target,
            occurrence=target,
            summary=f"Effect on {target}",
            action_key="a" * 64,
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del params, ctx
        return ToolResult(
            content="performed",
            effect_receipt=EffectReceipt(disposition="performed", provider_reference="effect-1"),
        )


class _NestedEffectTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient: str


class _NestedEffectParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    payload: _NestedEffectTarget


class _NestedEffectTool(_EffectTool):
    name: ClassVar[str] = "nested_effect"
    Params: ClassVar[type[BaseModel]] = _NestedEffectParams

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        del ctx
        payload = cast(dict[str, object], args["payload"])
        recipient = str(payload["recipient"])
        return EffectIdentity(
            operation="test.effect",
            target=recipient,
            occurrence=recipient,
            summary=f"Effect on {recipient}",
            action_key="b" * 64,
        )


async def test_contract_runtime_installs_declared_durable_task_state_guard(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    task_store = await DurableTaskStore.create(settings, profile="personal")
    registry = ToolRegistry(durable_task_tools(task_store, TaskArtifactStore(task_store)))
    tool = registry.get("claim_durable_task")
    assert tool is not None
    spec = JobSpec(
        version=3,
        name="coordinate",
        description="Coordinate one durable task.",
        provider="openrouter",
        model="test-model",
        goal="Advance the task.",
        tools=JobTools(allow=[tool.name]),
        permissions=JobPermissions(allow_mutating=[tool.name]),
    )
    selected = validate_execution_contract_tools(
        registry,
        spec,
        task_store=task_store,
        profile_scope=settings.resolve_profile_scope(),
    )
    assert [item.name for item in selected.tools()] == [tool.name]

    pinned = PinnedExecutionRuntime(
        tool_digests={tool.name: tool_contract_digest(tool)},
        tool_effect_kinds={tool.name: "ricky_state"},
        tool_unattended={tool.name: "allowed"},
        tool_state_guards={tool.name: "durable_task.lease"},
    )
    runtime = SimpleNamespace(
        durable_tasks=task_store,
        capabilities=SimpleNamespace(full_registry=registry),
    )
    validate_pinned_execution_runtime(cast(Any, runtime), pinned)


async def test_pinned_runtime_requires_execution_facts_for_every_tool() -> None:
    with pytest.raises(ValueError, match="must cover every exact tool"):
        PinnedExecutionRuntime(tool_digests={"read_file": "a" * 64})


async def test_contract_authorization_admits_and_guards_exact_mutating_tool(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    task_store = await DurableTaskStore.create(settings, profile="personal")
    spec = JobSpec(
        version=3,
        name="confirmed-effect",
        description="Confirmed task-scoped effect.",
        provider="openrouter",
        model="test-model",
        goal="Perform the confirmed effect.",
        tools=JobTools(allow=["test_effect"]),
        permissions=JobPermissions(allow_mutating=["test_effect"]),
        budget=JobBudget(effect_calls=1),
    )
    registry = ToolRegistry([cast(Tool, _EffectTool())])

    with pytest.raises(JobConfigurationError, match="not authorized"):
        validate_execution_contract_tools(
            registry,
            spec,
            task_store=task_store,
            profile_scope=settings.resolve_profile_scope(),
        )

    store = JobRunStore(settings)
    await store.initialize()
    await store.insert(
        JobRun(
            id="jobrun_confirmed",
            profile_scope=settings.resolve_profile_scope(),
            provider="openrouter",
            model="test-model",
            session_id="session_confirmed",
            started_at=datetime.now(UTC),
        ),
        scope=settings.resolve_profile_scope(),
    )
    selected = validate_execution_contract_tools(
        registry,
        spec,
        task_store=task_store,
        authorized_mutating_tools=frozenset({"test_effect"}),
        store=store,
        run_id="jobrun_confirmed",
        profile_scope=settings.resolve_profile_scope(),
    )

    tool = selected.get("test_effect")
    assert isinstance(tool, GuardedEffectTool)
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="test-model",
    )
    result = await tool.run(
        _EffectParams(target="recipient@example.com"),
        ToolContext(cwd=tmp_path, settings=settings, session=session),
    )
    assert result.effect_receipt is not None
    assert result.effect_receipt.disposition == "performed"
    [action] = await store.list_actions(scope=session.profile_scope)
    assert action.status == "performed"
    assert action.provider_reference == "effect-1"


async def test_effect_identity_receives_registry_canonical_nested_arguments(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = JobRunStore(settings)
    await store.initialize()
    await store.insert(
        JobRun(
            id="jobrun_nested",
            profile_scope=settings.resolve_profile_scope(),
            provider="openrouter",
            model="test-model",
            session_id="session_nested",
            started_at=datetime.now(UTC),
        ),
        scope=settings.resolve_profile_scope(),
    )
    guarded = GuardedEffectTool(
        cast(Tool, _NestedEffectTool()),
        store=store,
        job_name="nested",
        run_id="jobrun_nested",
        profile_scope=settings.resolve_profile_scope(),
        effect_budget=1,
    )
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="openrouter",
        model="test-model",
    )

    result = await ToolRegistry([cast(Tool, guarded)]).dispatch(
        "nested_effect",
        {"payload": '{"recipient":"alex@example.com"}'},
        ToolContext(cwd=tmp_path, settings=settings, session=session),
    )

    assert result.is_error is False
    [action] = await store.actions_for_run(
        "jobrun_nested",
        scope=session.profile_scope,
    )
    assert action.target == "alex@example.com"
    assert action.status == "performed"
