"""Tests for the isolated Claude Code CLI provider adapter."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tomllib
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict
from typer.testing import CliRunner

from ricky.agent import AgentLoop, AgentSession
from ricky.agent.events import PermissionRequestedEvent
from ricky.config import ClaudeCodeSettings, ProvidersSettings, RickySettings
from ricky.interfaces.cli.app import app
from ricky.llm.claude_code import (
    ClaudeCodeProvider,
    ClaudeCodeSessionState,
    ToolProtocolParser,
    parse_tool_call_suffix,
    render_tool_call,
    render_tool_protocol,
    to_claude_delta_prompt,
    to_claude_prompt,
)
from ricky.llm.provider import ResolvedMedia
from ricky.llm.types import (
    AuthError,
    CompletionRequest,
    ImagePart,
    MediaArtifactRef,
    Message,
    MessageDone,
    ProviderError,
    RateLimitError,
    TextDelta,
    TextPart,
    ThinkingDelta,
    ThinkingPart,
    ToolCallDelta,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
    TransportError,
)
from ricky.permissions import PermissionResponse
from ricky.profiles import ProfileLabel
from ricky.tools import Risk, ToolContext, ToolRegistry, ToolResult
from ricky.tools.builtin.tasks import UpdateTasksTool

_FAKE_CLAUDE = r"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import time

record_path = Path(os.environ["RICKY_FAKE_CLAUDE_RECORD"])
response_dir = Path(os.environ["RICKY_FAKE_CLAUDE_RESPONSES"])
try:
    invocation = len(record_path.read_text(encoding="utf-8").splitlines())
except FileNotFoundError:
    invocation = 0
prompt = sys.stdin.read()
record = {
    "argv": sys.argv[1:],
    "stdin": prompt,
    "cwd": os.getcwd(),
    "pid": os.getpid(),
    "api_key_present": "ANTHROPIC_API_KEY" in os.environ,
    "auth_token_present": "ANTHROPIC_AUTH_TOKEN" in os.environ,
}
with record_path.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(record) + "\n")

sleep_seconds = float(os.environ.get("RICKY_FAKE_CLAUDE_SLEEP", "0"))
if sleep_seconds:
    time.sleep(sleep_seconds)

response_path = response_dir / f"{invocation}.jsonl"
line_delay = float(os.environ.get("RICKY_FAKE_CLAUDE_LINE_DELAY", "0"))
if response_path.exists():
    for line in response_path.read_text(encoding="utf-8").splitlines(keepends=True):
        if line_delay:
            time.sleep(line_delay)
        sys.stdout.write(line)
        sys.stdout.flush()
stderr = os.environ.get("RICKY_FAKE_CLAUDE_STDERR")
if stderr:
    sys.stderr.write(stderr)
sys.exit(int(os.environ.get("RICKY_FAKE_CLAUDE_EXIT", "0")))
"""


def _fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    executable = tmp_path / "claude-fake"
    executable.write_text(_FAKE_CLAUDE, encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    record = tmp_path / "invocations.jsonl"
    responses = tmp_path / "responses"
    responses.mkdir()
    monkeypatch.setenv("RICKY_FAKE_CLAUDE_RECORD", str(record))
    monkeypatch.setenv("RICKY_FAKE_CLAUDE_RESPONSES", str(responses))
    return executable, record, responses


def _settings(executable: Path, *, timeout: float = 5) -> RickySettings:
    return RickySettings(
        user_data_dir=str(executable.parent / "user-data"),
        project_data_dir=str(executable.parent / ".ricky"),
        request_timeout_seconds=timeout,
        providers=ProvidersSettings(claude_code=ClaudeCodeSettings(cli_path=str(executable))),
    )


def _write_response(path: Path, *events: dict[str, Any]) -> None:
    path.write_text(
        "".join(f"{json.dumps(event)}\n" for event in events),
        encoding="utf-8",
    )


def _text_delta(text: str) -> dict[str, Any]:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        },
    }


def _thinking_delta(text: str) -> dict[str, Any]:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": text},
        },
    }


def _result(
    *,
    result: str = "ok",
    subtype: str = "success",
    is_error: bool = False,
    stop_reason: str = "end_turn",
) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": result,
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 7, "output_tokens": 3},
    }


