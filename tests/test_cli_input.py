"""Tests for enhanced interactive CLI prompt editing."""

from __future__ import annotations

import asyncio
from io import StringIO
from types import SimpleNamespace
from typing import Any, cast

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from ricky.interfaces.cli.input import (
    CHAT_COMMANDS,
    ChatCompleter,
    CliInputSession,
    chat_key_bindings,
)


def _console() -> tuple[Console, StringIO]:
    output = StringIO()
    return Console(file=output, force_terminal=False, color_system=None), output


def _completion_texts(completer: ChatCompleter, text: str) -> list[str]:
    return [
        completion.text for completion in completer.get_completions(Document(text), CompleteEvent())
    ]


def test_completion_is_limited_to_commands_and_skill_names() -> None:
    completer = ChatCompleter()
    completer.configure(commands=CHAT_COMMANDS, skills={"review", "work/release"})

    assert _completion_texts(completer, "/deb") == ["/debug"]
    assert _completion_texts(completer, "/skill rev") == ["review"]
    assert _completion_texts(completer, "/skill work/") == ["work/release"]
    assert _completion_texts(completer, "/workflow rel") == []
    assert _completion_texts(completer, "ordinary prompt") == []


def test_external_editor_returns_to_composer_without_submitting() -> None:
    calls: list[bool] = []
    buffer = SimpleNamespace(
        open_in_editor=lambda *, validate_and_handle: calls.append(validate_and_handle)
    )
    binding = chat_key_bindings().get_bindings_for_keys(("c-x", "c-e"))[0]

    binding.handler(cast(Any, SimpleNamespace(current_buffer=buffer)))

    assert calls == [False]


async def test_non_tty_input_preserves_plain_stream_behavior() -> None:
    console, output = _console()
    input_session = CliInputSession(console, stdin=StringIO("hello\n"), interactive=False)

    result = await input_session.read_chat()

    assert result == "hello"
    assert "ricky>" in output.getvalue()


async def test_secure_input_fails_closed_without_a_tty() -> None:
    console, output = _console()
    input_session = CliInputSession(
        console,
        stdin=StringIO("must-not-be-read\n"),
        interactive=False,
    )

    with pytest.raises(EOFError, match="interactive terminal"):
        await input_session.read_secret("Secret: ")

    assert "must-not-be-read" not in output.getvalue()


@pytest.mark.parametrize("newline_keys", ["\x0a", "\x1b\r", "\x1b\n"])
async def test_interactive_input_inserts_newline_and_submits_with_enter(
    newline_keys: str,
) -> None:
    console, _output = _console()
    with create_pipe_input() as pipe_input:
        input_session = CliInputSession(
            console,
            interactive=True,
            prompt_input=pipe_input,
            prompt_output=DummyOutput(),
        )
        task = asyncio.create_task(input_session.read_chat())
        pipe_input.send_text(f"first{newline_keys}second\r")

        assert await asyncio.wait_for(task, timeout=1) == "first\nsecond"


async def test_interactive_bracketed_paste_keeps_multiline_text_in_one_prompt() -> None:
    console, _output = _console()
    with create_pipe_input() as pipe_input:
        input_session = CliInputSession(
            console,
            interactive=True,
            prompt_input=pipe_input,
            prompt_output=DummyOutput(),
        )
        task = asyncio.create_task(input_session.read_chat())
        pipe_input.send_text("\x1b[200~one\ntwo\x1b[201~\r")

        assert await asyncio.wait_for(task, timeout=1) == "one\ntwo"


async def test_interactive_tab_commits_a_unique_command_completion_before_submit() -> None:
    console, _output = _console()
    with create_pipe_input() as pipe_input:
        input_session = CliInputSession(
            console,
            interactive=True,
            prompt_input=pipe_input,
            prompt_output=DummyOutput(),
        )
        input_session.configure_completion(commands=CHAT_COMMANDS)
        task = asyncio.create_task(input_session.read_chat())
        pipe_input.send_text("/he\t\r")

        assert await asyncio.wait_for(task, timeout=1) == "/help"


