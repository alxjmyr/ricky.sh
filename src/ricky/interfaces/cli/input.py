"""Interactive prompt editing for terminal CLI surfaces."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterable
from typing import TYPE_CHECKING, TextIO

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.formatted_text import HTML, AnyFormattedText
from prompt_toolkit.history import DummyHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from rich.text import Text

if TYPE_CHECKING:
    from prompt_toolkit.input import Input
    from prompt_toolkit.output import Output
    from rich.console import Console


CHAT_COMMANDS: tuple[str, ...] = (
    "/help",
    "/debug",
    "/tasks",
    "/context",
    "/compact",
    "/model",
    "/clear",
    "/permissions",
    "/remember",
    "/skill",
    "/workflow",
    "/quit",
    "/exit",
    "/q",
)

_COMMAND_DESCRIPTIONS = {
    "/help": "show commands",
    "/debug": "toggle verbose event rendering",
    "/tasks": "show temporary tasks",
    "/context": "inspect assembled context",
    "/compact": "compact older context",
    "/model": "show the current model",
    "/clear": "start a fresh session",
    "/permissions": "list or clear session grants",
    "/remember": "propose or save a memory",
    "/skill": "list or activate a skill",
    "/workflow": "list or run a workflow",
    "/quit": "exit chat",
    "/exit": "exit chat",
    "/q": "exit chat",
}
_CHAT_TOOLBAR = " Enter send · Ctrl+J newline · Ctrl+R history · Ctrl+X Ctrl+E editor "


class ChatCompleter(Completer):
    """Complete only top-level chat commands and skill identifiers."""

    def __init__(self) -> None:
        self._commands: tuple[str, ...] = ()
        self._skills: tuple[str, ...] = ()

    def configure(self, *, commands: Iterable[str], skills: Iterable[str] = ()) -> None:
        """Replace the locally available command and skill candidates."""
        self._commands = tuple(dict.fromkeys(commands))
        self._skills = tuple(sorted(set(skills)))

    def get_completions(self, document: object, complete_event: object) -> Iterable[Completion]:
        """Yield prefix matches for one command token or ``/skill`` name."""
        del complete_event
        text = getattr(document, "text_before_cursor", "")
        if not isinstance(text, str):
            return

        if text.startswith("/") and " " not in text:
            for command in self._commands:
                if command.startswith(text):
                    yield Completion(
                        command,
                        start_position=-len(text),
                        display_meta=_COMMAND_DESCRIPTIONS.get(command, ""),
                    )
            return

        prefix = "/skill "
        if text.startswith(prefix) and " " not in text[len(prefix) :]:
            fragment = text[len(prefix) :]
            for skill in self._skills:
                if skill.startswith(fragment):
                    yield Completion(skill, start_position=-len(fragment), display_meta="skill")


def chat_key_bindings() -> KeyBindings:
    """Return Ricky's portable multiline editing bindings."""
    bindings = KeyBindings()

    @bindings.add(Keys.Enter)
    def submit(event: object) -> None:
        buffer = event.current_buffer  # type: ignore[attr-defined]
        state = buffer.complete_state
        if state is not None:
            completion = state.current_completion
            if completion is None and len(state.completions) == 1:
                completion = state.completions[0]
            if completion is not None:
                buffer.apply_completion(completion)
            else:
                # Keep any common prefix inserted by Tab, but close the menu.
                buffer.complete_state = None
        buffer.validate_and_handle()

    @bindings.add(Keys.ControlI)
    def complete(event: object) -> None:
        buffer = event.current_buffer  # type: ignore[attr-defined]
        if buffer.complete_state is not None:
            buffer.complete_next()
            return
        if buffer.completer is None:
            return
        complete_event = CompleteEvent(completion_requested=True)
        completions = list(buffer.completer.get_completions(buffer.document, complete_event))
        if len(completions) == 1:
            buffer.apply_completion(completions[0])
        elif completions:
            buffer.start_completion(select_first=True, complete_event=complete_event)

    @bindings.add(Keys.ControlJ, eager=True)
    @bindings.add(Keys.Escape, Keys.Enter, eager=True)
    @bindings.add(Keys.Escape, Keys.ControlJ, eager=True)
    def newline(event: object) -> None:
        buffer = event.current_buffer  # type: ignore[attr-defined]
        if buffer.multiline():
            buffer.insert_text("\n")
        else:
            buffer.validate_and_handle()

    @bindings.add("c-c")
    def clear_or_interrupt(event: object) -> None:
        buffer = event.current_buffer  # type: ignore[attr-defined]
        if buffer.text:
            buffer.reset()
        else:
            event.app.exit(exception=KeyboardInterrupt())  # type: ignore[attr-defined]

    @bindings.add("c-x", "c-e")
    def edit_in_external_editor(event: object) -> None:
        # prompt_toolkit's stock binding submits after the editor exits. Keep
        # the edited draft in Ricky's composer so sending remains explicit.
        event.current_buffer.open_in_editor(validate_and_handle=False)  # type: ignore[attr-defined]

    @bindings.add("c-z", save_before=lambda _event: False)
    def undo(event: object) -> None:
        event.current_buffer.undo()  # type: ignore[attr-defined]

    @bindings.add("c-y", save_before=lambda _event: False)
    def redo(event: object) -> None:
        event.current_buffer.redo()  # type: ignore[attr-defined]

    return bindings