async def _events(provider: ClaudeCodeProvider, request: CompletionRequest) -> list[Any]:
    return [event async for event in provider.stream(request)]


def _request(*messages: Message, tools: list[ToolSpec] | None = None) -> CompletionRequest:
    return CompletionRequest(model="sonnet", messages=list(messages), tools=tools or [])


def _session_request(
    session_id: str,
    *messages: Message,
    model: str = "sonnet",
) -> CompletionRequest:
    return CompletionRequest(
        model=model,
        messages=list(messages),
        session_id=session_id,
    )


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _image_ref() -> MediaArtifactRef:
    return MediaArtifactRef(
        id="media_" + "e" * 32,
        byte_count=7,
        sha256="f" * 64,
        width=2,
        height=1,
        source_label=ProfileLabel.owned_by("personal"),
    )


@pytest.mark.asyncio
async def test_image_requests_use_stream_json_blocks_for_fresh_and_resumed_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, record, responses = _fake_cli(tmp_path, monkeypatch)
    _write_response(responses / "0.jsonl", _text_delta("first"), _result())
    _write_response(responses / "1.jsonl", _text_delta("second"), _result())

    class Resolver:
        async def resolve(self, reference: object) -> ResolvedMedia:
            assert reference == _image_ref()
            return ResolvedMedia(
                media_type="image/png",
                content=b"pngbody",
                sha256="f" * 64,
                width=2,
                height=1,
            )

    provider = ClaudeCodeProvider(_settings(executable), media_resolver=Resolver())
    image_message = Message(
        role="user",
        content=[
            TextPart(text="before"),
            ImagePart(artifact=_image_ref()),
            TextPart(text="after"),
        ],
    )
    first = _session_request("session_images", image_message)
    await _events(provider, first)
    second = _session_request(
        "session_images",
        image_message,
        Message.text("assistant", "first"),
        Message(
            role="user",
            content=[TextPart(text="again"), ImagePart(artifact=_image_ref())],
        ),
    )
    await _events(provider, second)
    await provider.aclose()

    invocations = _records(record)
    assert len(invocations) == 2
    for invocation in invocations:
        argv = invocation["argv"]
        assert argv[argv.index("--input-format") + 1] == "stream-json"
        envelope = json.loads(invocation["stdin"])
        blocks = envelope["message"]["content"]
        assert [block["type"] for block in blocks].count("image") >= 1
        images = [block for block in blocks if block["type"] == "image"]
        assert all(image["source"]["data"] == "cG5nYm9keQ==" for image in images)
        assert "/private" not in invocation["stdin"]
    assert "--session-id" in invocations[0]["argv"]
    assert "--resume" in invocations[1]["argv"]


def test_tool_protocol_render_is_deterministic() -> None:
    tools = [
        ToolSpec(
            name="read_file",
            description="Read one file",
            parameters={"required": ["path"], "type": "object"},
        )
    ]

    rendered = render_tool_protocol(tools)

    assert "- read_file: Read one file" in rendered
    assert 'parameters: {"required":["path"],"type":"object"}' in rendered
    assert rendered.endswith("</ricky_tool_protocol>")


def test_tool_protocol_preserves_provider_stringified_nested_tool_object() -> None:
    nested = json.dumps({"task_id": "task_" + "a" * 32, "profile": "personal"})
    suffix = "```tool_call\n" + json.dumps({"name": "probe", "args": {"payload": nested}}) + "\n```"

    calls = parse_tool_call_suffix(suffix, id_factory=lambda: "call_nested")

    assert calls is not None
    assert len(calls) == 1
    assert calls[0].name == "probe"
    assert calls[0].args == {"payload": nested}


def test_tool_protocol_marks_non_object_top_level_arguments_for_repair() -> None:
    calls = parse_tool_call_suffix(
        '```tool_call\n{"name":"probe","args":"not an object"}\n```',
        id_factory=lambda: "call_bad_args",
    )

    assert calls is not None
    assert calls[0].name == "probe"
    assert calls[0].args == {}
    assert calls[0].argument_error == "non-object JSON"