async def test_interactive_ctrl_arrow_and_ctrl_k_edit_by_word_and_line() -> None:
    console, _output = _console()
    with create_pipe_input() as pipe_input:
        input_session = CliInputSession(
            console,
            interactive=True,
            prompt_input=pipe_input,
            prompt_output=DummyOutput(),
        )
        task = asyncio.create_task(input_session.read_chat())
        pipe_input.send_text("hello world\x1b[1;5D\x0bthere\r")

        assert await asyncio.wait_for(task, timeout=1) == "hello there"


async def test_interactive_navigation_moves_vertically_and_to_line_boundaries() -> None:
    console, _output = _console()
    with create_pipe_input() as pipe_input:
        input_session = CliInputSession(
            console,
            interactive=True,
            prompt_input=pipe_input,
            prompt_output=DummyOutput(),
        )
        task = asyncio.create_task(input_session.read_chat())
        pipe_input.send_text("first\x1b\rsecond\x1b[A\x01X\x1b[B\x05Y\r")

        assert await asyncio.wait_for(task, timeout=1) == "Xfirst\nsecondY"


async def test_interactive_ctrl_right_and_undo_redo_edit_the_draft() -> None:
    console, _output = _console()
    with create_pipe_input() as pipe_input:
        input_session = CliInputSession(
            console,
            interactive=True,
            prompt_input=pipe_input,
            prompt_output=DummyOutput(),
        )
        task = asyncio.create_task(input_session.read_chat())
        pipe_input.send_text("one three\x01\x1b[1;5C two\x1a\x19\r")

        assert await asyncio.wait_for(task, timeout=1) == "one two three"


async def test_interactive_ctrl_c_clears_a_draft_without_exiting() -> None:
    console, _output = _console()
    with create_pipe_input() as pipe_input:
        input_session = CliInputSession(
            console,
            interactive=True,
            prompt_input=pipe_input,
            prompt_output=DummyOutput(),
        )
        task = asyncio.create_task(input_session.read_chat())
        pipe_input.send_text("discard me\x03keep me\r")

        assert await asyncio.wait_for(task, timeout=1) == "keep me"


async def test_interactive_history_is_retained_within_the_session() -> None:
    console, _output = _console()
    with create_pipe_input() as pipe_input:
        input_session = CliInputSession(
            console,
            interactive=True,
            prompt_input=pipe_input,
            prompt_output=DummyOutput(),
        )
        first = asyncio.create_task(input_session.read_chat())
        pipe_input.send_text("remember this\r")
        assert await asyncio.wait_for(first, timeout=1) == "remember this"

        second = asyncio.create_task(input_session.read_chat())
        pipe_input.send_text("\x1b[A\r")
        assert await asyncio.wait_for(second, timeout=1) == "remember this"


async def test_single_line_decisions_do_not_enter_chat_history() -> None:
    console, _output = _console()
    with create_pipe_input() as pipe_input:
        input_session = CliInputSession(
            console,
            interactive=True,
            prompt_input=pipe_input,
            prompt_output=DummyOutput(),
        )
        first = asyncio.create_task(input_session.read_chat())
        pipe_input.send_text("remember this\r")
        assert await asyncio.wait_for(first, timeout=1) == "remember this"

        decision = asyncio.create_task(input_session.read_line("Allow? "))
        pipe_input.send_text("y\r")
        assert await asyncio.wait_for(decision, timeout=1) == "y"

        recalled = asyncio.create_task(input_session.read_chat())
        pipe_input.send_text("\x1b[A\r")
        assert await asyncio.wait_for(recalled, timeout=1) == "remember this"


async def test_secure_input_has_no_echo_and_does_not_enter_chat_history() -> None:
    console, output = _console()
    with create_pipe_input() as pipe_input:
        input_session = CliInputSession(
            console,
            interactive=True,
            prompt_input=pipe_input,
            prompt_output=DummyOutput(),
        )
        first = asyncio.create_task(input_session.read_chat())
        pipe_input.send_text("safe history\r")
        assert await asyncio.wait_for(first, timeout=1) == "safe history"

        secret = asyncio.create_task(input_session.read_secret("Secret: "))
        pipe_input.send_text("unique-secret-sentinel\r")
        assert await asyncio.wait_for(secret, timeout=1) == "unique-secret-sentinel"

        recalled = asyncio.create_task(input_session.read_chat())
        pipe_input.send_text("\x1b[A\r")
        assert await asyncio.wait_for(recalled, timeout=1) == "safe history"
    assert "unique-secret-sentinel" not in output.getvalue()
