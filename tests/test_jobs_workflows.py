"""Workflow-backed job contract and launch-path acceptance."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from pydantic import BaseModel, ConfigDict
from typer.testing import CliRunner

from ricky.agent.events import WorkflowEvent
from ricky.config import ExecutionSettings, MessagingSettings, RickySettings
from ricky.durable_tasks.store import DurableTaskStore
from ricky.executions.dispatcher import ExecutionDispatcher, RunnerFactory
from ricky.interfaces.cli.app import app
from ricky.jobs.registry import JobRegistry, context_definition_digest
from ricky.jobs.runner import (
    JobConfigurationError,
    JobRunner,
    validate_recurring_tool_profile,
)
from ricky.jobs.spec import JobPermissions, JobSpec, JobWorkflow
from ricky.jobs.store import JobRunStore
from ricky.jobs.types import JobRun, RunTrigger
from ricky.llm import CompletionRequest, Message, MessageDone, StreamEvent, TextPart
from ricky.profiles import ProfileScope
from ricky.schedules.service import ScheduleService
from ricky.tool_contracts import EffectKind, Risk, UnattendedUse
from ricky.tools import (
    EffectReceipt,
    ToolContext,
    ToolRegistry,
    ToolResult,
    make_effect_identity,
)
from ricky.workflows.run_store import WorkflowRunStore

SCOPE = ProfileScope.create("personal")


def _recorded_workflow_run(
    name: str,
    *,
    profile_scope: ProfileScope,
    trigger: RunTrigger,
    trigger_id: str | None,
    run_id: str = "jobrun_recorded_workflow",
    spec_digest: str | None = None,
) -> JobRun:
    now = datetime.now(UTC)
    return JobRun(
        id=run_id,
        job_name=name,
        spec_digest=spec_digest,
        provider="openrouter",
        model="test-model",
        profile_scope=profile_scope,
        session_id=f"session_{run_id}",
        outcome="succeeded",
        started_at=now,
        finished_at=now,
        final_message="recorded workflow result",
        trigger=trigger,
        trigger_id=trigger_id,
        workflow_name="personal/triage-fixture",
        workflow_args={
            "account": "personal/personal",
            "query": "is:unread in:inbox",
        },
        workflow_run_id="workflowrun_recorded",
        workflow_status="completed",
    )


class RecordingJobRunner:
    """Record launch-boundary arguments while preserving persisted-run wiring."""

    def __init__(self, settings: RickySettings) -> None:
        self.settings = settings
        self.calls: list[dict[str, Any]] = []

    async def run(self, name: str, **kwargs: Any) -> JobRun:
        self.calls.append({"name": name, **kwargs})
        trigger = cast(RunTrigger, kwargs.get("trigger", "manual"))
        profile_scope = cast(ProfileScope, kwargs["profile_scope"])
        run = _recorded_workflow_run(
            name,
            profile_scope=profile_scope,
            trigger=trigger,
            trigger_id=cast(str | None, kwargs.get("trigger_id")),
            run_id=cast(str, kwargs.get("run_id", "jobrun_recorded_workflow")),
            spec_digest=cast(str | None, kwargs.get("expected_spec_digest")),
        )
        store = JobRunStore(self.settings)
        await store.initialize()
        await store.insert(run, scope=profile_scope)
        return run


class ScriptedProvider:
    name = "scripted"

    def __init__(self, outputs: list[str] | None = None) -> None:
        self.outputs = list(outputs or [])
        self.requests: list[CompletionRequest] = []
        self.closed = False

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        text = self.outputs.pop(0) if self.outputs else "{}"
        yield MessageDone(message=Message.text("assistant", text))

    async def aclose(self) -> None:
        self.closed = True


class SlowProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = False

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if False:
            yield MessageDone(message=Message.text("assistant", "never"))


class _NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _ContractEffectParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    occurrence: str


class _ContractEffectResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    occurrence: str


class ContractEffectTool:
    name: ClassVar[str] = "contract_effect"
    description: ClassVar[str] = "Perform a typed synthetic unattended effect."
    Params: ClassVar[type[BaseModel]] = _ContractEffectParams
    Result: ClassVar[type[BaseModel]] = _ContractEffectResult
    risk: ClassVar[Risk] = "mutating"
    capability_id: ClassVar[str | None] = None
    effect_kind: ClassVar[EffectKind] = "external"
    unattended: ClassVar[UnattendedUse] = "allowed"
    state_guard_id: ClassVar[str | None] = None
    idempotent_replay: ClassVar[bool] = True

    def __init__(self) -> None:
        self.calls = 0

    @staticmethod
    def idempotency_key(args: dict[str, object]) -> str:
        return f"contract-effect:{args['occurrence']}"

    def effect_identity(self, args: dict[str, object], ctx: ToolContext):
        del ctx
        occurrence = str(args["occurrence"])
        return make_effect_identity(
            operation=self.name,
            target="synthetic-target",
            occurrence=occurrence,
            summary="Perform the typed synthetic effect",
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        parsed = _ContractEffectParams.model_validate(params)
        self.calls += 1
        return ToolResult(
            content="performed typed synthetic effect",
            data={"occurrence": parsed.occurrence},
            effect_receipt=EffectReceipt(
                disposition="performed",
                provider_reference=f"synthetic:{parsed.occurrence}",
            ),
        )


class GuardedStateContractTool:
    name: ClassVar[str] = "guarded_state_contract"
    description: ClassVar[str] = "Expose a typed synthetic guarded state contract."
    Params: ClassVar[type[BaseModel]] = _NoParams
    Result: ClassVar[type[BaseModel]] = _ContractEffectResult
    risk: ClassVar[Risk] = "mutating"
    capability_id: ClassVar[str | None] = None
    effect_kind: ClassVar[EffectKind] = "ricky_state"
    unattended: ClassVar[UnattendedUse] = "allowed"
    state_guard_id: ClassVar[str | None] = "durable_task.lease"
    idempotent_replay: ClassVar[bool] = True

    @staticmethod
    def idempotency_key(args: dict[str, object]) -> str:
        del args
        return "guarded-state-contract"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del params, ctx
        return ToolResult(content="unused")


class DestructiveStateTool:
    name: ClassVar[str] = "erase_test_state"
    description: ClassVar[str] = "Erase synthetic state under an exact unattended contract."
    Params: ClassVar[type[BaseModel]] = _NoParams
    risk: ClassVar[Risk] = "destructive"
    capability_id: ClassVar[str | None] = None
    effect_kind: ClassVar[EffectKind] = "ricky_state"
    unattended: ClassVar[UnattendedUse] = "allowed"
    state_guard_id: ClassVar[str | None] = None

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del params, ctx
        return ToolResult(content="erased")


class ForbiddenDestructiveStateTool(DestructiveStateTool):
    name: ClassVar[str] = "forbidden_erase"
    unattended: ClassVar[UnattendedUse] = "forbidden"


def _settings(tmp_path: Path, *, messaging: bool = False) -> RickySettings:
    values: dict[str, object] = {
        "user_data_dir": str(tmp_path / "user"),
        "project_data_dir": ".ricky",
        "default_provider": "openrouter",
        "providers": {"openrouter": {"default_model": "test-model"}},
        "workflow": {"enabled": True, "model_attempts": 2},
        "memory": {"enabled": False},
        "google": {"accounts": {}},
        "google_oauth_clients": {},
        "executions": ExecutionSettings(
            claim_seconds=10,
            heartbeat_seconds=0.05,
            concurrency=1,
            poll_seconds=0.01,
        ),
    }
    if messaging:
        values["messaging"] = MessagingSettings.model_validate(
            {
                "telegram_accounts": {
                    "personal/owner-bot": {"bot_token": "test-token"},
                },
                "transports": {
                    "main": {"type": "telegram", "account": "personal/owner-bot"},
                },
                "routes": {
                    "owner": {
                        "transport": "main",
                        "destination": "chat-owner",
                        "owner_profile": "personal",
                        "accepted_profiles": ["shared", "personal"],
                    }
                },
                "agent_routes": ["owner"],
            }
        )
    return RickySettings.model_validate(values)


def _project(
    tmp_path: Path,
    *,
    locked_query: str | None = "is:unread in:inbox",
    approval: bool = False,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='workflow-job-test'\n")
    workflow = tmp_path / "user" / "profiles" / "personal" / "workflows" / "triage-fixture"
    workflow.mkdir(parents=True, exist_ok=True)
    step = (
        """[[steps]]