def test_prompt_render_extracts_system_and_handles_empty_history() -> None:
    request = _request(
        Message.text("system", "base system"),
        Message.text("system", "active skill"),
        Message.text("user", "hello"),
        tools=[ToolSpec(name="probe", description="Probe", parameters={})],
    )

    system, prompt = to_claude_prompt(request)

    assert system.startswith("base system\n\nactive skill")
    assert "<ricky_tool_protocol>" in system
    assert prompt == "hello"


def test_prompt_render_replays_tool_history_and_adds_continuation() -> None:
    request = _request(
        Message.text("system", "system"),
        Message.text("user", "inspect"),
        Message(
            role="assistant",
            content=[
                ThinkingPart(text="do not replay"),
                ToolCallPart(id="call_one", name="probe", args={"value": 1}),
            ],
        ),
        Message(
            role="tool",
            content=[ToolResultPart(call_id="call_one", content="done")],
        ),
    )

    _, prompt = to_claude_prompt(request)

    assert prompt.index("[user] inspect") < prompt.index("```tool_call")
    assert prompt.index("```tool_call") < prompt.index("[tool_result for call_one] done")
    assert "do not replay" not in prompt
    assert "If requested work remains, call the necessary tools now." in prompt
    assert prompt.endswith(
        "Finish only when the requested outcome is verified complete or you are concretely blocked."
    )


def test_delta_render_sends_only_new_user_or_trailing_tool_results() -> None:
    prior = Message.text("assistant", "prior answer")
    _, user_delta = to_claude_delta_prompt(
        _request(
            Message.text("system", "system"),
            Message.text("user", "old question"),
            prior,
            Message.text("user", "new question"),
        )
    )
    _, tool_delta = to_claude_delta_prompt(
        _request(
            Message.text("system", "system"),
            Message.text("user", "inspect"),
            Message(
                role="assistant",
                content=[ToolCallPart(id="a", name="one", args={})],
            ),
            Message(role="tool", content=[ToolResultPart(call_id="a", content="first")]),
            Message(role="tool", content=[ToolResultPart(call_id="b", content="second")]),
        )
    )

    assert user_delta == "new question"
    assert "old question" not in user_delta
    assert "[tool_result for a] first" in tool_delta
    assert "[tool_result for b] second" in tool_delta
    assert "inspect" not in tool_delta
    assert "Inspect the latest tool result(s)" in tool_delta


def test_rendered_assistant_call_round_trips_through_parser() -> None:
    rendered = render_tool_call(
        ToolCallPart(id="old-id", name="read_file", args={"path": "README.md"})
    )
    ids = iter(["call_new"])
    parser = ToolProtocolParser(id_factory=lambda: next(ids))

    assert parser.feed(rendered[:8]) == []
    assert parser.feed(rendered[8:]) == []
    events = parser.finish()

    assert events == [
        ToolCallDelta(
            index=0,
            id="call_new",
            name="read_file",
            args_delta='{"path":"README.md"}',
        )
    ]


def test_parser_accepts_parallel_blocks_and_holds_partial_fence_lines() -> None:
    ids = iter(["call_a", "call_b"])
    parser = ToolProtocolParser(id_factory=lambda: next(ids))

    first = parser.feed("I will check.\n```tool")
    second = parser.feed(
        '_call\n{"name":"one","args":{}}\n```\n\n```tool_call\n{"name":"two","args":{"x":2}}\n```'
    )
    final = parser.finish()

    assert first == [TextDelta(delta="I will check.\n")]
    assert second == []
    assert [event.name for event in final if isinstance(event, ToolCallDelta)] == [
        "one",
        "two",
    ]


def test_malformed_closed_block_is_a_non_runnable_synthetic_call() -> None:
    calls = parse_tool_call_suffix(
        '```tool_call\n{"name":"run_shell","args":\n```',
        id_factory=lambda: "call_bad",
    )

    assert calls is not None
    assert calls[0].id == "call_bad"
    assert calls[0].name == "__ricky_malformed_tool_call__"
    assert calls[0].args == {"intended_name": "run_shell"}


def test_unterminated_block_parses_if_valid_and_otherwise_flushes_as_text() -> None:
    valid = parse_tool_call_suffix(
        '```tool_call\n{"name":"probe","args":{}}',
        id_factory=lambda: "call_ok",
    )
    parser = ToolProtocolParser(id_factory=lambda: "unused")
    malformed = '```tool_call\n{"name":"probe"'
    parser.feed(malformed)

    assert valid is not None and valid[0].name == "probe"
    assert parser.finish() == [TextDelta(delta=malformed)]
    assert parser.tool_calls == []