class CliInputSession:
    """Own enhanced TTY editing while retaining a plain stream fallback."""

    def __init__(
        self,
        console: Console,
        *,
        stdin: TextIO | None = None,
        interactive: bool | None = None,
        prompt_input: Input | None = None,
        prompt_output: Output | None = None,
    ) -> None:
        self._console = console
        self._stdin = stdin or sys.stdin
        self._interactive = (
            self._stdin.isatty() and console.is_terminal if interactive is None else interactive
        )
        self._completer = ChatCompleter()
        self._prompt_session: PromptSession[str] | None = None
        self._line_prompt_session: PromptSession[str] | None = None
        self._secret_prompt_session: PromptSession[str] | None = None
        if self._interactive:
            self._prompt_session = PromptSession(
                history=InMemoryHistory(),
                multiline=True,
                wrap_lines=True,
                enable_history_search=True,
                enable_open_in_editor=True,
                tempfile_suffix=".md",
                completer=self._completer,
                complete_while_typing=False,
                reserve_space_for_menu=6,
                key_bindings=chat_key_bindings(),
                input=prompt_input,
                output=prompt_output,
            )
            # Permission, approval, and selection answers are intentionally
            # isolated from chat history so Up/Ctrl+R recall user prompts only.
            self._line_prompt_session = PromptSession(
                multiline=False,
                wrap_lines=True,
                input=prompt_input,
                output=prompt_output,
            )
            self._secret_prompt_session = PromptSession(
                history=DummyHistory(),
                multiline=False,
                wrap_lines=False,
                input=prompt_input,
                output=prompt_output,
            )

    @property
    def interactive(self) -> bool:
        """Whether terminal editing enhancements are active."""
        return self._interactive

    def configure_completion(self, *, commands: Iterable[str], skills: Iterable[str] = ()) -> None:
        """Configure completion candidates for the current CLI surface."""
        self._completer.configure(commands=commands, skills=skills)

    async def read_chat(self) -> str:
        """Read one possibly multiline chat prompt."""
        if self._prompt_session is None:
            return await self._read_plain_line("[bold cyan]ricky>[/bold cyan] ")
        return await self._prompt_session.prompt_async(
            HTML("<b><ansicyan>ricky&gt;</ansicyan></b> "),
            multiline=True,
            completer=self._completer,
            bottom_toolbar=_CHAT_TOOLBAR,
        )

    async def read_line(self, prompt: str) -> str:
        """Read one single-line answer for a permission or selection prompt."""
        if self._line_prompt_session is None:
            return await self._read_plain_line(prompt)
        return await self._line_prompt_session.prompt_async(
            _plain_prompt(prompt),
            multiline=False,
            completer=None,
            bottom_toolbar=None,
        )

    async def read_secret(self, prompt: str) -> str:
        """Read one no-echo value using a literal prompt on an interactive terminal."""
        if self._secret_prompt_session is None:
            raise EOFError("secure input requires an interactive terminal")
        return await self._secret_prompt_session.prompt_async(
            prompt,
            is_password=True,
            multiline=False,
            completer=None,
            complete_while_typing=False,
            bottom_toolbar=None,
        )

    async def _read_plain_line(self, prompt: str) -> str:
        """Read a stream line without creating a cancellable background thread on a TTY."""
        loop = asyncio.get_running_loop()
        try:
            fd = self._stdin.fileno()
        except (AttributeError, OSError, ValueError):
            return await self._read_stream_line(prompt)

        if not self._stdin.isatty():
            return await self._read_stream_line(prompt)
        if not hasattr(loop, "add_reader"):
            return await asyncio.to_thread(self._console.input, prompt)

        self._console.print(prompt, end="")
        future: asyncio.Future[str] = loop.create_future()

        def on_stdin_ready() -> None:
            if future.done():
                return
            try:
                line = self._stdin.readline()
            except Exception as exc:  # noqa: BLE001 - terminal read errors surface to caller.
                future.set_exception(exc)
                return
            if line == "":
                future.set_exception(EOFError())
                return
            future.set_result(line.rstrip("\n"))

        loop.add_reader(fd, on_stdin_ready)
        try:
            return await future
        finally:
            loop.remove_reader(fd)

    async def _read_stream_line(self, prompt: str) -> str:
        """Read from a non-TTY stream while keeping prompts visible in captured output."""
        self._console.print(prompt, end="")
        line = await asyncio.to_thread(self._stdin.readline)
        if line == "":
            raise EOFError
        return line.rstrip("\n")


def _plain_prompt(prompt: str) -> AnyFormattedText:
    """Convert existing Rich prompt markup to prompt-toolkit-safe plain text."""
    return Text.from_markup(prompt).plain