id = "review"
kind = "approval"
mode = "confirm"
prompt = "Approve fixture work."
proposal = { account = { ref = "trigger.account" } }
"""
        if approval
        else """[[steps]]
id = "summary"
kind = "message"
        message = { ref = "trigger.query" }
"""
    )
    (workflow / "workflow.toml").write_text(
        """version = 2
name = "triage-fixture"
description = "Run deterministic fixture triage."

[args.account]
type = "string"
description = "Exact account resource."

[args.query]
type = "string"
description = "Search query derived from the job goal."
default = "is:unread"

"""
        + step,
        encoding="utf-8",
    )
    job = tmp_path / "user" / "profiles" / "personal" / "jobs" / "workflow-triage"
    job.mkdir(parents=True, exist_ok=True)
    query = f"query = {json.dumps(locked_query)}\n" if locked_query is not None else ""
    (job / "job.toml").write_text(
        f"""version = 3
name = "workflow-triage"
description = "Triage mail through a workflow."
provider = "openrouter"
model = "test-model"
goal = "Triage unread personal mail since the last successful execution."

[budget]
wall_clock_seconds = 10
iterations = 2
max_completion_tokens_per_request = 100
effect_calls = 0

[workflow]
name = "triage-fixture"

[workflow.args]
account = "personal/personal"
{query}""",
        encoding="utf-8",
    )


def _effect_project(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='workflow-effect-test'\n")
    workflow = tmp_path / "user" / "profiles" / "personal" / "workflows" / "effect-fixture"
    workflow.mkdir(parents=True, exist_ok=True)
    (workflow / "workflow.toml").write_text(
        """version = 2