def test_partial_prose_streams_before_its_newline_arrives() -> None:
    parser = ToolProtocolParser()

    first = parser.feed("A long single-line paragraph")
    second = parser.feed(" that keeps going")
    third = parser.feed(" and ends.\nnext")
    final = parser.finish()

    assert first == [TextDelta(delta="A long single-line paragraph")]
    assert second == [TextDelta(delta=" that keeps going")]
    assert third == [TextDelta(delta=" and ends.\n"), TextDelta(delta="next")]
    assert final == []
    assert parser.text == "A long single-line paragraph that keeps going and ends.\nnext"


def test_partial_fence_prefix_is_held_only_until_disambiguated() -> None:
    ids = iter(["call_ok"])
    held = ToolProtocolParser(id_factory=lambda: next(ids))
    assert held.feed("```tool") == []
    assert held.feed('_call\n{"name":"probe","args":{}}\n```') == []
    assert [event.name for event in held.finish() if isinstance(event, ToolCallDelta)] == ["probe"]

    diverged = ToolProtocolParser()
    assert diverged.feed("```tool") == []
    assert diverged.feed("box rocks") == [TextDelta(delta="```toolbox rocks")]


def test_fence_example_followed_by_prose_remains_text() -> None:
    text = 'Example:\n```tool_call\n{"name":"probe","args":{}}\n```\nThis is only an example.\n'
    parser = ToolProtocolParser()

    events = [*parser.feed(text), *parser.finish()]

    assert "".join(event.delta for event in events if isinstance(event, TextDelta)) == text
    assert parser.tool_calls == []


@pytest.mark.asyncio
async def test_subprocess_stream_mapping_isolation_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, record, responses = _fake_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "must-not-leak")
    _write_response(
        responses / "0.jsonl",
        {"type": "system", "subtype": "init"},
        _thinking_delta("considering"),
        _text_delta("hello\n"),
        _text_delta("world"),
        _result(),
    )
    provider = ClaudeCodeProvider(_settings(executable))
    neutral_cwd = provider._cwd
    request = _request(
        Message.text("system", "be concise"),
        Message.text("user", "hi"),
        tools=[ToolSpec(name="probe", description="Probe", parameters={})],
    )

    events = await _events(provider, request)
    await provider.aclose()

    assert [event.delta for event in events if isinstance(event, ThinkingDelta)] == ["considering"]
    assert "".join(event.delta for event in events if isinstance(event, TextDelta)) == (
        "hello\nworld"
    )
    done = next(event for event in events if isinstance(event, MessageDone))
    assert done.message.content[0] == ThinkingPart(text="considering")
    assert done.message.content[1] == Message.text("assistant", "hello\nworld").content[0]
    assert done.usage.prompt_tokens == 7
    assert done.usage.completion_tokens == 3
    assert done.stop_reason == "stop"

    invocation = _records(record)[0]
    argv = invocation["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--max-turns") + 1] == "1"
    assert "be concise" in argv[argv.index("--system-prompt") + 1]
    assert invocation["stdin"] == "hi"
    assert invocation["cwd"] == str(neutral_cwd)
    assert invocation["api_key_present"] is False
    assert invocation["auth_token_present"] is False
    assert not neutral_cwd.exists()


