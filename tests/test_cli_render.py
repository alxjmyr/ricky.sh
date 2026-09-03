"""Tests for CLI event rendering."""

from __future__ import annotations

import asyncio
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import SecretStr
from rich.console import Console

from ricky.agent.events import (
    AgentErrorEvent,
    ContextAssembledEvent,
    ContextSection,
    LlmRequestStartedEvent,
    LlmResponseFinishedEvent,
    PermissionDecidedEvent,
    PermissionRequestedEvent,
    TasksUpdatedEvent,
    TextDeltaEvent,
    ToolCallFinishedEvent,
    ToolCallRequestedEvent,
    ToolCallStartedEvent,
    WorkflowEvent,
)
from ricky.config import RickySettings
from ricky.interfaces.cli.input import CliInputSession
from ricky.interfaces.cli.protected_values import _read_values
from ricky.interfaces.cli.render import CliRenderer, summarize_tool_call
from ricky.permissions import GrantOption
from ricky.profiles import ProfileResourceRef
from ricky.protected_values import (
    DestinationApprovalRequest,
    ProtectedFieldDescriptor,
    SecureValueInputRequest,
)


def _renderer(*, debug: bool = False) -> tuple[CliRenderer, StringIO]:
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=100)
    return CliRenderer(console=console, debug=debug), output


@pytest.mark.parametrize("label", ["[/bold]", "[bold]Security code[/bold]"])
async def test_secure_value_prompt_renders_protected_metadata_literally(label: str) -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=100)
    input_session = CliInputSession(console, stdin=StringIO(), interactive=False)
    prompts: list[str] = []
    sentinel = "secure-prompt-sentinel-5729"

    class RecordingSecretSession:
        async def prompt_async(self, prompt: str, **kwargs: object) -> str:
            prompts.append(prompt)
            assert kwargs["is_password"] is True
            return sentinel

    input_session._secret_prompt_session = cast(Any, RecordingSecretSession())
    renderer = CliRenderer(console=console, input_session=input_session)

    result = await renderer.request_secure_value(
        SecureValueInputRequest(
            ref=ProfileResourceRef(profile="personal", name="[italic]login"),
            field="security_code",
            label=label,
            top_level_origin="https://[::1]:8443",
            frame_origin="https://[::1]:8443",
        )
    )

    assert isinstance(result, SecretStr)
    assert result == SecretStr(sentinel)
    assert prompts == [f"Enter {label} for personal/[italic]login on https://[::1]:8443: "]
    assert sentinel not in output.getvalue()


async def test_operator_stored_value_prompt_renders_field_label_literally() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=100)
    input_session = CliInputSession(console, stdin=StringIO(), interactive=False)
    prompts: list[str] = []
    sentinel = "operator-secret-prompt-sentinel-8143"

    class RecordingSecretSession:
        async def prompt_async(self, prompt: str, **kwargs: object) -> str:
            prompts.append(prompt)
            assert kwargs["is_password"] is True
            return sentinel

    input_session._secret_prompt_session = cast(Any, RecordingSecretSession())
    renderer = CliRenderer(console=console, input_session=input_session)
    ref = ProfileResourceRef(profile="personal", name="[italic]login")

    values = await _read_values(
        renderer,
        ref,
        (
            ProtectedFieldDescriptor(
                name="password",
                label="[/bold]",
                mode="stored",
                compatible_controls=("password",),
            ),
        ),
    )

    assert values == {"password": SecretStr(sentinel)}
    assert prompts == ["Enter [/bold] for personal/[italic]login: "]
    assert sentinel not in output.getvalue()


