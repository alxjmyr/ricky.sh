"""Shared stream-assembly helpers for provider adapters."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ricky.llm.types import Message, TextPart, ThinkingPart, ToolCallPart, TransportError


@dataclass
class ToolCallState:
    """Incrementally accumulated tool-call fragments, keyed by stream index."""

    id: str | None = None
    name: str | None = None
    args_json: str = ""


def assembled_message(
    text: str,
    thinking: str,
    tool_calls: dict[int, ToolCallState],
) -> Message:
    """Assemble accumulated stream state into the final assistant message."""
    content = []
    if thinking:
        content.append(ThinkingPart(text=thinking))
    if text:
        content.append(TextPart(text=text))
    for index in sorted(tool_calls):
        state = tool_calls[index]
        argument_error = None
        try:
            args = json.loads(state.args_json or "{}")
        except json.JSONDecodeError:
            args = {}
            argument_error = "malformed JSON"
        if not isinstance(args, dict):
            args = {}
            argument_error = "non-object JSON"
        content.append(
            ToolCallPart(
                id=state.id or f"call_{index}",
                name=state.name or "",
                args=args,
                argument_error=argument_error,
            )
        )
    return Message(role="assistant", content=content)


def loads_json_object(data: str, *, provider: str) -> dict[str, Any]:
    """Parse one stream payload that must be a JSON object."""
    try:
        value = json.loads(data)
    except json.JSONDecodeError as exc:
        raise TransportError(f"Malformed {provider} stream event: {data}") from exc
    if not isinstance(value, dict):
        raise TransportError(f"Unexpected {provider} stream event: {data}")
    return value


def error_message(prefix: str, detail: str) -> str:
    """Join an error prefix with optional response detail."""
    if not detail:
        return prefix
    return f"{prefix}: {detail}"