@pytest.mark.asyncio
async def test_session_reuse_sends_full_then_delta_and_survives_provider_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, record, responses = _fake_cli(tmp_path, monkeypatch)
    for index, text in enumerate(("first", "second", "third")):
        _write_response(responses / f"{index}.jsonl", _text_delta(text), _result())
    settings = _settings(executable)
    session_id = "session_reuse"
    first_request = _session_request(
        session_id,
        Message.text("user", "earlier"),
        Message.text("assistant", "acknowledged"),
        Message.text("user", "first live prompt"),
    )
    second_request = _session_request(
        session_id,
        *first_request.messages,
        Message.text("assistant", "first"),
        Message.text("user", "second live prompt"),
    )

    provider = ClaudeCodeProvider(settings)
    await _events(provider, first_request)
    await _events(provider, second_request)
    await provider.aclose()
    restarted = ClaudeCodeProvider(settings)
    await _events(
        restarted,
        _session_request(
            session_id,
            *second_request.messages,
            Message.text("assistant", "second"),
            Message.text("user", "third live prompt"),
        ),
    )
    await restarted.aclose()

    invocations = _records(record)
    first_argv = invocations[0]["argv"]
    claude_session_id = first_argv[first_argv.index("--session-id") + 1]
    assert "--resume" not in first_argv
    assert "--no-session-persistence" not in first_argv
    assert "<conversation_history>" in invocations[0]["stdin"]
    assert "earlier" in invocations[0]["stdin"]
    for invocation, expected_prompt in zip(
        invocations[1:],
        ("second live prompt", "third live prompt"),
        strict=True,
    ):
        argv = invocation["argv"]
        assert argv[argv.index("--resume") + 1] == claude_session_id
        assert "--session-id" not in argv
        assert invocation["stdin"] == expected_prompt
        assert invocation["cwd"] == invocations[0]["cwd"]

    state_path = (
        Path(settings.user_data_dir)
        / "sessions"
        / session_id
        / "providers"
        / "claude-code"
        / "claude-code.json"
    )
    assert not (Path(settings.project_data_dir) / "sessions").exists()
    state = ClaudeCodeSessionState.model_validate_json(state_path.read_text(encoding="utf-8"))
    assert state.claude_session_id == claude_session_id
    assert state.model == "sonnet"


@pytest.mark.asyncio
async def test_rotation_and_model_change_create_fresh_sessions_from_full_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, record, responses = _fake_cli(tmp_path, monkeypatch)
    for index in range(3):
        _write_response(responses / f"{index}.jsonl", _text_delta("ok"), _result())
    settings = _settings(executable)
    session_id = "session_rotate"
    request = _session_request(session_id, Message.text("user", "before compaction"))
    provider = ClaudeCodeProvider(settings)

    await _events(provider, request)
    await provider.rotate(session_id)
    rotated_request = _session_request(
        session_id,
        Message.text("user", "retained tail"),
        Message.text("assistant", "retained answer"),
        Message.text("user", "after compaction"),
    )
    await _events(provider, rotated_request)
    await _events(provider, _session_request(session_id, *rotated_request.messages, model="opus"))
    await provider.aclose()

    invocations = _records(record)
    native_ids = [
        invocation["argv"][invocation["argv"].index("--session-id") + 1]
        for invocation in invocations
    ]
    assert len(set(native_ids)) == 3
    assert all("--resume" not in invocation["argv"] for invocation in invocations)
    assert "<conversation_history>" in invocations[1]["stdin"]
    assert "retained tail" in invocations[1]["stdin"]
    assert invocations[2]["argv"][invocations[2]["argv"].index("--model") + 1] == "opus"
    state_path = (
        Path(settings.user_data_dir)
        / "sessions"
        / session_id
        / "providers"
        / "claude-code"
        / "claude-code.json"
    )
    state = ClaudeCodeSessionState.model_validate_json(state_path.read_text(encoding="utf-8"))
    assert state.post_compaction is True
    assert state.model == "opus"


@pytest.mark.asyncio
async def test_resume_escape_hatch_preserves_stateless_full_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, record, responses = _fake_cli(tmp_path, monkeypatch)
    _write_response(responses / "0.jsonl", _text_delta("ok"), _result())
    settings = _settings(executable)
    providers = settings.providers.model_copy(
        update={
            "claude_code": settings.providers.claude_code.model_copy(
                update={"resume_sessions": False}
            )
        }
    )
    provider = ClaudeCodeProvider(settings.model_copy(update={"providers": providers}))

    await _events(
        provider,
        _session_request(
            "session_disabled",
            Message.text("user", "old"),
            Message.text("assistant", "answer"),
            Message.text("user", "new"),
        ),
    )
    await provider.aclose()

    invocation = _records(record)[0]
    assert "--no-session-persistence" in invocation["argv"]
    assert "--session-id" not in invocation["argv"]
    assert "--resume" not in invocation["argv"]
    assert "<conversation_history>" in invocation["stdin"]
    assert "old" in invocation["stdin"]