name = "effect-fixture"
description = "Exercise an exact destructive unattended effect."

[[steps]]
id = "run"
kind = "tool"
tool = "run_shell"
expose_output = false
args = { command = "true" }
""",
        encoding="utf-8",
    )
    job = tmp_path / "user" / "profiles" / "personal" / "jobs" / "workflow-effect"
    job.mkdir(parents=True, exist_ok=True)
    (job / "job.toml").write_text(
        """version = 3
name = "workflow-effect"
description = "Run one destructive workflow effect."
provider = "openrouter"
model = "test-model"
goal = "Run the exact supplied workflow unattended."

[budget]
wall_clock_seconds = 10
iterations = 2
max_completion_tokens_per_request = 100
effect_calls = 1

[workflow]
name = "effect-fixture"

[permissions]
allow_mutating = ["run_shell"]
""",
        encoding="utf-8",
    )


def _contract_effect_project(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='contract-effect-test'\n")
    workflow = tmp_path / "user" / "profiles" / "personal" / "workflows" / "contract-effect-fixture"
    workflow.mkdir(parents=True, exist_ok=True)
    (workflow / "workflow.toml").write_text(
        """version = 2
name = "contract-effect-fixture"
description = "Exercise a guarded effect's complete typed workflow contract."

[[steps]]
id = "mutate"
kind = "tool"
tool = "contract_effect"
args = { occurrence = "one" }
retry = { max_attempts = 2, on = ["tool_error"] }
""",
        encoding="utf-8",
    )
    job = tmp_path / "user" / "profiles" / "personal" / "jobs" / "contract-effect-job"
    job.mkdir(parents=True, exist_ok=True)
    (job / "job.toml").write_text(
        """version = 3
