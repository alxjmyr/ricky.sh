"""Offline one-shot job runner and CLI acceptance tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict
from typer.testing import CliRunner

from ricky.config import RickySettings
from ricky.interfaces.cli.app import app
from ricky.jobs.lock import job_lock
from ricky.jobs.runner import JobConfigurationError, JobRunner
from ricky.jobs.store import JobRunStore
from ricky.llm import (
    CompletionRequest,
    Message,
    MessageDone,
    StreamEvent,
    TextPart,
    ToolCallPart,
    Usage,
)
from ricky.profiles import ProfileScope
from ricky.tools import ToolContext, ToolResult

SCOPE = ProfileScope.create("personal")


class ScriptedProvider:
    name = "scripted"

    def __init__(self, scripts: list[list[StreamEvent | BaseException]]) -> None:
        self.scripts = list(scripts)
        self.requests: list[CompletionRequest] = []
        self.closed = False

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        script = self.scripts.pop(0)
        for event in script:
            if isinstance(event, BaseException):
                raise event
            yield event

    async def aclose(self) -> None:
        self.closed = True


class SlowProvider:
    name = "slow"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.closed = False
        self.requests: list[CompletionRequest] = []

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

    async def aclose(self) -> None:
        self.closed = True


class OrderedProvider(ScriptedProvider):
    def __init__(self, order: list[str], *, fail_close: bool = False) -> None:
        super().__init__([[_done()]])
        self.order = order
        self.fail_close = fail_close

    async def aclose(self) -> None:
        self.order.append("provider_closed")
        self.closed = True
        if self.fail_close:
            raise RuntimeError("close exploded")


class OrderedStore(JobRunStore):
    def __init__(self, settings: RickySettings, order: list[str]) -> None:
        super().__init__(settings)
        self.order = order

    async def finish(self, run, *, scope):
        probe = job_lock(self.root, "personal/brief")
        assert not probe.acquire(), "named lock released before run finalization"
        self.order.append("run_finished")
        return await super().finish(run, scope=scope)


class _NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SlowReadTool:
    name = "slow_read"
    description = "Wait until cancelled."
    Params = _NoParams
    risk = "read_only"
    capability_id = None
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def run(self, _params: BaseModel, _ctx: ToolContext) -> ToolResult:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return ToolResult(content="unreachable")


def _settings(tmp_path: Path) -> RickySettings:
    return RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "project_data_dir": ".ricky",
            "default_provider": "openrouter",
            "providers": {"openrouter": {"default_model": "test-model"}},
            "workflow": {"enabled": False},
            "memory": {"enabled": False},
            "google": {"accounts": {}},
            "google_oauth_clients": {},
        }
    )


def _bundle(
    tmp_path: Path,
    *,
    tools: list[str] | None = None,
    wall: float = 10,
    iterations: int = 2,
    result_notification: str | None = None,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='jobs-test'\n")
    root = tmp_path / "user" / "profiles" / "personal" / "jobs" / "brief"
    root.mkdir(parents=True)
    allowed = ", ".join(json.dumps(name) for name in (tools or []))
    notification = (
        f"result_notification = {json.dumps(result_notification)}\n"
        if result_notification is not None
        else ""
    )
    (root / "job.toml").write_text(
        f"""version = 3