@pytest.mark.asyncio
async def test_failed_resumed_request_invalidates_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, _, responses = _fake_cli(tmp_path, monkeypatch)
    _write_response(responses / "0.jsonl", _text_delta("first"), _result())
    _write_response(
        responses / "1.jsonl",
        _result(result="unexpected failure", subtype="error_during_execution", is_error=True),
    )
    settings = _settings(executable)
    session_id = "session_failure"
    provider = ClaudeCodeProvider(settings)
    await _events(provider, _session_request(session_id, Message.text("user", "first")))

    with pytest.raises(ProviderError, match="unexpected failure"):
        await _events(
            provider,
            _session_request(
                session_id,
                Message.text("user", "first"),
                Message.text("assistant", "first"),
                Message.text("user", "second"),
            ),
        )
    await provider.aclose()

    state_path = (
        Path(settings.user_data_dir)
        / "sessions"
        / session_id
        / "providers"
        / "claude-code"
        / "claude-code.json"
    )
    assert not state_path.exists()


@pytest.mark.asyncio
async def test_cancelled_resumed_request_kills_process_and_invalidates_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, record, responses = _fake_cli(tmp_path, monkeypatch)
    _write_response(responses / "0.jsonl", _text_delta("first"), _result())
    settings = _settings(executable)
    session_id = "session_cancelled_resume"
    provider = ClaudeCodeProvider(settings)
    await _events(provider, _session_request(session_id, Message.text("user", "first")))
    monkeypatch.setenv("RICKY_FAKE_CLAUDE_SLEEP", "5")
    task = asyncio.create_task(
        _events(
            provider,
            _session_request(
                session_id,
                Message.text("user", "first"),
                Message.text("assistant", "first"),
                Message.text("user", "second"),
            ),
        )
    )
    for _ in range(100):
        if len(_records(record)) == 2:
            break
        await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await provider.aclose()

    pid = _records(record)[1]["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    state_path = (
        Path(settings.user_data_dir)
        / "sessions"
        / session_id
        / "providers"
        / "claude-code"
        / "claude-code.json"
    )
    assert not state_path.exists()


@pytest.mark.asyncio
async def test_subprocess_emits_tool_call_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, _, responses = _fake_cli(tmp_path, monkeypatch)
    _write_response(
        responses / "0.jsonl",
        _text_delta('```tool_call\n{"name":"read_file","args":{"path":"README.md"}}\n```'),
        _result(),
    )
    provider = ClaudeCodeProvider(_settings(executable))

    events = await _events(provider, _request(Message.text("user", "read it")))
    await provider.aclose()

    tool_delta = next(event for event in events if isinstance(event, ToolCallDelta))
    done = next(event for event in events if isinstance(event, MessageDone))
    call = next(part for part in done.message.content if isinstance(part, ToolCallPart))
    assert tool_delta.name == "read_file"
    assert call.name == "read_file"
    assert call.args == {"path": "README.md"}
    assert done.stop_reason == "tool_calls"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("detail", "error_type"),
    [
        ("Please login with OAuth", AuthError),
        ("Five-hour usage limit reached", RateLimitError),
        ("unexpected failure", ProviderError),
    ],
)
async def test_error_results_map_to_typed_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    detail: str,
    error_type: type[ProviderError],
) -> None:
    executable, _, responses = _fake_cli(tmp_path, monkeypatch)
    _write_response(
        responses / "0.jsonl",
        _result(result=detail, subtype="error_during_execution", is_error=True),
    )
    provider = ClaudeCodeProvider(_settings(executable))

    with pytest.raises(error_type):
        await _events(provider, _request(Message.text("user", "hi")))
    await provider.aclose()


@pytest.mark.asyncio
async def test_nonzero_exit_and_malformed_stream_are_transport_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, _, responses = _fake_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("RICKY_FAKE_CLAUDE_EXIT", "7")
    monkeypatch.setenv("RICKY_FAKE_CLAUDE_STDERR", "diagnostic tail")
    provider = ClaudeCodeProvider(_settings(executable))

    with pytest.raises(TransportError, match="status 7.*diagnostic tail"):
        await _events(provider, _request(Message.text("user", "hi")))
    await provider.aclose()

    monkeypatch.setenv("RICKY_FAKE_CLAUDE_EXIT", "0")
    (responses / "1.jsonl").write_text("not-json\n", encoding="utf-8")
    provider = ClaudeCodeProvider(_settings(executable))
    with pytest.raises(TransportError, match="Malformed Claude Code stream line"):
        await _events(provider, _request(Message.text("user", "hi")))
    await provider.aclose()