name = "contract-effect-job"
description = "Run one typed guarded workflow effect."
provider = "openrouter"
model = "test-model"
goal = "Run the exact supplied typed-effect workflow unattended."

[budget]
wall_clock_seconds = 10
iterations = 2
max_completion_tokens_per_request = 100
effect_calls = 1

[workflow]
name = "contract-effect-fixture"

[permissions]
allow_mutating = ["contract_effect"]
""",
        encoding="utf-8",
    )


def _slow_model_project(tmp_path: Path, *, wall: float = 10) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='slow-workflow-test'\n")
    workflow = tmp_path / "user" / "profiles" / "personal" / "workflows" / "slow-fixture"
    workflow.mkdir(parents=True, exist_ok=True)
    (workflow / "workflow.toml").write_text(
        """version = 2
name = "slow-fixture"
description = "Wait in one isolated model step."

[schemas.result]
type = "object"
required = ["text"]
[schemas.result.properties.text]
type = "string"

[[steps]]
id = "wait"
kind = "model"
instruction = "Return the result."
inputs = {}
result_schema = "result"
""",
        encoding="utf-8",
    )
    job = tmp_path / "user" / "profiles" / "personal" / "jobs" / "slow-workflow"
    job.mkdir(parents=True, exist_ok=True)
    (job / "job.toml").write_text(
        f"""version = 3
name = "slow-workflow"
description = "Run a slow workflow."
provider = "openrouter"
model = "test-model"
goal = "Run the slow supplied workflow."

[budget]
wall_clock_seconds = {wall}
iterations = 2
max_completion_tokens_per_request = 100
effect_calls = 0