async def test_destination_approval_renders_protected_metadata_literally() -> None:
    renderer, output = _renderer()

    async def deny(_prompt: str) -> str:
        return "n"

    renderer._read_prompt_line = deny  # type: ignore[method-assign]

    response = await renderer.request_protected_destination(
        DestinationApprovalRequest(
            ref=ProfileResourceRef(profile="personal", name="[italic]login"),
            revision=1,
            field="password",
            label="[/bold]",
            top_level_origin="https://[::1]:8443",
            frame_origin="https://[::1]:9443",
            occurrence="test-occurrence",
            execution_mode="foreground",
        )
    )

    assert response.decision == "deny"
    rendered = output.getvalue()
    assert "personal/[italic]login ([/bold])" in rendered
    assert "https://[::1]:8443" in rendered
    assert "https://[::1]:9443" in rendered


def test_config_renders_resolved_user_data_path(tmp_path: Path) -> None:
    output = StringIO()
    renderer = CliRenderer(
        console=Console(file=output, force_terminal=False, color_system=None, width=240)
    )
    settings = RickySettings(user_data_dir=str(tmp_path / "custom-user-data"))

    renderer.render_config(
        settings,
        installation_config_path=tmp_path / "ricky.toml",
        profile_config_path=tmp_path / "profiles" / "personal" / "ricky.toml",
        profile_secrets_path=tmp_path / "profiles" / "personal" / ".secrets.toml",
    )

    text = output.getvalue()
    assert "user_data_path" in text
    assert str((tmp_path / "custom-user-data").resolve()) in text


def test_completed_response_renders_markdown_before_tool_line() -> None:
    renderer, output = _renderer()

    renderer.render_event(TextDeltaEvent(turn_id="turn", delta="**hello**"))
    renderer.render_event(
        LlmResponseFinishedEvent(
            turn_id="turn",
            iteration=1,
            text_chars=9,
            thinking_chars=0,
            tool_call_count=1,
            empty=False,
        )
    )
    renderer.render_event(
        ToolCallFinishedEvent(
            turn_id="turn",
            call_id="call",
            tool_name="read_file",
            is_error=False,
            content_chars=42,
            content="file contents",
        )
    )

    text = output.getvalue()
    assert "hello" in text
    assert "**hello**" not in text
    assert "read_file" in text
    assert "ok" in text
    assert text.index("hello") < text.index("read_file")


def test_each_model_response_is_a_separate_markdown_segment_around_tools() -> None:
    renderer, output = _renderer()

    renderer.render_event(
        LlmRequestStartedEvent(
            turn_id="turn", iteration=1, model="model", message_count=1, tool_count=1
        )
    )
    assert renderer._activity_message == "Thinking…"
    renderer.render_event(TextDeltaEvent(turn_id="turn", delta="I will inspect it."))
    renderer.render_event(
        LlmResponseFinishedEvent(
            turn_id="turn",
            iteration=1,
            text_chars=18,
            thinking_chars=0,
            tool_call_count=1,
            empty=False,
        )
    )
    assert renderer._activity_message is None
    renderer.render_event(
        ToolCallRequestedEvent(
            turn_id="turn", call_id="call", tool_name="read_file", args={"path": "a.md"}
        )
    )
    renderer.render_event(
        ToolCallStartedEvent(turn_id="turn", call_id="call", tool_name="read_file")
    )
    assert renderer._activity_message == "Running read_file…"
    renderer.render_event(
        ToolCallFinishedEvent(
            turn_id="turn",
            call_id="call",
            tool_name="read_file",
            is_error=False,
            content_chars=10,
            content="contents",
        )
    )
    assert renderer._activity_message is None
    renderer.render_event(
        LlmRequestStartedEvent(
            turn_id="turn", iteration=2, model="model", message_count=3, tool_count=1
        )
    )
    renderer.render_event(TextDeltaEvent(turn_id="turn", delta="## Result\n\nDone."))
    renderer.render_event(
        LlmResponseFinishedEvent(
            turn_id="turn",
            iteration=2,
            text_chars=16,
            thinking_chars=0,
            tool_call_count=0,
            empty=False,
        )
    )

    text = output.getvalue()
    assert text.index("I will inspect it.") < text.index("read_file") < text.index("Result")
    assert "## Result" not in text


