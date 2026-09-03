"""Tests for provider helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ricky.llm.provider import collect
from ricky.llm.types import TextDelta, ThinkingDelta, ToolCallDelta, ToolCallPart


async def _events() -> AsyncIterator[TextDelta | ThinkingDelta | ToolCallDelta]:
    yield ThinkingDelta(delta="plan")
    yield TextDelta(delta="Use ")
    yield TextDelta(delta="tool")
    yield ToolCallDelta(index=0, id="call_1", name="read_file", args_delta='{"path"')
    yield ToolCallDelta(index=0, args_delta=':"README.md"}')


@pytest.mark.asyncio
async def test_collect_assembles_delta_only_stream() -> None:
    message, usage = await collect(_events())

    assert message.role == "assistant"
    assert usage.total_tokens == 0
    assert isinstance(message.content[-1], ToolCallPart)
    assert message.content[-1].args == {"path": "README.md"}