[workflow]
name = "slow-fixture"
""",
        encoding="utf-8",
    )


def test_workflow_job_spec_locks_authored_args_and_derives_tools() -> None:
    spec = JobSpec(
        version=3,
        name="triage",
        description="Triage through a workflow.",
        goal="Triage mail.",
        workflow=JobWorkflow(name="personal/email-triage", args={"limit": 3}),
    )

    assert spec.workflow is not None
    assert spec.workflow.args == {"limit": 3}
    with pytest.raises(ValueError, match="tools.allow must be empty"):
        JobSpec.model_validate(
            spec.model_dump(mode="json") | {"tools": {"allow": ["gmail_search"]}}
        )


async def test_all_static_args_run_workflow_without_resolver_call(tmp_path: Path) -> None:
    _project(tmp_path)
    settings = _settings(tmp_path)
    provider = ScriptedProvider()
    loaded = JobRegistry(settings, profile_scope=SCOPE).load("workflow-triage")
    files = dict(loaded.source_files)
    assert files["workflow.identity"] == b"personal/triage-fixture"
    changed_identity = replace(
        loaded,
        source_files=tuple(
            (name, b"work/triage-fixture" if name == "workflow.identity" else content)
            for name, content in loaded.source_files
        ),
    )
    assert context_definition_digest(changed_identity) != context_definition_digest(loaded)

    run = await JobRunner(settings, project_root=tmp_path).run(
        "workflow-triage",
        profile_scope=SCOPE,
        provider=provider,
    )

    assert run.outcome == "succeeded", run.error
    assert provider.requests == []
    assert run.workflow_name == "personal/triage-fixture"
    assert run.workflow_run_id is not None
    assert run.workflow_status == "completed"
    assert run.workflow_args == {
        "account": "personal/personal",
        "query": "is:unread in:inbox",
    }
    assert "is:unread in:inbox" in (run.final_message or "")
    stored = await JobRunStore(settings).get(run.id, scope=SCOPE)
    assert stored.workflow_run_id == run.workflow_run_id
    workflow = await WorkflowRunStore(settings).load(
        run.workflow_run_id,
        profile_scope=SCOPE,
        scope="user",
    )
    assert workflow.status == "completed"


async def test_resolver_populates_only_omitted_args_from_goal_and_prior_run(
    tmp_path: Path,
) -> None:
    _project(tmp_path, locked_query=None)
    settings = _settings(tmp_path)
    first_provider = ScriptedProvider(['{"query":"is:unread after:2026/08/23"}'])
    first = await JobRunner(settings, project_root=tmp_path).run(
        "workflow-triage", profile_scope=SCOPE, provider=first_provider
    )
    second_provider = ScriptedProvider(['{"query":"is:unread after:2026/08/24"}'])
    second = await JobRunner(settings, project_root=tmp_path).run(
        "workflow-triage", profile_scope=SCOPE, provider=second_provider
    )

    assert first.outcome == second.outcome == "succeeded"
    assert second.workflow_args == {
        "account": "personal/personal",
        "query": "is:unread after:2026/08/24",
    }
    request_text = "\n".join(
        part.text
        for message in second_provider.requests[0].messages
        for part in message.content
        if isinstance(part, TextPart)
    )
    assert '"locked_args":{"account":"personal/personal"}' in request_text
    assert first.id in request_text
    assert '"properties":{"query"' in request_text
    assert '"properties":{"account"' not in request_text


async def test_workflow_resolver_never_uses_dry_run_as_live_prior_success(
    tmp_path: Path,
) -> None:
    _project(tmp_path, locked_query=None)
    settings = _settings(tmp_path)
    dry_provider = ScriptedProvider(['{"query":"dry-only-query"}'])
    dry = await JobRunner(settings, project_root=tmp_path).run(
        "workflow-triage",
        profile_scope=SCOPE,
        provider=dry_provider,
        dry_run=True,
    )
    live_provider = ScriptedProvider(['{"query":"live-query"}'])
    live = await JobRunner(settings, project_root=tmp_path).run(
        "workflow-triage", profile_scope=SCOPE, provider=live_provider
    )

    assert dry.outcome == live.outcome == "succeeded"
    request_text = "\n".join(
        part.text
        for message in live_provider.requests[0].messages
        for part in message.content
        if isinstance(part, TextPart)
    )
    assert dry.id not in request_text
    assert "dry-only-query" not in request_text


async def test_invalid_resolver_output_fails_before_workflow_run(tmp_path: Path) -> None:
    _project(tmp_path, locked_query=None)
    settings = _settings(tmp_path)
    provider = ScriptedProvider(
        [
            '{"account":"work/work","query":"is:unread"}',
            '{"account":"work/work","query":"is:unread"}',
        ]
    )

    run = await JobRunner(settings, project_root=tmp_path).run(
        "workflow-triage", profile_scope=SCOPE, provider=provider
    )

    assert run.outcome == "failed"
    assert "argument resolution failed" in (run.error or "")
    assert run.workflow_run_id is None
    assert len(provider.requests) == 2


async def test_interactive_approval_step_is_rejected_for_unattended_job(
    tmp_path: Path,
) -> None:
    _project(tmp_path, approval=True)
    settings = _settings(tmp_path)

    with pytest.raises(JobConfigurationError, match="interactive approval"):
        await JobRunner(settings, project_root=tmp_path).validate(
            "workflow-triage", profile_scope=SCOPE
        )


async def test_destructive_tool_is_allowed_when_tool_contract_allows_unattended(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    task_store = await DurableTaskStore.create(settings, profile="personal")
    spec = JobSpec(
        version=3,
        name="destructive-workflow",
        description="Exercise destructive unattended policy.",
        goal="Erase exact fixture state.",
        workflow=JobWorkflow(name="fixture"),
        permissions=JobPermissions(allow_mutating=["erase_test_state"]),
    )
    selected = validate_recurring_tool_profile(
        ToolRegistry([DestructiveStateTool()]),
        spec,
        store=JobRunStore(settings),
        task_store=task_store,
        run_id="jobrun_test",
        dry_run=False,
        profile_scope=SCOPE,
        tool_names=("erase_test_state",),
    )
    assert [tool.name for tool in selected.tools()] == ["erase_test_state"]

    with pytest.raises(JobConfigurationError, match="missing from allow_mutating"):
        validate_recurring_tool_profile(
            ToolRegistry([DestructiveStateTool()]),
            spec.model_copy(update={"permissions": JobPermissions()}),
            store=JobRunStore(settings),
            task_store=task_store,
            run_id="jobrun_test",
            dry_run=False,
            profile_scope=SCOPE,
            tool_names=("erase_test_state",),
        )

    with pytest.raises(JobConfigurationError, match="never available unattended"):
        validate_recurring_tool_profile(
            ToolRegistry([ForbiddenDestructiveStateTool()]),
            spec.model_copy(
                update={
                    "permissions": JobPermissions(allow_mutating=["forbidden_erase"]),
                }
            ),
            store=JobRunStore(settings),
            task_store=task_store,
            run_id="jobrun_test",
            dry_run=False,
            profile_scope=SCOPE,
            tool_names=("forbidden_erase",),
        )


async def test_destructive_workflow_effect_uses_job_ledger_and_exact_authority(
    tmp_path: Path,
) -> None:
    _effect_project(tmp_path)
    settings = _settings(tmp_path)

    run = await JobRunner(settings, project_root=tmp_path).run(
        "workflow-effect",
        profile_scope=SCOPE,
        provider=ScriptedProvider(),
    )

    assert run.outcome == "succeeded", run.error
    assert run.workflow_status == "completed"
    assert run.effect_calls == 1
    [action] = await JobRunStore(settings).actions_for_run(run.id, scope=SCOPE)
    assert action.operation == "run_shell"
    assert action.status == "performed"


async def test_live_workflow_job_preserves_guarded_effect_tool_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = ContractEffectTool()
    monkeypatch.setattr("ricky.runtime.composition.builtin_tools", lambda: [tool])
    _contract_effect_project(tmp_path)
    settings = _settings(tmp_path)

    run = await JobRunner(settings, project_root=tmp_path).run(
        "contract-effect-job",
        profile_scope=SCOPE,
        provider=ScriptedProvider(),
    )

    assert run.outcome == "succeeded", run.error
    assert run.workflow_status == "completed"
    assert run.effect_calls == 1
    assert tool.calls == 1
    [action] = await JobRunStore(settings).actions_for_run(run.id, scope=SCOPE)
    assert action.operation == "contract_effect"
    assert action.status == "performed"


async def test_state_guard_preserves_optional_tool_contract(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    task_store = await DurableTaskStore.create(settings, profile="personal")
    spec = JobSpec(
        version=3,
        name="guarded-state-workflow",
        description="Exercise guarded state tool contract transparency.",
        goal="Run the guarded state fixture.",
        workflow=JobWorkflow(name="fixture"),
        permissions=JobPermissions(allow_mutating=["guarded_state_contract"]),
    )

    selected = validate_recurring_tool_profile(
        ToolRegistry([GuardedStateContractTool()]),
        spec,
        store=JobRunStore(settings),
        task_store=task_store,
        run_id="jobrun_test",
        dry_run=False,
        profile_scope=SCOPE,
        tool_names=("guarded_state_contract",),
    )
    guarded = selected.get("guarded_state_contract")

    assert guarded is not None
    assert getattr(guarded, "Result", None) is _ContractEffectResult
    assert getattr(guarded, "idempotent_replay", False) is True
    assert guarded.idempotency_key({}) == "guarded-state-contract"  # type: ignore[attr-defined]


async def test_workflow_job_dry_run_records_no_job_effect(tmp_path: Path) -> None:
    _effect_project(tmp_path)
    settings = _settings(tmp_path)

    run = await JobRunner(settings, project_root=tmp_path).run(
        "workflow-effect",
        profile_scope=SCOPE,
        provider=ScriptedProvider(),
        dry_run=True,
    )

    assert run.outcome == "succeeded"
    assert run.workflow_status == "completed"
    assert run.effect_calls == 0
    assert await JobRunStore(settings).actions_for_run(run.id, scope=SCOPE) == []


async def test_cancellation_joins_workflow_and_persists_interrupted_link(
    tmp_path: Path,
) -> None:
    _slow_model_project(tmp_path)
    settings = _settings(tmp_path)
    provider = SlowProvider()
    task = asyncio.create_task(
        JobRunner(settings, project_root=tmp_path).run(
            "slow-workflow",
            profile_scope=SCOPE,
            provider=provider,
        )
    )
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    task.cancel()

    run = await asyncio.wait_for(task, timeout=2)

    assert run.outcome == "interrupted"
    assert run.workflow_run_id is not None
    assert run.workflow_status == "interrupted"
    assert provider.cancelled


async def test_workflow_job_settles_checkpoint_when_event_sink_fails(tmp_path: Path) -> None:
    _slow_model_project(tmp_path)
    settings = _settings(tmp_path)
    provider = SlowProvider()

    async def fail_after_run_identity(event: object) -> None:
        if isinstance(event, WorkflowEvent) and event.action == "step_started":
            await asyncio.wait_for(provider.started.wait(), timeout=2)
            raise RuntimeError("synthetic event sink failure")

    run = await JobRunner(
        settings,
        project_root=tmp_path,
        event_sink=fail_after_run_identity,
    ).run(
        "slow-workflow",
        profile_scope=SCOPE,
        provider=provider,
    )

    assert run.outcome == "failed"
    assert run.workflow_run_id is not None
    assert run.workflow_status == "interrupted"
    workflow = await WorkflowRunStore(settings).load(
        run.workflow_run_id,
        profile_scope=SCOPE,
        scope="user",
    )
    assert workflow.status == "interrupted"
    assert provider.cancelled


async def test_wall_clock_timeout_interrupts_owned_workflow(tmp_path: Path) -> None:
    _slow_model_project(tmp_path, wall=0.05)
    settings = _settings(tmp_path)
    provider = SlowProvider()

    run = await JobRunner(settings, project_root=tmp_path).run(
        "slow-workflow",
        profile_scope=SCOPE,
        provider=provider,
    )

    assert run.outcome == "budget_exceeded"
    assert run.workflow_run_id is not None
    assert run.workflow_status == "interrupted"
    assert provider.cancelled


async def test_schedule_invocation_pins_workflow_job_identity_and_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path)
    settings = _settings(tmp_path)
    user_root = Path(settings.user_data_dir)
    user_root.mkdir(parents=True, exist_ok=True)
    (user_root / "ricky.toml").write_text(
        """default_provider = "openrouter"