name = "brief"
description = "Build a report."
provider = "openrouter"
model = "test-model"
goal = "Report what matters."
{notification}[budget]
wall_clock_seconds = {wall}
iterations = {iterations}
max_completion_tokens_per_request = 123
effect_calls = 0
[tools]
allow = [{allowed}]
""",
        encoding="utf-8",
    )


def _done(text: str = "Final report.", *, usage: Usage | None = None) -> MessageDone:
    return MessageDone(
        message=Message.text("assistant", text),
        usage=usage or Usage(prompt_tokens=7, completion_tokens=3),
    )


@pytest.mark.asyncio
async def test_named_job_runs_existing_loop_with_bounds_and_audit(tmp_path: Path) -> None:
    _bundle(tmp_path, tools=["read_file"])
    settings = _settings(tmp_path)
    provider = ScriptedProvider([[_done()]])
    run = await JobRunner(settings, project_root=tmp_path).run(
        "brief", profile_scope=SCOPE, provider=provider
    )

    assert run.outcome == "succeeded"
    assert run.final_message == "Final report."
    assert (run.iterations, run.prompt_tokens, run.completion_tokens) == (1, 7, 3)
    assert provider.closed
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.max_tokens == 123
    assert [tool.name for tool in request.tools] == ["read_file"]
    assert any(
        "job: brief" in getattr(part, "text", "")
        for message in request.messages
        for part in message.content
    )
    transcript = Path(run.transcript_path or "")
    lines = transcript.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["kind"] for line in lines][-1] == "turn_finished"
    assert oct(transcript.stat().st_mode & 0o777) == "0o600"
    snapshot = (
        Path(settings.user_data_dir)
        / "profiles"
        / "personal"
        / "agent-runs"
        / "specs"
        / str(run.spec_digest)
    )
    assert (snapshot / "job.toml").is_file()
    assert await JobRunStore(settings).get(run.id, scope=SCOPE) == run


@pytest.mark.asyncio
async def test_named_job_persists_silent_result_notification_policy(tmp_path: Path) -> None:
    _bundle(tmp_path, result_notification="never")
    settings = _settings(tmp_path)

    run = await JobRunner(settings, project_root=tmp_path).run(
        "brief",
        profile_scope=SCOPE,
        provider=ScriptedProvider([[_done()]]),
    )

    assert run.outcome == "succeeded"
    assert run.result_notification == "never"
    assert (await JobRunStore(settings).get(run.id, scope=SCOPE)).result_notification == "never"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["missing_tool", "write_file", "run_shell"])
async def test_invalid_or_mutating_tool_fails_before_model_and_is_audited(
    tmp_path: Path, tool: str
) -> None:
    _bundle(tmp_path, tools=[tool])
    settings = _settings(tmp_path)
    provider = ScriptedProvider([[_done()]])
    with pytest.raises(JobConfigurationError):
        await JobRunner(settings, project_root=tmp_path).run(
            "brief", profile_scope=SCOPE, provider=provider
        )

    assert provider.requests == []
    assert provider.closed
    history = await JobRunStore(settings).list(scope=SCOPE, job_name="personal/brief")
    assert len(history) == 1
    assert history[0].outcome == "failed"
    assert history[0].transcript_path is None


@pytest.mark.asyncio
async def test_held_job_lock_persists_skipped_attempt(tmp_path: Path) -> None:
    _bundle(tmp_path)
    settings = _settings(tmp_path)
    store = JobRunStore(settings)
    await store.initialize()
    lock = job_lock(store.root, "personal/brief")
    assert lock.acquire()
    provider = ScriptedProvider([[_done()]])
    try:
        run = await JobRunner(settings, project_root=tmp_path, store=store).run(
            "brief", profile_scope=SCOPE, provider=provider
        )
    finally:
        lock.release()

    assert run.outcome == "skipped_locked"
    assert provider.requests == []
    assert (await store.get(run.id, scope=SCOPE)).outcome == "skipped_locked"


@pytest.mark.asyncio
async def test_iteration_budget_is_persisted(tmp_path: Path) -> None:
    _bundle(tmp_path, tools=["read_file"], iterations=1)
    call = ToolCallPart(id="call_1", name="read_file", args={"path": "pyproject.toml"})
    provider = ScriptedProvider(
        [
            [
                MessageDone(
                    message=Message(role="assistant", content=[call]),
                    usage=Usage(prompt_tokens=2, completion_tokens=1),
                )
            ]
        ]
    )
    run = await JobRunner(_settings(tmp_path), project_root=tmp_path).run(
        "brief", profile_scope=SCOPE, provider=provider
    )

    assert run.outcome == "budget_exceeded"
    assert run.iterations == 1
    assert "maximum turn iterations" in (run.error or "")
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_wall_clock_timeout_cancels_provider_and_finishes_record(tmp_path: Path) -> None:
    _bundle(tmp_path, wall=0.01)
    settings = _settings(tmp_path)
    provider = SlowProvider()
    run = await JobRunner(settings, project_root=tmp_path).run(
        "brief", profile_scope=SCOPE, provider=provider
    )

    assert run.outcome == "budget_exceeded"
    assert provider.cancelled and provider.closed
    assert (await JobRunStore(settings).get(run.id, scope=SCOPE)).finished_at is not None


@pytest.mark.asyncio
async def test_outer_cancellation_becomes_interrupted_and_cleans_up(tmp_path: Path) -> None:
    _bundle(tmp_path, wall=10)
    settings = _settings(tmp_path)
    provider = SlowProvider()
    task = asyncio.create_task(
        JobRunner(settings, project_root=tmp_path).run(
            "brief", profile_scope=SCOPE, provider=provider
        )
    )
    await provider.started.wait()
    task.cancel()
    run = await task

    assert run.outcome == "interrupted"
    assert provider.cancelled and provider.closed
    assert (await JobRunStore(settings).get(run.id, scope=SCOPE)).outcome == "interrupted"


@pytest.mark.asyncio
async def test_cancellation_awaits_in_flight_tool_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bundle(tmp_path, tools=["slow_read"])
    settings = _settings(tmp_path)
    tool = SlowReadTool()
    monkeypatch.setattr("ricky.runtime.composition.builtin_tools", lambda: [tool])
    call = ToolCallPart(id="call_slow", name="slow_read", args={})
    provider = ScriptedProvider([[MessageDone(message=Message(role="assistant", content=[call]))]])
    task = asyncio.create_task(
        JobRunner(settings, project_root=tmp_path).run(
            "brief", profile_scope=SCOPE, provider=provider
        )
    )
    await tool.started.wait()
    task.cancel()
    run = await task

    assert run.outcome == "interrupted"
    assert tool.cancelled
    assert provider.closed


@pytest.mark.asyncio
async def test_provider_and_transcript_failures_finish_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bundle(tmp_path)
    settings = _settings(tmp_path)
    provider_error = ScriptedProvider([[RuntimeError("provider exploded")]])
    failed = await JobRunner(settings, project_root=tmp_path).run(
        "brief", profile_scope=SCOPE, provider=provider_error
    )
    assert failed.outcome == "failed"
    assert "provider exploded" in (failed.error or "")

    async def fail_append(_self, _event) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("ricky.jobs.runner.JobTranscript.append", fail_append)
    transcript_error = ScriptedProvider([[_done()]])
    failed_transcript = await JobRunner(settings, project_root=tmp_path).run(
        "brief", profile_scope=SCOPE, provider=transcript_error
    )
    assert failed_transcript.outcome == "failed"
    assert "disk full" in (failed_transcript.error or "")
    assert transcript_error.closed


@pytest.mark.asyncio
async def test_ad_hoc_runs_have_no_identity_or_named_lock(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='jobs-test'\n")
    provider = ScriptedProvider([[_done("Once.")]])
    run = await JobRunner(_settings(tmp_path), project_root=tmp_path).once(
        "One report", profile_scope=SCOPE, provider=provider
    )
    assert run.outcome == "succeeded"
    assert run.job_name is None and run.spec_digest is None


@pytest.mark.asyncio
async def test_each_run_has_fresh_session_and_no_prior_transcript(tmp_path: Path) -> None:
    _bundle(tmp_path)
    settings = _settings(tmp_path)
    runner = JobRunner(settings, project_root=tmp_path)
    first_provider = ScriptedProvider([[_done("First result")]])
    second_provider = ScriptedProvider([[_done("Second result")]])
    first = await runner.run("brief", profile_scope=SCOPE, provider=first_provider)
    second = await runner.run("brief", profile_scope=SCOPE, provider=second_provider)

    assert first.session_id != second.session_id
    visible_text = [
        part.text
        for message in second_provider.requests[0].messages
        for part in message.content
        if isinstance(part, TextPart)
    ]
    assert "First result" not in visible_text


@pytest.mark.asyncio
async def test_resources_close_before_finish_and_lock_releases_last(tmp_path: Path) -> None:
    _bundle(tmp_path)
    settings = _settings(tmp_path)
    order: list[str] = []
    store = OrderedStore(settings, order)
    provider = OrderedProvider(order)
    run = await JobRunner(settings, project_root=tmp_path, store=store).run(
        "brief", profile_scope=SCOPE, provider=provider
    )

    assert run.outcome == "succeeded"
    assert order == ["provider_closed", "run_finished"]
    probe = job_lock(store.root, "personal/brief")
    assert probe.acquire()
    probe.release()


@pytest.mark.asyncio
async def test_runtime_cleanup_failure_replaces_success_and_is_persisted(tmp_path: Path) -> None:
    _bundle(tmp_path)
    settings = _settings(tmp_path)
    order: list[str] = []
    provider = OrderedProvider(order, fail_close=True)
    run = await JobRunner(settings, project_root=tmp_path).run(
        "brief", profile_scope=SCOPE, provider=provider
    )

    assert run.outcome == "failed"
    assert "runtime cleanup failed" in (run.error or "")
    assert (await JobRunStore(settings).get(run.id, scope=SCOPE)).outcome == "failed"


@pytest.mark.asyncio
async def test_completed_transcript_retention_never_targets_live_runs(tmp_path: Path) -> None:
    _bundle(tmp_path)
    base = _settings(tmp_path)
    settings = base.model_copy(
        update={"jobs": base.jobs.model_copy(update={"transcript_retention": 1})}
    )
    runner = JobRunner(settings, project_root=tmp_path)
    first = await runner.run(
        "brief", profile_scope=SCOPE, provider=ScriptedProvider([[_done("First")]])
    )
    second = await runner.run(
        "brief", profile_scope=SCOPE, provider=ScriptedProvider([[_done("Second")]])
    )

    assert first.transcript_path is not None
    assert not Path(first.transcript_path).exists()
    assert Path(second.transcript_path or "").exists()
    stored_first = await JobRunStore(settings).get(first.id, scope=SCOPE)
    assert stored_first.transcript_path is None


def test_cli_run_history_and_report(tmp_path: Path, monkeypatch) -> None:
    _bundle(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RICKY_USER_DATA_DIR", str(tmp_path / "user"))
    providers: list[ScriptedProvider] = []

    def provider_factory(*_args, **_kwargs):
        provider = ScriptedProvider([[_done("CLI report.")]])
        providers.append(provider)
        return provider

    monkeypatch.setattr("ricky.runtime.composition.create_provider", provider_factory)
    cli = CliRunner()
    result = cli.invoke(app, ["job", "run", "brief"])
    assert result.exit_code == 0
    assert "succeeded" in result.stdout
    assert "CLI report." in result.stdout
    history = cli.invoke(app, ["job", "history", "--job", "brief"])
    assert history.exit_code == 0
    run_id = history.stdout.split()[0]
    report = cli.invoke(app, ["job", "report", run_id])
    assert report.exit_code == 0
    assert "CLI report." in report.stdout
    assert providers[0].closed


def test_provider_free_cli_list_show_validate_history(tmp_path: Path, monkeypatch) -> None:
    _bundle(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RICKY_USER_DATA_DIR", str(tmp_path / "user"))
    monkeypatch.setattr(
        "ricky.runtime.composition.create_provider",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("provider constructed")),
    )
    runner = CliRunner()
    assert runner.invoke(app, ["job", "list"]).exit_code == 0
    shown = runner.invoke(app, ["job", "show", "brief"])
    assert shown.exit_code == 0
    assert "tool availability: available" in shown.stdout
    assert runner.invoke(app, ["job", "validate", "brief"]).exit_code == 0
    assert runner.invoke(app, ["job", "history"]).exit_code == 0


@pytest.mark.asyncio
async def test_job_transcript_keeps_expanded_context_event(tmp_path: Path) -> None:
    _bundle(tmp_path, tools=["read_file"])
    settings = _settings(tmp_path)
    provider = ScriptedProvider([[_done("Unchanged result.")]])

    run = await JobRunner(settings, project_root=tmp_path).run(
        "brief", profile_scope=SCOPE, provider=provider
    )

    records = [
        json.loads(line)
        for line in Path(run.transcript_path or "").read_text(encoding="utf-8").splitlines()
    ]
    context_event = next(record for record in records if record["kind"] == "context_assembled")
    assert context_event["report"]["estimated_input_tokens"] > 0
    assert context_event["report"]["tool_count"] == 1
    assert any(
        section["name"] == "advertised_tool_definitions"
        for section in context_event["report"]["sections"]
    )
    assert provider.requests[0].tools[0].name == "read_file"
    assert run.final_message == "Unchanged result."
