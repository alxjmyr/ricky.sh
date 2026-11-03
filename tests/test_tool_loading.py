"""Tool advertisement is turn-owned and independent from execution authority."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import ClassVar

import pytest
from pydantic import BaseModel, ConfigDict, Field

from ricky.agent import AgentLoop, AgentSession
from ricky.agent.events import ToolCallRejectedEvent, TurnFinishedEvent
from ricky.agent.session import SessionArtifactRecord
from ricky.agent.tool_loading import LoadToolsParams, LoadToolsTool, ToolLoadingState
from ricky.config import RickySettings
from ricky.llm import (
    CompletionRequest,
    Message,
    MessageDone,
    StreamEvent,
    ToolCallPart,
    ToolResultPart,
)
from ricky.permissions import PermissionEngine
from ricky.permissions.types import Policy, PolicyRule
from ricky.tools import Risk, ToolContext, ToolRegistry, ToolResult
from ricky.tools.testing import assert_tool_contract


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: str = Field(description="Detailed argument guidance. " * 100)


class RecordingTool:
    name: ClassVar[str] = "service_read"
    description: ClassVar[str] = "Read a record from the selected service."
    Params: ClassVar[type[BaseModel]] = Params
    risk: ClassVar[Risk] = "read_only"
    capability_id = "builtin.session.mutate"
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None

    def __init__(self, name: str = "service_read") -> None:
        object.__setattr__(self, "name", name)
        self.calls: list[str] = []

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        self.calls.append(Params.model_validate(params).value)
        return ToolResult(content="record")


class Provider:
    name = "fake"

    def __init__(self, *messages: Message) -> None:
        self.messages = list(messages)
        self.requests: list[CompletionRequest] = []

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        yield MessageDone(message=self.messages.pop(0))

    async def aclose(self) -> None:
        pass


def calls(*items: tuple[str, dict[str, object]]) -> Message:
    return Message(
        role="assistant",
        content=[
            ToolCallPart(id=f"call_{index}_{name}", name=name, args=args)
            for index, (name, args) in enumerate(items)
        ],
    )


def session_for(settings: RickySettings) -> AgentSession:
    return AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())


def names(request: CompletionRequest) -> set[str]:
    return {tool.name for tool in request.tools}


async def test_batched_loading_is_native_and_resets_after_turn() -> None:
    settings = RickySettings()
    session = session_for(settings)
    first, second = RecordingTool(), RecordingTool("service_other")
    provider = Provider(
        calls(("load_tools", {"names": [second.name, first.name, first.name]})),
        calls((first.name, {"value": "one"}), (second.name, {"value": "two"})),
        Message.text("assistant", "done"),
        Message.text("assistant", "next"),
    )
    registry = ToolRegistry([first, second])
    loop = AgentLoop(provider=provider, registry=registry, settings=settings, tool_loading="chat")
    eager = AgentLoop(provider=provider, registry=registry, settings=settings)
    initial = loop.inspect_context(session)
    assert initial.serialized_chars < eager.inspect_context(session).serialized_chars
    assert any(section.name == "tool_discovery_catalog" for section in initial.sections)
    events = [event async for event in loop.run_turn(session, "read both")]
    assert isinstance(events[-1], TurnFinishedEvent) and events[-1].error is None
    assert names(provider.requests[0]) == {"load_tools"}
    assert names(provider.requests[1]) == {first.name, second.name, "load_tools"}
    assert provider.requests[1].tools[0].parameters == Params.model_json_schema()
    assert first.calls == ["one"] and second.calls == ["two"]
    results = [p for m in session.history for p in m.content if isinstance(p, ToolResultPart)]
    assert "Detailed argument guidance" not in results[0].content
    assert loop.inspect_context(session).tool_count == 1
    _ = [event async for event in loop.run_turn(session, "follow up")]
    assert names(provider.requests[-1]) == {"load_tools"}


async def test_invalid_batch_and_same_response_calls_cannot_dispatch() -> None:
    settings = RickySettings()
    session = session_for(settings)
    tool = RecordingTool()
    provider = Provider(
        calls(("load_tools", {"names": [tool.name, "forbidden_tool"]})),
        calls(("load_tools", {"names": [tool.name]}), (tool.name, {"value": "too early"})),
        calls((tool.name, {"value": "valid"})),
        Message.text("assistant", "done"),
    )
    loop = AgentLoop(
        provider=provider, registry=ToolRegistry([tool]), settings=settings, tool_loading="chat"
    )
    events = [event async for event in loop.run_turn(session, "read")]
    assert names(provider.requests[1]) == {"load_tools"}
    assert tool.calls == ["valid"]
    assert any(isinstance(event, ToolCallRejectedEvent) for event in events)
    results = [p for m in session.history for p in m.content if isinstance(p, ToolResultPart)]
    assert any("No tools loaded" in p.content for p in results)
    assert any("not loaded" in p.content for p in results)


async def test_loading_does_not_bypass_permission_or_validation() -> None:
    settings = RickySettings()
    session = session_for(settings)
    tool = RecordingTool()
    provider = Provider(
        calls(("load_tools", {"names": [tool.name]})),
        calls((tool.name, {"value": 123})),
        calls((tool.name, {"value": "denied"})),
        Message.text("assistant", "done"),
    )
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([tool]),
        settings=settings,
        tool_loading="chat",
        permission_engine=PermissionEngine(
            Policy(rules=[PolicyRule(tool_name=tool.name, decision="deny")])
        ),
    )
    events = [event async for event in loop.run_turn(session, "read")]
    assert tool.calls == []
    assert any(
        isinstance(event, ToolCallRejectedEvent) and event.reason == "invalid_arguments"
        for event in events
    )


async def test_cancellation_drops_loaded_state() -> None:
    settings = RickySettings()
    session = session_for(settings)
    tool = RecordingTool()
    entered = asyncio.Event()

    class WaitingProvider(Provider):
        async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
            if self.requests:
                self.requests.append(request)
                entered.set()
                await asyncio.Event().wait()
            else:
                async for event in super().stream(request):
                    yield event

    provider = WaitingProvider(calls(("load_tools", {"names": [tool.name]})))
    loop = AgentLoop(
        provider=provider, registry=ToolRegistry([tool]), settings=settings, tool_loading="chat"
    )

    async def consume() -> None:
        async for _ in loop.run_turn(session, "read"):
            pass

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert tool.name in names(provider.requests[-1])
    finally:
        task.cancel()
        await task
    assert loop.inspect_context(session).tool_count == 1
    assert tool.calls == []


async def test_loader_contract_and_registry_preserve_wrapped_instances(tmp_path) -> None:
    settings = RickySettings()
    session = session_for(settings)
    tool = RecordingTool()
    state = ToolLoadingState("chat")
    registry = ToolRegistry([tool])
    loader = LoadToolsTool(state, registry)
    await assert_tool_contract(
        loader,
        valid_args={"names": [tool.name]},
        ctx=ToolContext(
            cwd=tmp_path,
            settings=settings,
            session=session,
        ),
    )
    assert state.registry(registry).get(tool.name) is tool
    with pytest.raises(ValueError):
        LoadToolsParams.model_validate({"names": []})


def test_eager_mode_and_gateway_baseline_do_not_expand_authority() -> None:
    settings = RickySettings()
    session = session_for(settings)
    file_tool = RecordingTool("read_file")
    registry = ToolRegistry([file_tool])
    provider = Provider()
    eager = AgentLoop(provider=provider, registry=registry, settings=settings)
    gateway = AgentLoop(
        provider=provider, registry=registry, settings=settings, tool_loading="gateway"
    )
    assert eager._registry_for(session).get("read_file") is file_tool
    assert gateway._registry_for(session).get("read_file") is None
    assert gateway._registry_for(session).get("delegate_task") is None


async def test_selected_registry_preserves_result_limits(tmp_path) -> None:
    class VerboseTool(RecordingTool):
        async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
            return ToolResult(content="record " * 200)

    settings = RickySettings()
    session = session_for(settings)
    tool = VerboseTool()
    registry = ToolRegistry([tool], max_result_chars=100)
    state = ToolLoadingState("chat")
    state.loaded.add(tool.name)
    ctx = ToolContext(cwd=tmp_path, settings=settings, session=session)
    direct = await registry.dispatch(tool.name, {"value": "read"}, ctx)
    selected = await state.registry(registry).dispatch(tool.name, {"value": "read"}, ctx)
    assert selected == direct
    assert len(selected.content) <= 100
    assert selected.full_content_chars == 1400


async def test_artifact_reader_is_available_only_after_artifact_and_keeps_limits(tmp_path) -> None:
    class ArtifactReader(RecordingTool):
        deferred_until_artifact = True

        async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
            return ToolResult(content="record " * 200)

    settings = RickySettings()
    session = session_for(settings)
    reader = ArtifactReader("read_tool_artifact")
    loop = AgentLoop(
        provider=Provider(),
        registry=ToolRegistry([], max_result_chars=100),
        settings=settings,
        tool_loading="chat",
        deferred_tools=(reader,),
    )
    assert loop._registry_for(session).get(reader.name) is None
    session.artifacts.append(
        SessionArtifactRecord(
            id="artifact_" + "a" * 32,
            call_id="call",
            tool_name="service_read",
            full_chars=1400,
            sha256="b" * 64,
            excerpt_chars=100,
            relative_path="artifact_" + "a" * 32 + ".txt",
        )
    )
    selected = loop._registry_for(session)
    assert selected.get(reader.name) is reader
    result = await selected.dispatch(
        reader.name,
        {"value": "read"},
        ToolContext(
            cwd=tmp_path,
            settings=settings,
            session=session,
        ),
    )
    assert len(result.content) <= 100
    assert result.full_content_chars == 1400


async def test_concurrent_sessions_have_independent_loaded_schemas() -> None:
    settings = RickySettings()
    first_session, second_session = session_for(settings), session_for(settings)
    first_tool, second_tool = RecordingTool("first_read"), RecordingTool("second_read")
    choices = {first_session.id: first_tool.name, second_session.id: second_tool.name}
    waiting: dict[str, CompletionRequest] = {}
    both_waiting = asyncio.Event()
    release = asyncio.Event()

    class ConcurrentProvider(Provider):
        async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
            assert request.session_id is not None
            wanted = choices[request.session_id]
            if wanted not in names(request):
                yield MessageDone(message=calls(("load_tools", {"names": [wanted]})))
            else:
                waiting[request.session_id] = request
                if len(waiting) == 2:
                    both_waiting.set()
                await release.wait()
                yield MessageDone(message=Message.text("assistant", "done"))

    loop = AgentLoop(
        provider=ConcurrentProvider(),
        registry=ToolRegistry([first_tool, second_tool]),
        settings=settings,
        tool_loading="chat",
    )

    async def consume(session: AgentSession) -> None:
        async for _ in loop.run_turn(session, "read"):
            pass

    tasks = [asyncio.create_task(consume(session)) for session in (first_session, second_session)]
    try:
        await asyncio.wait_for(both_waiting.wait(), 5)
        assert names(waiting[first_session.id]) == {"load_tools", first_tool.name}
        assert names(waiting[second_session.id]) == {"load_tools", second_tool.name}
        assert loop._registry_for(first_session).get(second_tool.name) is None
        assert loop._registry_for(second_session).get(first_tool.name) is None
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert loop.inspect_context(first_session).tool_count == 1
    assert loop.inspect_context(second_session).tool_count == 1
