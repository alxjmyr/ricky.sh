"""Scripted tests for the agent loop state machine."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from ricky.agent import AgentLoop, AgentSession
from ricky.agent.events import (
    AgentEvent,
    PermissionRequestedEvent,
    ToolCallFinishedEvent,
    UserInteractionRequiredEvent,
)
from ricky.config import RickySettings
from ricky.llm import (
    CompletionRequest,
    ImagePart,
    MediaArtifactRef,
    Message,
    MessageDone,
    StreamEvent,
    TextDelta,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    Usage,
)
from ricky.permissions import GrantScope, PermissionResponse
from ricky.profiles import ProfileLabel
from ricky.tool_contracts import ToolRuntimeFailure
from ricky.tools import (
    EffectIdentity,
    EffectReceipt,
    PreparedEffect,
    Risk,
    ToolContext,
    ToolRegistry,
    ToolResult,
    UserInteractionRequest,
    builtin_tools,
    make_effect_identity,
)


class FakeProvider:
    name = "fake"

    def __init__(self, scripts: list[list[StreamEvent | BaseException]]) -> None:
        self.scripts = scripts
        self.requests: list[CompletionRequest] = []

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        script = self.scripts.pop(0)
        for event in script:
            if isinstance(event, BaseException):
                raise event
            yield event

    async def aclose(self) -> None:
        pass


async def _collect_events(loop: AgentLoop, session: AgentSession, prompt: str) -> list[AgentEvent]:
    return [event async for event in loop.run_turn(session, prompt)]


def _tool_message(call_id: str, name: str, args: dict[str, object]) -> MessageDone:
    return MessageDone(
        message=Message(
            role="assistant",
            content=[ToolCallPart(id=call_id, name=name, args=args)],
        ),
        usage=Usage(prompt_tokens=1, completion_tokens=1),
        stop_reason="tool_calls",
    )


def _final_message(text: str) -> MessageDone:
    return MessageDone(
        message=Message(role="assistant", content=[TextPart(text=text)]),
        usage=Usage(prompt_tokens=2, completion_tokens=3),
        stop_reason="stop",
    )


def _empty_message(*, stop_reason: str = "stop") -> MessageDone:
    return MessageDone(
        message=Message(role="assistant"),
        usage=Usage(prompt_tokens=2, completion_tokens=0),
        stop_reason=stop_reason,
    )


class _InteractionParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pass


class _InteractionTool:
    name: ClassVar[str] = "request_user_detail"
    description: ClassVar[str] = "Request one exact user detail."
    Params: ClassVar[type[BaseModel]] = _InteractionParams
    risk: ClassVar[Risk] = "read_only"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del params, ctx
        return ToolResult(
            content="waiting for exact user input",
            user_interaction=UserInteractionRequest(
                kind="guardrail_input",
                correlation_id="draft_test:1",
                prompt="What is the exact arrival window?",
            ),
        )


class _NestedParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class _StructuredMutationParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    payload: _NestedParams


class _StructuredMutationTool:
    name: ClassVar[str] = "structured_mutation"
    description: ClassVar[str] = "Mutate using one structured payload."
    Params: ClassVar[type[BaseModel]] = _StructuredMutationParams
    risk: ClassVar[Risk] = "mutating"

    def __init__(self) -> None:
        self.received: list[dict[str, object]] = []

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        parsed = _StructuredMutationParams.model_validate(params)
        self.received.append(parsed.model_dump(mode="python"))
        return ToolResult(content="performed")


class _PreparedFileParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str


class _PreparedFileEffect(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str
    identity: EffectIdentity
    permission_summary: str | None
    content: bytes


class _PreparedFileTool:
    name: ClassVar[str] = "prepared_file_send"
    description: ClassVar[str] = "Send the exact reviewed file bytes."
    Params: ClassVar[type[BaseModel]] = _PreparedFileParams
    risk: ClassVar[Risk] = "mutating"
    effect_kind: ClassVar[str] = "external"

    def __init__(self) -> None:
        self.prepare_count = 0
        self.dispatched: list[bytes] = []

    async def prepare_effect(self, args: dict[str, object], ctx: ToolContext) -> PreparedEffect:
        del ctx
        self.prepare_count += 1
        path = Path(str(args["path"]))
        content = path.read_bytes()
        return _PreparedFileEffect(
            tool_name=self.name,
            identity=make_effect_identity(
                operation=self.name,
                target=path.name,
                occurrence="test",
                summary="Send reviewed bytes",
            ),
            permission_summary=f"Send {len(content)} reviewed bytes",
            content=content,
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del params, ctx
        raise AssertionError("ordinary dispatch must not run for a prepared effect")

    async def run_prepared(
        self,
        params: BaseModel,
        prepared: PreparedEffect,
        ctx: ToolContext,
    ) -> ToolResult:
        del params, ctx
        assert isinstance(prepared, _PreparedFileEffect)
        self.dispatched.append(prepared.content)
        return ToolResult(
            content="sent",
            effect_receipt=EffectReceipt(disposition="performed"),
        )


class _MediaParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    index: int


class _MediaTool:
    name: ClassVar[str] = "synthetic_media"
    description: ClassVar[str] = "Return one already-admitted synthetic image."
    Params: ClassVar[type[BaseModel]] = _MediaParams
    risk: ClassVar[Risk] = "read_only"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        parsed = _MediaParams.model_validate(params)
        image = ImagePart(
            artifact=MediaArtifactRef(
                id=f"media_{parsed.index:032x}",
                byte_count=10,
                sha256=f"{parsed.index:064x}",
                width=2,
                height=2,
                source_label=ProfileLabel.owned_by("personal"),
            )
        )
        if parsed.index == 1:
            await asyncio.sleep(0.01)
        return ToolResult(content=f"media {parsed.index}", follow_up_media=[image])


@pytest.mark.asyncio
async def test_foreground_prepared_effect_dispatches_the_bytes_reviewed_once(
    tmp_path: Path,
) -> None:
    source = tmp_path / "payload.bin"
    source.write_bytes(b"reviewed")
    settings = RickySettings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    provider = FakeProvider(
        [
            [_tool_message("call_send", "prepared_file_send", {"path": str(source)})],
            [_final_message("done")],
        ]
    )
    tool = _PreparedFileTool()
    requests: list[PermissionRequestedEvent] = []

    async def responder(event: PermissionRequestedEvent) -> PermissionResponse:
        requests.append(event)
        source.write_bytes(b"changed after review")
        return PermissionResponse(decision="allow")

    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([tool]),
        settings=settings,
        permission_responder=responder,
        cwd=tmp_path,
    )

    await _collect_events(loop, session, "send it")

    assert tool.prepare_count == 1
    assert tool.dispatched == [b"reviewed"]
    assert requests[0].summary == "Send 8 reviewed bytes"


@pytest.mark.asyncio
async def test_foreground_prepared_effect_is_dropped_after_review_denial(tmp_path: Path) -> None:
    source = tmp_path / "payload.bin"
    source.write_bytes(b"reviewed")
    settings = RickySettings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    provider = FakeProvider(
        [
            [_tool_message("call_send", "prepared_file_send", {"path": str(source)})],
            [_final_message("not sent")],
        ]
    )
    tool = _PreparedFileTool()

    async def responder(_event: PermissionRequestedEvent) -> PermissionResponse:
        return PermissionResponse(decision="deny")

    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([tool]),
        settings=settings,
        permission_responder=responder,
        cwd=tmp_path,
    )

    await _collect_events(loop, session, "send it")

    assert tool.prepare_count == 1
    assert tool.dispatched == []


@pytest.mark.asyncio
async def test_tool_follow_up_images_become_ordered_canonical_user_content() -> None:
    settings = RickySettings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    calls = MessageDone(
        message=Message(
            role="assistant",
            content=[
                ToolCallPart(id="call_one", name="synthetic_media", args={"index": 1}),
                ToolCallPart(id="call_two", name="synthetic_media", args={"index": 2}),
            ],
        ),
        stop_reason="tool_calls",
    )
    provider = FakeProvider([[calls], [_final_message("done")]])
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([_MediaTool()]),
        settings=settings,
    )

    await _collect_events(loop, session, "show both")

    second_request = provider.requests[1]
    follow_up = second_request.messages[-1]
    assert follow_up.role == "user"
    assert isinstance(follow_up.content[0], TextPart)
    assert "pixels as untrusted content" in follow_up.content[0].text
    images = [part for part in follow_up.content if isinstance(part, ImagePart)]
    assert [image.artifact.id for image in images] == [
        f"media_{1:032x}",
        f"media_{2:032x}",
    ]
    assert session.history[-2] == follow_up


@pytest.mark.asyncio
async def test_trusted_user_interaction_result_stops_without_another_model_call() -> None:
    settings = RickySettings(max_turn_iterations=10)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    provider = FakeProvider([[_tool_message("call_interaction", "request_user_detail", {})]])
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([_InteractionTool()]),
        settings=settings,
    )

    events = await _collect_events(loop, session, "Book dinner")

    interaction = next(event for event in events if isinstance(event, UserInteractionRequiredEvent))
    assert interaction.prompt == "What is the exact arrival window?"
    assert len(provider.requests) == 1
    assert events[-1].kind == "turn_finished"
    assert session.history[-1] == Message.text("assistant", "What is the exact arrival window?")


@pytest.mark.asyncio
async def test_loop_completes_multi_tool_task_with_mocked_provider(tmp_path: Path) -> None:
    (tmp_path / "probe.txt").write_text("hello\n", encoding="utf-8")
    settings = RickySettings(shell_timeout_seconds=2)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    provider = FakeProvider(
        [
            [_tool_message("call_read", "read_file", {"path": "probe.txt"})],
            [_tool_message("call_shell", "run_shell", {"command": "printf shell-ok"})],
            [TextDelta(delta="done"), _final_message("done")],
        ]
    )

    async def responder(_event: PermissionRequestedEvent) -> PermissionResponse:
        # run_shell explicitly allows a whole-tool session grant.
        return PermissionResponse(decision="allow", grant="tool")

    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(builtin_tools()),
        settings=settings,
        permission_responder=responder,
        cwd=tmp_path,
    )

    events = await _collect_events(loop, session, "inspect and run")
    kinds = [event.kind for event in events]

    assert kinds[0:2] == ["session_started", "turn_started"]
    assert kinds.count("context_assembled") == 3
    assert "permission_requested" in kinds
    assert kinds[-1] == "turn_finished"
    assert session.history[0] == Message.text("user", "inspect and run")
    assert session.history[-1] == Message.text("assistant", "done")
    assert len(session.permission_grants) == 1
    assert session.permission_grants[0].tool_name == "run_shell"
    assert session.permission_grants[0].params_equal == {}
    assert session.cumulative_usage.total_tokens == 9
    read_result = session.history[2].content[0]
    shell_result = session.history[4].content[0]
    assert isinstance(read_result, ToolResultPart)
    assert isinstance(shell_result, ToolResultPart)
    assert "1: hello" in read_result.content
    assert "shell-ok" in shell_result.content


@pytest.mark.asyncio
async def test_loop_recovers_from_empty_completion_after_tool_result(tmp_path: Path) -> None:
    (tmp_path / "probe.txt").write_text("hello\n", encoding="utf-8")
    settings = RickySettings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    provider = FakeProvider(
        [
            [_tool_message("call_read", "read_file", {"path": "probe.txt"})],
            [_empty_message()],
            [TextDelta(delta="done"), _final_message("done")],
        ]
    )
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(builtin_tools()),
        settings=settings,
        cwd=tmp_path,
    )

    events = await _collect_events(loop, session, "inspect the file")

    responses = [event for event in events if event.kind == "llm_response_finished"]
    assert len(provider.requests) == 3
    recovery_text = "\n".join(
        part.text
        for message in provider.requests[2].messages
        for part in message.content
        if isinstance(part, TextPart)
    )
    assert "previous model response" in recovery_text.lower()
    assert [event.empty for event in responses] == [False, True, False]
    assert [event.tool_call_count for event in responses] == [1, 0, 0]
    assert session.history[0] == Message.text("user", "inspect the file")
    assert session.history[-1] == Message.text("assistant", "done")
    assert all(message.content for message in session.history)
    assert events[-1].kind == "turn_finished"
    assert events[-1].error is None


@pytest.mark.asyncio
async def test_loop_fails_visibly_when_empty_completions_exhaust_bound(tmp_path: Path) -> None:
    settings = RickySettings(max_turn_iterations=2)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    provider = FakeProvider([[_empty_message()], [_empty_message()]])
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(builtin_tools()),
        settings=settings,
        cwd=tmp_path,
    )

    events = await _collect_events(loop, session, "answer me")

    assert len(provider.requests) == 2
    assert session.history == []
    assert events[-2].kind == "agent_error"
    assert events[-2].error_type == "MaxIterations"
    assert events[-1].kind == "turn_finished"
    assert (
        events[-1].error == "maximum turn iterations exceeded: 2 (model returned an empty response)"
    )


@pytest.mark.asyncio
async def test_denied_tool_call_returns_model_visible_result(tmp_path: Path) -> None:
    settings = RickySettings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    provider = FakeProvider(
        [
            [_tool_message("call_shell", "run_shell", {"command": "printf nope"})],
            [_final_message("permission noted")],
        ]
    )

    async def responder(_event: PermissionRequestedEvent) -> PermissionResponse:
        return PermissionResponse(decision="deny")

    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(builtin_tools()),
        settings=settings,
        permission_responder=responder,
        cwd=tmp_path,
    )

    events = await _collect_events(loop, session, "run shell")

    assert "permission_decided" in [event.kind for event in events]
    denied_result = session.history[2].content[0]
    provider_result = provider.requests[1].messages[-1].content[0]
    assert isinstance(denied_result, ToolResultPart)
    assert isinstance(provider_result, ToolResultPart)
    assert denied_result.is_error is True
    assert denied_result.content == "permission denied by user"
    assert provider_result.content == "permission denied by user"


@pytest.mark.asyncio
async def test_scoped_grant_is_honored_across_identity_params(tmp_path: Path) -> None:
    # write_file declares a {path} scope, so remembering it once should cover a
    # later write to the *same* path even though the content (identity) differs.
    settings = RickySettings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    provider = FakeProvider(
        [
            [_tool_message("call_one", "write_file", {"path": "notes.txt", "content": "one"})],
            [_tool_message("call_two", "write_file", {"path": "notes.txt", "content": "two"})],
            [_final_message("done")],
        ]
    )
    asks = 0

    async def responder(_event: PermissionRequestedEvent) -> PermissionResponse:
        nonlocal asks
        asks += 1
        return PermissionResponse(decision="allow", grant="scoped")

    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(builtin_tools()),
        settings=settings,
        permission_responder=responder,
        cwd=tmp_path,
    )

    await _collect_events(loop, session, "write it twice")

    assert asks == 1
    assert len(session.permission_grants) == 1
    grant = session.permission_grants[0]
    assert grant.tool_name == "write_file"
    assert grant.params_equal == {"path": str(tmp_path / "notes.txt")}
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "two"


@pytest.mark.asyncio
async def test_run_shell_session_grant_allows_later_commands_without_asking(
    tmp_path: Path,
) -> None:
    settings = RickySettings(shell_timeout_seconds=2)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    provider = FakeProvider(
        [
            [_tool_message("call_one", "run_shell", {"command": "printf one"})],
            [_tool_message("call_two", "run_shell", {"command": "printf two"})],
            [_final_message("done")],
        ]
    )
    requests: list[PermissionRequestedEvent] = []

    async def responder(event: PermissionRequestedEvent) -> PermissionResponse:
        requests.append(event)
        return PermissionResponse(decision="allow", grant="tool")

    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(builtin_tools()),
        settings=settings,
        permission_responder=responder,
        cwd=tmp_path,
    )

    await _collect_events(loop, session, "run two shell commands")

    assert len(requests) == 1
    assert [(option.id, option.label) for option in requests[0].offered_grants] == [
        ("tool", "all run_shell (any params)")
    ]
    assert len(session.permission_grants) == 1
    grant = session.permission_grants[0]
    assert grant.tool_name == "run_shell"
    assert grant.params_equal == {}
    assert grant.label == "all run_shell (any params)"
    shell_results = [
        part
        for message in session.history
        for part in message.content
        if isinstance(part, ToolResultPart)
    ]
    assert "one" in shell_results[0].content
    assert "two" in shell_results[1].content


def test_grant_candidates_reject_empty_scoped_projection() -> None:
    call = ToolCallPart(id="call_trash", name="gmail_trash", args={"message_id": "m1"})

    scoped_only = AgentLoop._grant_candidates(
        call,
        GrantScope(params_equal={}, label="gmail_trash in this scope"),
    )
    unconstrained = AgentLoop._grant_candidates(
        call,
        GrantScope(
            params_equal={},
            label="gmail_trash in this scope",
            allow_unconstrained=True,
        ),
    )

    assert scoped_only == []
    assert [option.id for option, _grant in unconstrained] == ["tool"]
    assert unconstrained[0][1].params_equal == {}


@pytest.mark.asyncio
async def test_loop_stops_at_max_iterations(tmp_path: Path) -> None:
    settings = RickySettings(max_turn_iterations=1)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    provider = FakeProvider([[_tool_message("call_read", "read_file", {"path": "missing.txt"})]])
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(builtin_tools()),
        settings=settings,
        cwd=tmp_path,
    )

    events = await _collect_events(loop, session, "keep going")

    assert events[-2].kind == "agent_error"
    assert events[-1].kind == "turn_finished"
    assert events[-1].error == "maximum turn iterations exceeded: 1"


@pytest.mark.asyncio
async def test_normalized_arguments_are_used_for_permission_and_execution(
    tmp_path: Path,
) -> None:
    settings = RickySettings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    tool = _StructuredMutationTool()
    requests: list[PermissionRequestedEvent] = []

    async def responder(event: PermissionRequestedEvent) -> PermissionResponse:
        requests.append(event)
        return PermissionResponse(decision="allow")

    provider = FakeProvider(
        [
            [
                _tool_message(
                    "call_structured",
                    "structured_mutation",
                    {"payload": '{"value":"canonical"}'},
                )
            ],
            [_final_message("done")],
        ]
    )
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([tool]),
        settings=settings,
        permission_responder=responder,
        cwd=tmp_path,
    )

    events = await _collect_events(loop, session, "perform it")

    assert [event.kind for event in events].count("tool_call_normalized") == 1
    assert requests[0].args == {"payload": {"value": "canonical"}}
    assert tool.received == [{"payload": {"value": "canonical"}}]


@pytest.mark.asyncio
async def test_invalid_arguments_return_repair_feedback_then_allow_corrected_call(
    tmp_path: Path,
) -> None:
    settings = RickySettings(max_turn_iterations=10)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    (tmp_path / "note.txt").write_text("fixed", encoding="utf-8")
    provider = FakeProvider(
        [
            [_tool_message("call_bad", "read_file", {})],
            [_tool_message("call_fixed", "read_file", {"path": "note.txt"})],
            [_final_message("done")],
        ]
    )
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(builtin_tools()),
        settings=settings,
        cwd=tmp_path,
    )

    events = await _collect_events(loop, session, "read it")

    kinds = [event.kind for event in events]
    assert kinds.count("tool_call_rejected") == 1
    assert kinds.count("tool_call_started") == 1
    assert kinds.count("tool_call_finished") == 1
    repair_result = session.history[2].content[0]
    assert isinstance(repair_result, ToolResultPart)
    assert repair_result.is_error is True
    assert "Correct the arguments and retry" in repair_result.content
    assert events[-1].kind == "turn_finished"
    assert events[-1].error is None


@pytest.mark.asyncio
async def test_repeated_identical_invalid_call_stops_at_repair_limit(
    tmp_path: Path,
) -> None:
    settings = RickySettings(max_turn_iterations=50)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    provider = FakeProvider(
        [
            [_tool_message("call_bad_1", "read_file", {})],
            [_tool_message("call_bad_2", "read_file", {})],
            [_final_message("must not be requested")],
        ]
    )
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(builtin_tools()),
        settings=settings,
        cwd=tmp_path,
    )

    events = await _collect_events(loop, session, "read it")

    assert len(provider.requests) == 2
    assert events[-2].kind == "agent_error"
    assert events[-2].error_type == "ToolArgumentRepairLimit"
    assert events[-1].kind == "turn_finished"
    assert events[-1].iterations == 2


@pytest.mark.asyncio
async def test_provider_malformed_argument_json_is_rejected_without_dispatch(
    tmp_path: Path,
) -> None:
    settings = RickySettings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    malformed = MessageDone(
        message=Message(
            role="assistant",
            content=[
                ToolCallPart(
                    id="call_bad_json",
                    name="read_file",
                    args={},
                    argument_error="malformed JSON",
                )
            ],
        ),
        stop_reason="tool_calls",
    )
    provider = FakeProvider([[malformed], [_final_message("corrected later")]])
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(builtin_tools()),
        settings=settings,
        cwd=tmp_path,
    )

    events = await _collect_events(loop, session, "read it")

    rejected = next(event for event in events if event.kind == "tool_call_rejected")
    assert rejected.reason == "malformed_json"
    assert not any(event.kind == "tool_call_started" for event in events)
    result = session.history[2].content[0]
    assert isinstance(result, ToolResultPart)
    assert "provider returned malformed JSON" in result.content


@pytest.mark.asyncio
async def test_cancellation_emits_interrupted_turn_finished(tmp_path: Path) -> None:
    settings = RickySettings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    provider = FakeProvider([[TextDelta(delta="partial"), asyncio.CancelledError()]])
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(builtin_tools()),
        settings=settings,
        cwd=tmp_path,
    )

    events = await _collect_events(loop, session, "cancel")

    assert [event.kind for event in events][-2:] == ["text_delta", "turn_finished"]
    interrupted = events[-1]
    assert interrupted.kind == "turn_finished"
    assert interrupted.interrupted is True
    assert session.history == []


@pytest.mark.asyncio
async def test_cancellation_during_permission_prompt_closes_dangling_tool_calls(
    tmp_path: Path,
) -> None:
    settings = RickySettings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    provider = FakeProvider([[_tool_message("call_shell", "run_shell", {"command": "printf hi"})]])

    async def responder(_event: PermissionRequestedEvent) -> PermissionResponse:
        raise asyncio.CancelledError()

    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(builtin_tools()),
        settings=settings,
        permission_responder=responder,
        cwd=tmp_path,
    )

    events = await _collect_events(loop, session, "run it")

    finished = events[-1]
    assert finished.kind == "turn_finished"
    assert finished.interrupted is True
    closing = session.history[-1].content[0]
    assert isinstance(closing, ToolResultPart)
    assert closing.call_id == "call_shell"
    assert closing.is_error is True


@pytest.mark.asyncio
async def test_cancellation_mid_tool_run_cancels_in_flight_tool(tmp_path: Path) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class SlowParams(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)

        pass

    class SlowTool:
        name: ClassVar[str] = "slow_tool"
        description: ClassVar[str] = "Sleep until cancelled."
        Params: ClassVar[type[BaseModel]] = SlowParams
        risk: ClassVar[Risk] = "read_only"

        async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
            _ = params, ctx
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return ToolResult(content="never")

    settings = RickySettings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    provider = FakeProvider([[_tool_message("call_slow", "slow_tool", {})]])
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([SlowTool()]),
        settings=settings,
        cwd=tmp_path,
    )
    events: list[AgentEvent] = []

    async def consume() -> None:
        async for event in loop.run_turn(session, "slow"):
            events.append(event)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert cancelled.is_set()
    finished = events[-1]
    assert finished.kind == "turn_finished"
    assert finished.interrupted is True
    closing = session.history[-1].content[0]
    assert isinstance(closing, ToolResultPart)
    assert closing.call_id == "call_slow"
    assert closing.is_error is True


@pytest.mark.asyncio
async def test_parallel_tool_calls_are_started_before_finishing(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
    settings = RickySettings()
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    provider = FakeProvider(
        [
            [
                MessageDone(
                    message=Message(
                        role="assistant",
                        content=[
                            ToolCallPart(id="call_a", name="read_file", args={"path": "a.txt"}),
                            ToolCallPart(id="call_b", name="read_file", args={"path": "b.txt"}),
                        ],
                    )
                )
            ],
            [_final_message("done")],
        ]
    )
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(builtin_tools()),
        settings=settings,
        cwd=tmp_path,
    )

    events = await _collect_events(loop, session, "read both")
    kinds = [event.kind for event in events]
    first_finished = kinds.index("tool_call_finished")
    started_before_finish = kinds[:first_finished].count("tool_call_started")

    assert started_before_finish == 2
    first_result = session.history[2].content[0]
    second_result = session.history[3].content[0]
    assert isinstance(first_result, ToolResultPart)
    assert isinstance(second_result, ToolResultPart)
    assert first_result.call_id == "call_a"
    assert second_result.call_id == "call_b"


class _RuntimeConflictParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int = 1


class _RuntimeConflictTool:
    name: ClassVar[str] = "runtime_conflict"
    description: ClassVar[str] = "Exercise classified runtime failures."
    Params: ClassVar[type[BaseModel]] = _RuntimeConflictParams
    risk: ClassVar[Risk] = "read_only"

    def __init__(self, results: list[ToolResult]) -> None:
        self.results = results
        self.calls = 0

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        self.calls += 1
        return self.results.pop(0)


def _runtime_conflict(revision: str = "2") -> ToolResult:
    return ToolResult(
        content="stale task revision",
        is_error=True,
        runtime_failure=ToolRuntimeFailure(
            kind="state_conflict",
            state_fingerprint=f"task:revision:{revision}",
            recovery="Use the held lease to complete the task instead of claiming again.",
        ),
    )


@pytest.mark.asyncio
async def test_repeated_runtime_conflicts_warn_then_stop(tmp_path: Path) -> None:
    settings = RickySettings(max_turn_iterations=100)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    tool = _RuntimeConflictTool([_runtime_conflict() for _ in range(4)])
    (tmp_path / "receipt.txt").write_text("effect already performed")
    provider = FakeProvider(
        [
            [_tool_message("a", tool.name, {})],
            [_tool_message("read", "read_file", {"path": "receipt.txt"})],
            [_tool_message("b", tool.name, {})],
            [_tool_message("c", tool.name, {})],
            [_final_message("must not run")],
        ]
    )
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([*builtin_tools(), tool]),
        settings=settings,
        cwd=tmp_path,
    )
    events = await _collect_events(loop, session, "finish task")
    assert tool.calls == 3
    assert len(provider.requests) == 4
    assert (
        "Another identical failure will stop this turn" in provider.requests[-1].model_dump_json()
    )
    assert "Use the held lease" in provider.requests[-1].model_dump_json()
    assert events[-2].kind == "agent_error"
    assert events[-2].error_type == "ToolRuntimeRepairLimit"
    assert events[-1].kind == "turn_finished"
    assert events[-1].iterations == 4
    assert len([m for m in session.history if m.role == "tool"]) == 4
    failures = [e for e in events if isinstance(e, ToolCallFinishedEvent) and e.runtime_failure]
    assert len(failures) == 3
    assert ToolCallFinishedEvent.model_validate_json(failures[0].model_dump_json()) == failures[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("reset", ["state", "success", "transient", "arguments"])
async def test_runtime_conflict_recovery_and_changed_state_remain_allowed(
    tmp_path: Path,
    reset: str,
) -> None:
    settings = RickySettings(max_turn_iterations=20)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    initial = [_runtime_conflict(), _runtime_conflict()]
    if reset == "state":
        results = [*initial, _runtime_conflict("3"), _runtime_conflict("2")]
    elif reset == "success":
        results = [*initial, ToolResult(content="progress recorded"), _runtime_conflict()]
    elif reset == "transient":
        results = [
            ToolResult(content="service temporarily unavailable", is_error=True) for _ in range(4)
        ]
    else:
        results = [*initial, _runtime_conflict()]
    tool = _RuntimeConflictTool(results)
    scripts: list[list[StreamEvent | BaseException]] = [
        [_tool_message(str(i), tool.name, {})] for i in range(len(results))
    ]
    if reset == "arguments":
        scripts[-1] = [_tool_message("recover", tool.name, {"expected_revision": 2})]
    scripts.append([_final_message("recovered")])
    provider = FakeProvider(scripts)
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([*builtin_tools(), tool]),
        settings=settings,
        cwd=tmp_path,
    )
    events = await _collect_events(loop, session, "finish task")
    assert events[-1].kind == "turn_finished"
    assert events[-1].error is None
    assert not any(e.kind == "agent_error" for e in events)


def test_runtime_failure_contract_rejects_unsafe_outcomes_and_reads_old_events() -> None:
    failure = _runtime_conflict().runtime_failure
    assert failure is not None
    assert ToolRuntimeFailure.model_validate_json(failure.model_dump_json()) == failure
    with pytest.raises(ValidationError):
        ToolRuntimeFailure(kind="state_conflict", state_fingerprint=2, recovery="read task")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ToolResult(content="success", runtime_failure=failure)
    for disposition in ("performed", "in_doubt"):
        with pytest.raises(ValidationError):
            ToolResult(
                content="error",
                is_error=True,
                runtime_failure=failure,
                effect_receipt=EffectReceipt(disposition=disposition),  # type: ignore[arg-type]
            )
    old_event = ToolCallFinishedEvent.model_validate_json(
        '{"turn_id":"t","call_id":"c","tool_name":"read_file","is_error":true,"content_chars":5}'
    )
    assert old_event.runtime_failure is None
    assert ToolResult.model_validate_json('{"content":"old"}').runtime_failure is None


@pytest.mark.asyncio
async def test_duplicate_runtime_conflicts_allow_feedback_before_termination(
    tmp_path: Path,
) -> None:
    settings = RickySettings(max_turn_iterations=100)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    tool = _RuntimeConflictTool([_runtime_conflict() for _ in range(4)])
    batch = MessageDone(
        message=Message(
            role="assistant",
            content=[ToolCallPart(id=f"batch_{i}", name=tool.name, args={}) for i in range(3)],
        ),
        usage=Usage(),
        stop_reason="tool_calls",
    )
    provider = FakeProvider(
        [
            [batch],
            [_tool_message("next", tool.name, {})],
            [_final_message("recovered")],
        ]
    )
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([tool]),
        settings=settings,
        cwd=tmp_path,
    )
    events = await _collect_events(loop, session, "finish task")
    assert tool.calls == 4
    assert events[-1].kind == "turn_finished"
    assert events[-1].error is None
    assert (
        "Another identical failure will stop this turn" in provider.requests[-1].model_dump_json()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reset", ["success", "state"])
async def test_same_response_runtime_conflict_reset_drops_pending_failure(
    tmp_path: Path,
    reset: str,
) -> None:
    settings = RickySettings(max_turn_iterations=100)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    reset_result = ToolResult(content="done") if reset == "success" else _runtime_conflict("3")
    tool = _RuntimeConflictTool(
        [
            _runtime_conflict(),
            _runtime_conflict(),
            reset_result,
            _runtime_conflict(),
            _runtime_conflict(),
        ]
    )
    batch = MessageDone(
        message=Message(
            role="assistant",
            content=[
                ToolCallPart(id="batch_failure", name=tool.name, args={}),
                ToolCallPart(id="batch_reset", name=tool.name, args={}),
            ],
        ),
        usage=Usage(),
        stop_reason="tool_calls",
    )
    provider = FakeProvider(
        [
            [_tool_message("initial", tool.name, {})],
            [batch],
            [_tool_message("after_reset_1", tool.name, {})],
            [_tool_message("after_reset_2", tool.name, {})],
            [_final_message("recovered")],
        ]
    )
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([tool]),
        settings=settings,
        cwd=tmp_path,
    )
    events = await _collect_events(loop, session, "finish task")
    assert tool.calls == 5
    assert events[-1].kind == "turn_finished"
    assert events[-1].error is None
