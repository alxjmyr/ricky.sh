"""Provider protocol and helpers."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ricky.llm._assembly import ToolCallState, assembled_message
from ricky.llm.types import (
    CompletionRequest,
    Message,
    MessageDone,
    ModelInfo,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    Usage,
)


@dataclass(frozen=True)
class ResolvedMedia:
    """Digest-verified bytes returned by a provider-bound resolver."""

    media_type: str
    content: bytes
    sha256: str
    width: int
    height: int


class MediaResolver(Protocol):
    """Materialize one canonical media ref under bound session/provider policy."""

    async def resolve(self, reference: object) -> ResolvedMedia:
        """Return validated bytes or fail locally before provider dispatch."""
        ...


class Provider(Protocol):
    """Streaming LLM provider interface."""

    name: str

    def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        """Stream provider events for a completion request."""
        ...

    async def aclose(self) -> None:
        """Release provider-owned resources (clients, subprocesses, temp state)."""
        ...


@runtime_checkable
class SupportsModelListing(Protocol):
    """Optional provider capability for live model catalogs."""

    async def list_models(self) -> list[ModelInfo]:
        """Return models currently advertised by the provider."""
        ...


@runtime_checkable
class SupportsSessionRotation(Protocol):
    """Optional provider capability for invalidating provider-native state."""

    async def rotate(self, session_id: str) -> None:
        """Start the next request for one agent session from canonical history."""
        ...


@runtime_checkable
class SupportsMediaResolver(Protocol):
    """Optional provider capability for session-bound multimodal requests."""

    def bind_media_resolver(self, resolver: MediaResolver) -> None:
        """Bind one exact session/provider materialization boundary."""
        ...


async def collect(stream: AsyncIterable[StreamEvent]) -> tuple[Message, Usage]:
    """Collect a stream into its final assistant message and usage."""
    text = ""
    thinking = ""
    tool_calls: dict[int, ToolCallState] = {}

    async for event in stream:
        if isinstance(event, MessageDone):
            return event.message, event.usage
        if isinstance(event, TextDelta):
            text += event.delta
        elif isinstance(event, ThinkingDelta):
            thinking += event.delta
        elif isinstance(event, ToolCallDelta):
            state = tool_calls.setdefault(event.index, ToolCallState())
            if event.id is not None:
                state.id = event.id
            if event.name is not None:
                state.name = event.name
            state.args_json += event.args_delta

    return assembled_message(text, thinking, tool_calls), Usage()