def test_interrupted_partial_response_is_preserved_and_labeled() -> None:
    renderer, output = _renderer()

    renderer.render_event(TextDeltaEvent(turn_id="turn", delta="unfinished **response"))
    renderer.render_event(
        AgentErrorEvent(turn_id="turn", error_type="ProviderError", message="disconnected")
    )

    text = output.getvalue()
    assert "Partial response (interrupted)" in text
    assert "unfinished" in text
    assert "ProviderError" in text


def test_parallel_tool_activity_tracks_remaining_calls() -> None:
    renderer, _output = _renderer()
    renderer.render_event(
        ToolCallStartedEvent(turn_id="turn", call_id="one", tool_name="read_file")
    )
    renderer.render_event(
        ToolCallStartedEvent(turn_id="turn", call_id="two", tool_name="web_search")
    )
    assert renderer._activity_message == "Running 2 tools…"

    renderer.render_event(
        ToolCallFinishedEvent(
            turn_id="turn",
            call_id="one",
            tool_name="read_file",
            is_error=False,
            content_chars=1,
            content="x",
        )
    )

    assert renderer._activity_message == "Running web_search…"


def test_thinking_status_is_started_and_stopped_on_an_interactive_terminal() -> None:
    output = StringIO()
    renderer = CliRenderer(
        console=Console(file=output, force_terminal=True, color_system="standard", width=100)
    )

    renderer.render_event(
        LlmRequestStartedEvent(
            turn_id="turn", iteration=1, model="model", message_count=1, tool_count=0
        )
    )
    assert renderer._activity is not None
    assert renderer._activity_message == "Thinking…"
    renderer.render_event(
        LlmResponseFinishedEvent(
            turn_id="turn",
            iteration=1,
            text_chars=0,
            thinking_chars=1,
            tool_call_count=0,
            empty=True,
        )
    )

    assert renderer._activity_message is None


def test_transient_indicators_are_omitted_from_redirected_output() -> None:
    renderer, output = _renderer()

    renderer.render_event(
        LlmRequestStartedEvent(
            turn_id="turn", iteration=1, model="model", message_count=1, tool_count=0
        )
    )
    renderer.render_event(
        LlmResponseFinishedEvent(
            turn_id="turn",
            iteration=1,
            text_chars=0,
            thinking_chars=1,
            tool_call_count=0,
            empty=True,
        )
    )

    assert output.getvalue() == ""


def test_context_event_is_debug_only() -> None:
    event = ContextAssembledEvent(
        turn_id="turn",
        iteration=1,
        model="model-a",
        sections=[ContextSection(name="system", chars=10)],
        message_count=2,
        tool_count=3,
        char_count=99,
    )
    normal, normal_output = _renderer()
    debug, debug_output = _renderer(debug=True)

    normal.render_event(event)
    debug.render_event(event)

    assert normal_output.getvalue() == ""
    assert "Context assembled" in debug_output.getvalue()
    assert "model-a" in debug_output.getvalue()


def test_llm_response_event_is_debug_only() -> None:
    event = LlmResponseFinishedEvent(
        turn_id="turn",
        iteration=2,
        stop_reason="stop",
        text_chars=0,
        thinking_chars=14,
        tool_call_count=0,
        empty=True,
    )
    normal, normal_output = _renderer()
    debug, debug_output = _renderer(debug=True)

    normal.render_event(event)
    debug.render_event(event)

    assert normal_output.getvalue() == ""
    rendered = debug_output.getvalue()
    assert "LLM response" in rendered
    assert '"stop_reason": "stop"' in rendered
    assert '"empty": true' in rendered


def test_tool_request_summary_and_debug_args() -> None:
    event = ToolCallRequestedEvent(
        turn_id="turn",
        call_id="call",
        tool_name="run_shell",
        args={"command": "uv run pytest"},
    )
    normal, normal_output = _renderer()
    debug, debug_output = _renderer(debug=True)

    normal.render_event(event)
    debug.render_event(event)

    assert "uv run pytest" in normal_output.getvalue()
    assert "command" in debug_output.getvalue()


