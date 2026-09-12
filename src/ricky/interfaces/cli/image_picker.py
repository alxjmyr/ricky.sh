"""Transactional terminal image selection and image-free command history."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import PathCompleter
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings

from ricky.media import SessionMediaError

if TYPE_CHECKING:
    from prompt_toolkit.input import Input
    from prompt_toolkit.output import Output
    from rich.console import Console

    from ricky.media import ImageUpload


class ChatHistory(InMemoryHistory):
    """Keep attachment commands out of recall to prevent accidental reattachment."""

    def append_string(self, string: str) -> None:
        if string.strip().lower().split(maxsplit=1)[:1] != ["/img"]:
            super().append_string(string)


class OpenImagePicker(Exception):
    """Leave the composer with its unsent text intact."""

    def __init__(self, draft: str) -> None:
        self.draft = draft
        super().__init__()


async def pick_images(
    current: list[ImageUpload],
    *,
    loader: Callable[[Path], ImageUpload],
    max_images: int,
    console: Console,
    prompt_input: Input | None,
    prompt_output: Output | None,
) -> list[ImageUpload]:
    """Select snapshots across directories; commit on Done, discard on Escape."""
    selected = list(current)
    bindings = KeyBindings()

    @bindings.add("escape", eager=True)
    def cancel(event: object) -> None:
        event.app.exit(exception=KeyboardInterrupt())  # type: ignore[attr-defined]

    prompt: PromptSession[str] = PromptSession(
        input=prompt_input,
        output=prompt_output,
        completer=PathCompleter(
            expanduser=True,
            file_filter=lambda filename: (
                Path(filename).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
            ),
        ),
        complete_while_typing=True,
        key_bindings=bindings,
    )
    console.print("Images: enter a path (Tab completes), /remove N, /done, or Esc to cancel.")
    while True:
        console.print(
            "Selected: "
            + ("  ".join(f"[{i}] {item.filename}" for i, item in enumerate(selected, 1)) or "none"),
            markup=False,
        )
        try:
            answer = (await prompt.prompt_async("image> ")).strip()
        except (KeyboardInterrupt, EOFError):
            return current
        if answer.lower() in {"/done", "done"}:
            return selected
        if answer.lower() == "/cancel":
            return current
        if answer.startswith("/remove "):
            try:
                index = int(answer.removeprefix("/remove ")) - 1
                if index < 0 or index >= len(selected):
                    raise ValueError
                selected.pop(index)
            except ValueError:
                console.print("Use /remove followed by a selected image number.")
            continue
        if not answer:
            continue
        try:
            if len(selected) >= max_images:
                raise ValueError(f"Attach at most {max_images} images per message.")
            selected.append(await asyncio.to_thread(loader, Path(answer).expanduser()))
        except (OSError, ValueError, SessionMediaError) as exc:
            console.print(str(exc), markup=False, style="yellow")