@pytest.mark.asyncio
async def test_timeout_kills_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, record, _ = _fake_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("RICKY_FAKE_CLAUDE_SLEEP", "5")
    provider = ClaudeCodeProvider(_settings(executable, timeout=0.1))

    with pytest.raises(TransportError, match="timed out"):
        await _events(provider, _request(Message.text("user", "hi")))

    pid = _records(record)[0]["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert provider._processes == set()
    await provider.aclose()


@pytest.mark.asyncio
async def test_steady_streaming_outlasts_the_inactivity_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The timeout guards silence, not total stream duration."""
    executable, _, responses = _fake_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("RICKY_FAKE_CLAUDE_LINE_DELAY", "0.25")
    _write_response(
        responses / "0.jsonl",
        _text_delta("a"),
        _text_delta("b"),
        _text_delta("c"),
        _text_delta("d"),
        _result(),
    )
    provider = ClaudeCodeProvider(_settings(executable, timeout=1.0))

    events = await _events(provider, _request(Message.text("user", "hi")))
    await provider.aclose()

    assert "".join(event.delta for event in events if isinstance(event, TextDelta)) == "abcd"
    assert any(isinstance(event, MessageDone) for event in events)


@pytest.mark.asyncio
async def test_unlaunchable_binary_is_a_transport_error(tmp_path: Path) -> None:
    not_executable = tmp_path / "claude-no-exec-bit"
    not_executable.write_text("#!/bin/sh\n", encoding="utf-8")
    provider = ClaudeCodeProvider(_settings(not_executable))

    with pytest.raises(TransportError, match="Failed to launch"):
        await _events(provider, _request(Message.text("user", "hi")))
    await provider.aclose()


@pytest.mark.asyncio
async def test_cancellation_kills_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, record, _ = _fake_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("RICKY_FAKE_CLAUDE_SLEEP", "5")
    provider = ClaudeCodeProvider(_settings(executable))
    task = asyncio.create_task(_events(provider, _request(Message.text("user", "hi"))))
    for _ in range(100):
        if record.exists():
            break
        await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    pid = _records(record)[0]["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert provider._processes == set()
    await provider.aclose()


@pytest.mark.asyncio
async def test_missing_binary_is_actionable_auth_error(tmp_path: Path) -> None:
    provider = ClaudeCodeProvider(_settings(tmp_path / "missing-claude"))

    with pytest.raises(AuthError, match="install `claude`.*cli_path"):
        await _events(provider, _request(Message.text("user", "hi")))
    await provider.aclose()


@pytest.mark.asyncio
async def test_static_model_catalog_uses_cli_aliases(tmp_path: Path) -> None:
    provider = ClaudeCodeProvider(_settings(tmp_path / "unused"))

    models = await provider.list_models()
    await provider.aclose()

    assert [model.id for model in models] == ["fable", "opus", "sonnet", "haiku"]
    assert all(model.input_modalities == ["text", "image"] for model in models)


def test_claude_code_environment_settings_are_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='probe'\n")
    (tmp_path / "user-data").mkdir(exist_ok=True)
    (tmp_path / "user-data" / "ricky.toml").write_text(
        'default_provider = "claude_code"\n'
        "[providers.claude_code]\n"
        'default_model = "opus"\n'
        'cli_path = "/configured/claude"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RICKY_PROVIDERS__CLAUDE_CODE__DEFAULT_MODEL", "haiku")
    monkeypatch.setenv("RICKY_PROVIDERS__CLAUDE_CODE__RESUME_SESSIONS", "false")

    settings = RickySettings()

    assert settings.providers.claude_code.cli_path == "/configured/claude"
    assert settings.providers.claude_code.resume_sessions is True
    assert settings.resolve_selection().model_dump() == {
        "provider": "claude_code",
        "model": "opus",
    }


def test_cli_ask_and_picker_use_fake_claude(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, _, responses = _fake_cli(tmp_path, monkeypatch)
    _write_response(responses / "0.jsonl", _text_delta("ricky-ok"), _result())
    (tmp_path / "pyproject.toml").write_text("[project]\nname='probe'\n")
    (tmp_path / "user-data").mkdir(exist_ok=True)
    (tmp_path / "user-data" / "ricky.toml").write_text(
        f'[providers.claude_code]\ncli_path = "{executable}"\ndefault_model = "sonnet"\n'
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    ask_result = runner.invoke(app, ["ask", "-p", "claude_code", "hello"])
    picker_result = runner.invoke(app, ["config", "model"], input="3\n\n3\n")

    assert ask_result.exit_code == 0
    assert "ricky-ok" in ask_result.stdout
    assert picker_result.exit_code == 0
    parsed = tomllib.loads(
        (tmp_path / "user-data" / "profiles" / "personal" / "ricky.toml").read_text(
            encoding="utf-8"
        )
    )
    assert parsed["profile"]["default_provider"] == "claude_code"
    assert parsed["profile"]["default_models"]["claude_code"] == "sonnet"


class _ProbeParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str


class _ProbeTool:
    name: ClassVar[str] = "probe"
    description: ClassVar[str] = "Return a deterministic probe value."
    Params: ClassVar[type[BaseModel]] = _ProbeParams
    risk: ClassVar[Risk] = "mutating"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        parsed = _ProbeParams.model_validate(params)
        return ToolResult(content=f"stub:{parsed.value}")


@pytest.mark.asyncio
async def test_agent_loop_dispatches_claude_code_tools_inside_ricky(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, record, responses = _fake_cli(tmp_path, monkeypatch)
    tool_suffix = (
        '```tool_call\n{"name":"probe","args":{"value":"alpha"}}\n```\n'
        "```tool_call\n"
        '{"name":"update_tasks","args":{"tasks":'
        '[{"title":"Verify integration","status":"in_progress"}]}}\n```'
    )
    _write_response(
        responses / "0.jsonl",
        _text_delta(tool_suffix),
        _result(),
    )
    _write_response(
        responses / "1.jsonl",
        _text_delta("integration complete"),
        _result(),
    )
    settings = _settings(executable)
    provider = ClaudeCodeProvider(settings)
    session = AgentSession.create(
        settings,
        profile_scope=settings.resolve_profile_scope(),
        provider="claude_code",
        model="sonnet",
    )
    permission_requests: list[PermissionRequestedEvent] = []

    async def allow(request: PermissionRequestedEvent) -> PermissionResponse:
        permission_requests.append(request)
        return PermissionResponse(decision="allow")

    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([_ProbeTool(), UpdateTasksTool()]),
        settings=settings,
        permission_responder=allow,
        cwd=tmp_path,
    )

    events = [event async for event in loop.run_turn(session, "run the integration")]
    await provider.aclose()

    kinds = [event.kind for event in events]
    assert kinds.count("context_assembled") == 2
    assert kinds.count("tool_call_requested") == 2
    assert kinds.count("tool_call_finished") == 2
    assert "permission_requested" in kinds
    assert "tasks_updated" in kinds
    assert permission_requests[0].tool_name == "probe"
    assert session.tasks[0].title == "Verify integration"
    assert session.tasks[0].status == "in_progress"
    assert session.history[-1] == Message.text("assistant", "integration complete")

    invocations = _records(record)
    assert len(invocations) == 2
    first_argv = invocations[0]["argv"]
    follow_up_argv = invocations[1]["argv"]
    assert "--session-id" in first_argv
    assert "--no-session-persistence" not in first_argv
    assert (
        follow_up_argv[follow_up_argv.index("--resume") + 1]
        == first_argv[first_argv.index("--session-id") + 1]
    )
    follow_up = invocations[1]["stdin"]
    assert "<conversation_history>" not in follow_up
    assert "[tool_result for call_" in follow_up
    assert "stub:alpha" in follow_up
    assert "Updated 1 task(s)" in follow_up
    assert "Do not stop at a promise, progress update" in follow_up
    assert follow_up.endswith(
        "Finish only when the requested outcome is verified complete or you are concretely blocked."
    )