[providers.openrouter]
default_model = "test-model"
[workflow]
enabled = true
[memory]
enabled = false
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("RICKY_USER_DATA_DIR", str(user_root))
    service = ScheduleService(settings, profile_scope=SCOPE, project_root=tmp_path)
    schedule = await service.create(
        "workflow-triage",
        "0 7 * * *",
        profile_scope=SCOPE,
    )
    calls: list[dict[str, Any]] = []

    async def record_run(_runner: JobRunner, name: str, **kwargs: Any) -> JobRun:
        calls.append({"name": name, **kwargs})
        return _recorded_workflow_run(
            name,
            profile_scope=kwargs["profile_scope"],
            trigger=kwargs["trigger"],
            trigger_id=kwargs["trigger_id"],
            spec_digest=kwargs["expected_spec_digest"],
        )

    monkeypatch.setattr(JobRunner, "run", record_run)

    run = await service.invoke(schedule.id)

    assert calls == [
        {
            "name": "personal/workflow-triage",
            "trigger": "schedule",
            "trigger_id": schedule.id,
            "expected_spec_digest": schedule.approved_spec_digest,
            "expected_runtime_policy_digest": schedule.approved_runtime_policy_digest,
            "profile_scope": SCOPE,
        }
    ]
    assert run.trigger == "schedule" and run.trigger_id == schedule.id
    assert run.workflow_args == {
        "account": "personal/personal",
        "query": "is:unread in:inbox",
    }