def test_tasks_updated_renders_task_titles() -> None:
    renderer, output = _renderer()

    renderer.render_event(
        TasksUpdatedEvent(
            turn_id="turn",
            tasks=[{"id": "task_1", "title": "Run tests", "status": "in_progress"}],
        )
    )

    assert "Run tests" in output.getvalue()


def test_summarize_tool_call_truncates_long_values() -> None:
    summary = summarize_tool_call("run_shell", {"command": "x" * 500})

    assert len(summary) <= 140
    assert summary.endswith("...")


def test_summarize_slack_send_keeps_transcript_compact() -> None:
    text = "line one\nline two\n" + "y" * 500
    summary = summarize_tool_call(
        "slack_send_message",
        {"target": "#eng", "text": text, "thread_ts": "1752831240.001200"},
    )

    assert len(summary) <= 140
    assert "\n" not in summary
    assert text not in summary


async def test_permission_prompt_retries_invalid_answer() -> None:
    renderer, output = _renderer()
    answers = iter(["bad", "a"])

    async def fake_read_prompt_line(_prompt: str) -> str:
        return next(answers)

    renderer._read_prompt_line = fake_read_prompt_line  # type: ignore[method-assign]

    response = await renderer.request_permission(
        PermissionRequestedEvent(
            turn_id="turn",
            call_id="call",
            tool_name="gmail_trash",
            args={"account": "personal", "message_id": "m1"},
            reason="default for mutating tools",
            offered_grants=[GrantOption(id="scoped", label="gmail_trash on personal")],
        )
    )

    assert response.decision == "allow"
    assert response.grant == "scoped"
    assert "Choose" in output.getvalue()


async def test_permission_prompt_maps_unconstrained_key_to_tool_id() -> None:
    renderer, _ = _renderer()
    prompts: list[str] = []

    async def fake_read_prompt_line(prompt: str) -> str:
        prompts.append(prompt)
        return "A"

    renderer._read_prompt_line = fake_read_prompt_line  # type: ignore[method-assign]

    response = await renderer.request_permission(
        PermissionRequestedEvent(
            turn_id="turn",
            call_id="call",
            tool_name="gmail_trash",
            args={"account": "personal", "message_id": "m1"},
            reason="default for mutating tools",
            offered_grants=[
                GrantOption(id="scoped", label="gmail_trash on personal"),
                GrantOption(id="tool", label="all gmail_trash (any params)"),
            ],
        )
    )

    assert response.decision == "allow"
    assert response.grant == "tool"
    assert "a/A" in prompts[0]


async def test_permission_prompt_maps_directory_key_without_choosing_scope() -> None:
    renderer, _ = _renderer()
    prompts: list[str] = []

    async def fake_read_prompt_line(prompt: str) -> str:
        prompts.append(prompt)
        return "d"

    renderer._read_prompt_line = fake_read_prompt_line  # type: ignore[method-assign]

    response = await renderer.request_permission(
        PermissionRequestedEvent(
            turn_id="turn",
            call_id="call",
            tool_name="read_file",
            args={"path": "/home/alex/Documents/report.txt"},
            reason="host path is outside the active workspace",
            offered_grants=[
                GrantOption(id="scoped", label="read_file at report.txt"),
                GrantOption(id="directory", label="read_file under Documents"),
            ],
        )
    )

    assert response.decision == "allow"
    assert response.grant == "directory"
    assert "a/d" in prompts[0]


async def test_permission_prompt_without_grants_offers_only_yes_no() -> None:
    renderer, _ = _renderer()
    prompts: list[str] = []

    async def fake_read_prompt_line(prompt: str) -> str:
        prompts.append(prompt)
        return "y"

    renderer._read_prompt_line = fake_read_prompt_line  # type: ignore[method-assign]

    response = await renderer.request_permission(
        PermissionRequestedEvent(
            turn_id="turn",
            call_id="call",
            tool_name="gmail_send_message",
            args={"account": "work"},
            reason="default for mutating tools",
        )
    )

    assert response.decision == "allow"
    assert response.grant is None
    assert "[y/n]" in prompts[0]


