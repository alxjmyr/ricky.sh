"""Lossless tool-result offloading acceptance tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel, ConfigDict

from ricky.agent import AgentLoop, AgentSession
from ricky.agent.artifacts import SessionArtifactError, SessionArtifactStore
from ricky.agent.events import (
    AgentEvent,
    ToolCallFinishedEvent,
    ToolResultOffloadFailedEvent,
)
from ricky.config import RickySettings
from ricky.jobs.runner import JobRunner
from ricky.jobs.store import JobRunStore
from ricky.jobs.transcript import prune_transcripts
from ricky.jobs.types import JobRun
from ricky.llm import (
    CompletionRequest,
    Message,
    MessageDone,
    StreamEvent,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from ricky.tools import EffectReceipt, Risk, ToolContext, ToolRegistry, ToolResult
from ricky.tools.builtin.artifacts import ReadToolArtifactTool


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _LargeTool:
    name: ClassVar[str] = "large_result"
    description: ClassVar[str] = "Return fixed text for offload tests."
    Params: ClassVar[type[BaseModel]] = _Params
    risk: ClassVar[Risk] = "read_only"

    def __init__(
        self,
        content: str,
        *,
        is_error: bool = False,
        data: object = None,
        effect_receipt: EffectReceipt | None = None,
    ) -> None:
        self.content = content
        self.is_error = is_error
        self.data = data
        self.effect_receipt = effect_receipt
        self.calls = 0

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del params, ctx
        self.calls += 1
        return ToolResult(
            content=self.content,
            is_error=self.is_error,
            data=self.data,  # type: ignore[arg-type]
            effect_receipt=self.effect_receipt,
        )


class _Provider:
    name = "scripted"

    def __init__(self, scripts: list[list[StreamEvent]]) -> None:
        self.scripts = scripts
        self.requests: list[CompletionRequest] = []

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        for event in self.scripts.pop(0):
            yield event

    async def aclose(self) -> None:
        pass


class _FailingStore(SessionArtifactStore):
    async def offload(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        raise OSError("synthetic persistence failure")


class _BlockingStore(SessionArtifactStore):
    def __init__(self, settings: RickySettings, session_id: str) -> None:
        super().__init__(settings, session_id)
        self.started = threading.Event()
        self.release = threading.Event()

    def _write_atomic(self, relative_path: str, payload: bytes) -> None:
        self.started.set()
        self.release.wait(timeout=5)
        super()._write_atomic(relative_path, payload)


def _settings(tmp_path: Path, **overrides: object) -> RickySettings:
    tool_results: dict[str, object] = {
        "enabled": True,
        "offload_threshold_chars": 40,
        "inline_excerpt_chars": 12,
        "head_fraction": 0.75,
        "artifact_max_chars": 500,
        "session_artifact_max_chars": 1_000,
        "read_chunk_chars": 17,
    }
    tool_results.update(overrides)
    return RickySettings.model_validate(
        {
            "user_data_dir": str(tmp_path / "user"),
            "context": {"tool_results": tool_results},
        }
    )


def _tool_call() -> MessageDone:
    return MessageDone(
        message=Message(
            role="assistant",
            content=[ToolCallPart(id="call_large", name="large_result")],
        ),
        stop_reason="tool_calls",
    )


def _final() -> MessageDone:
    return MessageDone(
        message=Message(role="assistant", content=[TextPart(text="done")]),
        stop_reason="stop",
    )


async def _events(loop: AgentLoop, session: AgentSession) -> list[AgentEvent]:
    return [event async for event in loop.run_turn(session, "get the full result")]


@pytest.mark.asyncio
async def test_small_result_stays_inline_without_creating_artifact(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    store = SessionArtifactStore.create(settings, session.id)
    tool = _LargeTool("small result")

    result = await ToolRegistry([tool]).dispatch(
        tool.name,
        {},
        ToolContext(
            cwd=tmp_path,
            settings=settings,
            session=session,
            artifact_sink=store,
        ),
        call_id="call_small",
    )

    assert result.content == "small result"
    assert result.artifact is None
    assert session.artifacts == []
    assert not store.root.exists()


@pytest.mark.asyncio
async def test_loop_offloads_large_result_and_accounts_only_visible_projection(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    body = "HEAD-" + "x" * 90 + "-TAIL"
    tool = _LargeTool(body, is_error=True)
    store = SessionArtifactStore.create(settings, session.id)
    reader = ReadToolArtifactTool(store)
    provider = _Provider([[_tool_call()], [_final()]])
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([tool]),
        settings=settings,
        cwd=tmp_path,
        artifact_store=store,
        deferred_tools=(reader,),
    )

    events = await _events(loop, session)

    assert tool.calls == 1
    assert len(provider.requests) == 2
    assert [spec.name for spec in provider.requests[0].tools] == ["large_result"]
    assert [spec.name for spec in provider.requests[1].tools] == [
        "large_result",
        "read_tool_artifact",
    ]
    part = session.history[2].content[0]
    assert isinstance(part, ToolResultPart)
    assert part.is_error
    assert part.artifact is not None
    assert body not in part.content
    assert body[:9] in part.content and body[-3:] in part.content
    assert "[omitted 88 chars; use read_tool_artifact]" in part.content
    assert str(store.root) not in part.content
    assert provider.requests[1].messages[-1] == session.history[2]

    record = session.artifacts[0]
    stored = (store.root / record.relative_path).read_text(encoding="utf-8")
    assert stored == body
    assert record.full_chars == len(body)
    assert record.sha256 == hashlib.sha256(body.encode()).hexdigest()
    if os.name == "posix":
        assert (store.root.stat().st_mode & 0o777) == 0o700
        assert ((store.root / record.relative_path).stat().st_mode & 0o777) == 0o600

    finished = next(event for event in events if isinstance(event, ToolCallFinishedEvent))
    assert finished.offloaded and finished.artifact_id == record.id
    assert finished.full_content_chars == len(body)
    assert finished.visible_content_chars == len(part.content)
    assert finished.content == part.content and body not in (finished.content or "")
    report = loop.inspect_context(session)
    assert report.artifact_count == 1
    assert report.stored_artifact_chars == len(body)
    tool_results = next(section for section in report.sections if section.name == "tool_results")
    assert tool_results.chars == len(part.model_dump_json(exclude_none=False))
    assert "x" * 20 not in provider.requests[1].model_dump_json()
    assert sum(section.chars for section in report.sections) == report.serialized_chars


@pytest.mark.asyncio
async def test_reader_pages_full_unicode_content_without_overlap(tmp_path: Path) -> None:
    settings = _settings(tmp_path, read_chunk_chars=11)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    store = SessionArtifactStore.create(settings, session.id)
    body = "αβγ-" * 25
    record = await store.offload(
        session,
        call_id="call_unicode",
        tool_name="large_result",
        content=body,
        excerpt_chars=12,
    )
    reader = ReadToolArtifactTool(store)
    registry = ToolRegistry([reader])
    offset = 0
    chunks: list[str] = []
    while True:
        result = await registry.dispatch(
            reader.name,
            {"artifact_id": record.id, "offset": offset, "max_chars": 11},
            ToolContext(cwd=tmp_path, settings=settings, session=session),
            call_id=f"read_{offset}",
        )
        assert not result.is_error
        assert isinstance(result.data, dict)
        chunks.append(str(result.data["content"]))
        next_offset = result.data["next_offset"]
        if next_offset is None:
            break
        assert isinstance(next_offset, int)
        offset = next_offset

    assert "".join(chunks) == body


@pytest.mark.asyncio
async def test_result_above_legacy_limit_is_recoverable_byte_for_byte(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        offload_threshold_chars=12_000,
        inline_excerpt_chars=8_000,
        artifact_max_chars=20_000,
        session_artifact_max_chars=20_000,
        read_chunk_chars=12_000,
    )
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    store = SessionArtifactStore.create(settings, session.id)
    body = "start:" + "0123456789" * 1_300 + ":end"
    result = await ToolRegistry([_LargeTool(body)]).dispatch(
        "large_result",
        {},
        ToolContext(
            cwd=tmp_path,
            settings=settings,
            session=session,
            artifact_sink=store,
        ),
        call_id="call_legacy_limit",
    )
    assert len(body) > 12_000
    assert result.artifact is not None and body not in result.content
    first = await store.read(session, result.artifact.id, max_chars=12_000)
    second = await store.read(session, result.artifact.id, offset=first.end_offset)
    assert first.content + second.content == body


@pytest.mark.asyncio
async def test_manifest_round_trip_reopens_and_security_failures_close_access(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    store = SessionArtifactStore.create(settings, session.id)
    first = await store.offload(
        session,
        call_id="call_first",
        tool_name="large_result",
        content="a" * 80,
        excerpt_chars=12,
    )
    restored = AgentSession.model_validate_json(session.model_dump_json())
    reopened = SessionArtifactStore.create(settings, restored.id)
    assert (await reopened.read(restored, first.id, max_chars=17)).content == "a" * 17

    with pytest.raises(SessionArtifactError, match="unknown"):
        await reopened.read(restored, "../artifact_escape", max_chars=1)
    with pytest.raises(SessionArtifactError, match="unknown"):
        await reopened.read(restored, "artifact_" + "f" * 32, max_chars=1)
    with pytest.raises(SessionArtifactError, match="read size"):
        await reopened.read(restored, first.id, max_chars=18)
    with pytest.raises(SessionArtifactError, match="offset"):
        await reopened.read(restored, first.id, offset=-1)

    first_path = reopened.root / first.relative_path
    first_path.write_text("tampered", encoding="utf-8")
    with pytest.raises(SessionArtifactError, match="mismatch"):
        await reopened.read(restored, first.id, max_chars=1)

    second = await store.offload(
        session,
        call_id="call_second",
        tool_name="large_result",
        content="b" * 80,
        excerpt_chars=12,
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    second_path = store.root / second.relative_path
    second_path.unlink()
    second_path.symlink_to(outside)
    with pytest.raises(SessionArtifactError, match="symlink|regular"):
        await store.read(session, second.id, max_chars=1)

    third = await store.offload(
        session,
        call_id="call_missing",
        tool_name="large_result",
        content="c" * 80,
        excerpt_chars=12,
    )
    (store.root / third.relative_path).unlink()
    with pytest.raises(SessionArtifactError, match="missing"):
        await store.read(session, third.id, max_chars=1)


@pytest.mark.asyncio
async def test_concurrent_writes_are_distinct_and_respect_aggregate_limit(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, artifact_max_chars=100, session_artifact_max_chars=160)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    store = SessionArtifactStore.create(settings, session.id)

    records = await asyncio.gather(
        *[
            store.offload(
                session,
                call_id=f"call_{index}",
                tool_name="large_result",
                content=str(index) * 50,
                excerpt_chars=12,
            )
            for index in range(3)
        ]
    )
    assert len({record.id for record in records}) == 3
    assert len(session.artifacts) == 3
    assert all((store.root / record.relative_path).is_file() for record in records)

    with pytest.raises(SessionArtifactError, match="session artifact limit"):
        await store.offload(
            session,
            call_id="call_over",
            tool_name="large_result",
            content="z" * 20,
            excerpt_chars=12,
        )


@pytest.mark.asyncio
async def test_cancellation_during_write_leaves_no_file_or_manifest(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    store = _BlockingStore(settings, session.id)
    task = asyncio.create_task(
        store.offload(
            session,
            call_id="call_cancel",
            tool_name="large_result",
            content="c" * 80,
            excerpt_chars=12,
        )
    )
    await asyncio.to_thread(store.started.wait, 2)
    task.cancel()
    store.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.artifacts == []
    assert not store.root.exists() or list(store.root.iterdir()) == []


@pytest.mark.asyncio
async def test_persistence_failure_never_replays_tool_and_emits_bounded_warning(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    body = "sensitive-" + "q" * 100
    tool = _LargeTool(body, effect_receipt=EffectReceipt(disposition="performed"))
    store = _FailingStore(settings, session.id)
    provider = _Provider([[_tool_call()], [_final()]])
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([tool], max_result_chars=40),
        settings=settings,
        cwd=tmp_path,
        artifact_store=store,
    )

    events = await _events(loop, session)

    assert tool.calls == 1
    assert len(provider.requests) == 2
    part = session.history[2].content[0]
    assert isinstance(part, ToolResultPart)
    assert part.artifact is None
    assert len(part.content) == 40
    assert body not in part.content
    assert "truncated" in part.content
    assert session.artifacts == []
    warning = next(event for event in events if isinstance(event, ToolResultOffloadFailedEvent))
    assert warning.error_type == "OSError"
    assert warning.full_content_chars == len(body)
    assert body not in warning.model_dump_json()


@pytest.mark.asyncio
async def test_offload_preserves_structured_data_and_effect_receipt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    store = SessionArtifactStore.create(settings, session.id)
    receipt = EffectReceipt(disposition="performed", provider_reference="remote-1")
    tool = _LargeTool("d" * 80, data={"value": 7}, effect_receipt=receipt)

    result = await ToolRegistry([tool]).dispatch(
        tool.name,
        {},
        ToolContext(
            cwd=tmp_path,
            settings=settings,
            session=session,
            artifact_sink=store,
        ),
        call_id="call_structured",
    )

    assert result.artifact is not None
    assert result.data == {"value": 7}
    assert result.effect_receipt == receipt


@pytest.mark.asyncio
async def test_transcript_retention_removes_only_last_session_reference(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = JobRunStore(settings)
    await store.initialize()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    artifacts = SessionArtifactStore.create(settings, session.id)
    await artifacts.offload(
        session,
        call_id="call_job",
        tool_name="large_result",
        content="j" * 80,
        excerpt_chars=12,
    )
    transcript_root = store.root / "transcripts"
    transcript_root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    runs: list[JobRun] = []
    for index, finished_at in enumerate((now - timedelta(seconds=1), now)):
        path = transcript_root / f"run_{index}.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        run = JobRun(
            id=f"run_{index}",
            profile_scope=session.profile_scope,
            provider="openrouter",
            model="test",
            session_id=session.id,
            outcome="succeeded",
            started_at=finished_at - timedelta(seconds=1),
            finished_at=finished_at,
            transcript_path=str(path),
        )
        await store.insert(run, scope=session.profile_scope)
        runs.append(run)

    await prune_transcripts(
        store,
        scope=session.profile_scope,
        keep=1,
        settings=settings,
    )
    assert artifacts.root.exists()
    assert (await store.get(runs[0].id, scope=session.profile_scope)).transcript_path is None
    assert (await store.get(runs[1].id, scope=session.profile_scope)).transcript_path is not None

    await prune_transcripts(
        store,
        scope=session.profile_scope,
        keep=0,
        settings=settings,
    )
    assert not artifacts.root.exists()
    assert (await store.get(runs[1].id, scope=session.profile_scope)).transcript_path is None


@pytest.mark.asyncio
async def test_job_loop_uses_same_offload_projection_and_bounded_transcript(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    body = "job-sensitive-" + "r" * 100
    (tmp_path / "large.txt").write_text(body, encoding="utf-8")
    provider = _Provider(
        [
            [
                MessageDone(
                    message=Message(
                        role="assistant",
                        content=[
                            ToolCallPart(
                                id="call_job_read",
                                name="read_file",
                                args={"path": "large.txt"},
                            )
                        ],
                    ),
                    stop_reason="tool_calls",
                )
            ],
            [_final()],
        ]
    )

    run = await JobRunner(settings, project_root=tmp_path).once(
        "read the large file",
        profile_scope=settings.resolve_profile_scope(),
        tools=["read_file"],
        provider=provider,
    )

    assert run.outcome == "succeeded"
    assert [tool.name for tool in provider.requests[0].tools] == ["read_file"]
    assert [tool.name for tool in provider.requests[1].tools] == [
        "read_file",
        "read_tool_artifact",
    ]
    result = next(
        part
        for message in provider.requests[1].messages
        for part in message.content
        if isinstance(part, ToolResultPart)
    )
    assert result.artifact is not None
    assert body not in result.content
    artifact_root = Path(settings.user_data_dir) / "sessions" / run.session_id / "artifacts"
    stored_files = list(artifact_root.glob("artifact_*.txt"))
    assert len(stored_files) == 1
    assert body in stored_files[0].read_text(encoding="utf-8")
    transcript = Path(run.transcript_path or "").read_text(encoding="utf-8")
    assert body not in transcript
    records = [json.loads(line) for line in transcript.splitlines()]
    finished = next(record for record in records if record["kind"] == "tool_call_finished")
    assert finished["offloaded"] is True
    assert finished["artifact_id"] == result.artifact.id