async def test_workflow_bundle_change_requires_validation_without_authority_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path)
    settings = _settings(tmp_path)
    user_root = Path(settings.user_data_dir)
    user_root.mkdir(parents=True, exist_ok=True)
    (user_root / "ricky.toml").write_text(
        """default_provider = "openrouter"
[providers.openrouter]
default_model = "test-model"
[workflow]
enabled = true
[memory]
enabled = false
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("RICKY_USER_DATA_DIR", str(user_root))
    service = ScheduleService(settings, profile_scope=SCOPE, project_root=tmp_path)
    schedule = await service.create(
        "workflow-triage",
        "0 7 * * *",
        profile_scope=SCOPE,
    )
    source = (
        tmp_path / "user" / "profiles" / "personal" / "workflows" / "triage-fixture"
    ) / "workflow.toml"
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "Run deterministic fixture triage.",
            "Run changed deterministic fixture triage.",
        ),
        encoding="utf-8",
    )

    inspection = await service.show(schedule.id)

    assert inspection.state == "validation_required"
    assert inspection.current_spec_digest != schedule.approved_spec_digest
    before, refreshed, _ = await service.refresh(schedule.id)
    assert before.state == "validation_required"
    assert refreshed.approved_authority == schedule.approved_authority
    assert (await service.show(schedule.id)).state == "ready"


async def test_gateway_named_execution_wires_pinned_workflow_job_to_runner(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    settings = _settings(tmp_path, messaging=True)
    provider = ScriptedProvider()
    runner = RecordingJobRunner(settings)
    dispatcher = ExecutionDispatcher(
        settings,
        project_root=tmp_path,
        runner_factory=cast(RunnerFactory, lambda: runner),
        provider_factory=lambda _: provider,
    )
    request = await dispatcher.start_named_job(
        "workflow-triage",
        notification_route="owner",
        request_key="gateway-workflow-job",
        profile_scope=SCOPE,
    )

    [terminal] = await dispatcher.worker_once(scope=SCOPE)

    assert terminal.id == request.id
    assert terminal.status == "succeeded"
    assert terminal.run_id is not None
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["name"] == "personal/workflow-triage"
    assert call["provider"] is provider
    assert call["trigger"] == "execution"
    assert call["trigger_id"] == request.id
    assert call["expected_spec_digest"] == request.job_digest
    assert call["run_id"] == terminal.run_id
    assert call["profile_scope"] == SCOPE
    assert call["system_sections"] == {
        "execution": (f"Execution request: {request.id}\nDo not infer foreground history.")
    }
    run = await JobRunStore(settings).get(terminal.run_id, scope=SCOPE)
    assert run.trigger == "execution"
    assert run.workflow_args == {
        "account": "personal/personal",
        "query": "is:unread in:inbox",
    }
    assert run.workflow_status == "completed"


def test_cli_job_run_wires_exact_workflow_job_args_to_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project(tmp_path)
    user_root = tmp_path / "user"
    user_root.mkdir(exist_ok=True)
    (user_root / "ricky.toml").write_text(
        """default_provider = "openrouter"
[providers.openrouter]
default_model = "test-model"
[workflow]
enabled = true
[memory]
enabled = false
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RICKY_USER_DATA_DIR", str(user_root))
    runner = RecordingJobRunner(_settings(tmp_path))
    monkeypatch.setattr(
        "ricky.interfaces.cli.jobs.JobRunner",
        lambda _settings, **_kwargs: runner,
    )

    result = CliRunner().invoke(app, ["job", "run", "workflow-triage"])

    assert result.exit_code == 0, result.output
    assert "succeeded" in result.output
    assert "recorded workflow result" in result.output
    assert runner.calls == [
        {
            "name": "workflow-triage",
            "profile_scope": SCOPE,
            "dry_run": False,
        }
    ]