async def test_permission_summary_renders_markup_like_transaction_text_literally() -> None:
    renderer, output = _renderer()

    async def fake_read_prompt_line(_prompt: str) -> str:
        return "n"

    renderer._read_prompt_line = fake_read_prompt_line  # type: ignore[method-assign]
    response = await renderer.request_permission(
        PermissionRequestedEvent(
            turn_id="turn",
            call_id="call",
            tool_name="browser_commit",
            args={"kind": "financial"},
            reason="fresh interactive review required",
            summary=("FINANCIAL TRANSACTION\nPayee: [/bold] [red]Page-provided merchant[/red]"),
        )
    )

    assert response.decision == "deny"
    rendered = output.getvalue()
    assert "[/bold]" in rendered
    assert "[red]Page-provided merchant[/red]" in rendered


async def test_permission_prompt_defers_workflow_events_until_answered() -> None:
    renderer, output = _renderer()
    prompt_opened = asyncio.Event()
    answer_ready = asyncio.Event()

    async def fake_read_prompt_line(_prompt: str) -> str:
        prompt_opened.set()
        await answer_ready.wait()
        return "y"

    renderer._read_prompt_line = fake_read_prompt_line  # type: ignore[method-assign]
    response_task = asyncio.create_task(
        renderer.request_permission(
            PermissionRequestedEvent(
                turn_id="turn",
                call_id="call",
                tool_name="gmail_modify_labels",
                args={"account": "personal"},
                reason="default for mutating tools",
            )
        )
    )
    await prompt_opened.wait()

    renderer.render_event(
        WorkflowEvent(
            run_id="run",
            workflow_name="triage",
            action="step_skipped",
            execution_address='process/"id"/draft-response',
            reason="condition was false",
        )
    )

    assert "step_skipped" not in output.getvalue()
    answer_ready.set()

    response = await response_task

    assert response.decision == "allow"
    rendered = output.getvalue()
    assert rendered.index("Permission required") < rendered.index("step_skipped")


async def test_permission_prompts_are_serialized() -> None:
    renderer, output = _renderer()
    answers: asyncio.Queue[str] = asyncio.Queue()
    first_prompt_opened = asyncio.Event()
    prompt_count = 0

    async def fake_read_prompt_line(_prompt: str) -> str:
        nonlocal prompt_count
        prompt_count += 1
        if prompt_count == 1:
            first_prompt_opened.set()
        return await answers.get()

    renderer._read_prompt_line = fake_read_prompt_line  # type: ignore[method-assign]
    request = lambda call_id: PermissionRequestedEvent(  # noqa: E731 - keeps paired requests clear.
        turn_id="turn",
        call_id=call_id,
        tool_name="gmail_modify_labels",
        args={"account": "personal"},
        reason="default for mutating tools",
    )
    first = asyncio.create_task(renderer.request_permission(request("first")))
    await first_prompt_opened.wait()
    second = asyncio.create_task(renderer.request_permission(request("second")))
    await asyncio.sleep(0)

    assert output.getvalue().count("Permission required") == 1
    await answers.put("y")
    first_response = await first
    await asyncio.sleep(0)

    assert output.getvalue().count("Permission required") == 2
    await answers.put("n")
    second_response = await second

    assert first_response.decision == "allow"
    assert second_response.decision == "deny"


def test_permission_decided_shows_remembered_label() -> None:
    renderer, output = _renderer()

    renderer.render_event(
        PermissionDecidedEvent(
            turn_id="turn",
            call_id="call",
            tool_name="gmail_trash",
            decision="allow",
            reason="allowed by user",
            remembered=True,
            grant_label="gmail_trash on personal",
        )
    )

    assert "remembered: gmail_trash on personal" in output.getvalue()
