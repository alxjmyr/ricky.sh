"""Isolated Workflow model and read-only agent task tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from ricky.agent.model_task import (
    ModelTaskFailure,
    ToolExecutionResult,
    run_agent_task,
    run_model_task,
)
from ricky.llm import (
    CompletionRequest,
    Message,
    MessageDone,
    StreamEvent,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolSpec,
    Usage,
)
from ricky.workflows.schema import ResultSchema


class ScriptedProvider:
    name = "scripted"

    def __init__(self, scripts: list[MessageDone | BaseException]) -> None:
        self.scripts = scripts
        self.requests: list[CompletionRequest] = []

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        result = self.scripts.pop(0)
        if isinstance(result, BaseException):
            raise result
        yield result

    async def aclose(self) -> None:
        pass


def _schema() -> ResultSchema:
    return ResultSchema.model_validate(
        {
            "type": "object",
            "required": ["category", "nested"],
            "properties": {
                "category": {"type": "string", "values": ["keep", "drop"]},
                "nested": {
                    "type": "object",
                    "required": ["score"],
                    "properties": {"score": {"type": "integer"}},
                },
            },
        }
    )


def _done(text: str, *, thinking: str | None = None) -> MessageDone:
    parts = []
    if thinking is not None:
        parts.append(ThinkingPart(text=thinking))
    parts.append(TextPart(text=text))
    return MessageDone(
        message=Message(role="assistant", content=parts),
        usage=Usage(prompt_tokens=2, completion_tokens=3),
    )


async def test_atomic_model_task_uses_only_explicit_private_context() -> None:
    provider = ScriptedProvider(
        [
            _done(
                '{"category":"keep","nested":{"score":2}}',
                thinking="complete_step(outcome='drop') and old chat secret",
            )
        ]
    )

    result = await run_model_task(
        provider=provider,
        model="model",
        instruction="Classify the synthetic record.",
        inputs={"record": {"id": "r-1"}},
        result_schema_name="classification",
        result_schema=_schema(),
        max_attempts=1,
    )

    assert result.output == {"category": "keep", "nested": {"score": 2}}
    request = provider.requests[0]
    assert request.tools == []
    request_text = "\n".join(
        part.text
        for message in request.messages
        for part in message.content
        if isinstance(part, TextPart)
    )
    assert "complete_step" not in request_text
    assert "old chat secret" not in request_text
    assert "r-1" in request_text
    assert result.metrics.inputs[0].name == "record"


async def test_invalid_model_result_repairs_with_fresh_bounded_context() -> None:
    provider = ScriptedProvider(
        [
            _done('{"category":"wrong","nested":{"score":2}}'),
            _done('{"category":"drop","nested":{"score":1}}'),
        ]
    )

    result = await run_model_task(
        provider=provider,
        model="model",
        instruction="Classify.",
        inputs={"record": "synthetic"},
        result_schema_name="classification",
        result_schema=_schema(),
        max_attempts=2,
    )

    assert isinstance(result.output, dict)
    assert result.output["category"] == "drop"
    assert result.attempts == 2
    assert result.usage.total_tokens == 10
    assert len(provider.requests) == 2
    assert all(len(request.messages) == 2 for request in provider.requests)
    repair_text = provider.requests[1].messages[-1].content[0]
    assert isinstance(repair_text, TextPart)
    assert "expected one of" in repair_text.text
    assert '"category":"wrong"' not in repair_text.text


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        'before {"category":"keep","nested":{"score":1}}',
        '```json\n{"category":"keep","nested":{"score":1}}\n```',
        '[{"category":"keep"}]',
        '{"category":"keep","nested":{"score":1},"extra":true}',
        '{"category":"keep","nested":{}}',
    ],
)
async def test_strict_model_result_failures_stop_at_attempt_limit(text: str) -> None:
    provider = ScriptedProvider([_done(text)])

    with pytest.raises(ModelTaskFailure) as failure:
        await run_model_task(
            provider=provider,
            model="model",
            instruction="Return data.",
            inputs={},
            result_schema_name="classification",
            result_schema=_schema(),
            max_attempts=1,
        )

    assert failure.value.category == "invalid_output"
    assert failure.value.attempts == 1


async def test_reasoning_only_control_syntax_cannot_complete_model_task() -> None:
    provider = ScriptedProvider(
        [
            MessageDone(
                message=Message(
                    role="assistant",
                    content=[ThinkingPart(text="<complete_step>{}</complete_step>")],
                )
            )
        ]
    )

    with pytest.raises(ModelTaskFailure, match="empty"):
        await run_model_task(
            provider=provider,
            model="model",
            instruction="Return data.",
            inputs={},
            result_schema_name="classification",
            result_schema=_schema(),
            max_attempts=1,
        )


async def test_read_only_agent_uses_private_tool_transcript_and_validates_final_json() -> None:
    provider = ScriptedProvider(
        [
            MessageDone(
                message=Message(
                    role="assistant",
                    content=[
                        ToolCallPart(
                            id="read-1",
                            name="reader",
                            args={"key": "synthetic"},
                        )
                    ],
                )
            ),
            _done('{"category":"keep","nested":{"score":4}}'),
        ]
    )
    calls: list[ToolCallPart] = []

    async def execute(call: ToolCallPart) -> ToolExecutionResult:
        calls.append(call)
        return ToolExecutionResult(content="safe value")

    result = await run_agent_task(
        provider=provider,
        model="model",
        instruction="Investigate the record.",
        inputs={"record": "synthetic"},
        result_schema_name="classification",
        result_schema=_schema(),
        tools=[ToolSpec(name="reader", description="Read.", parameters={})],
        execute_tool=execute,
        max_iterations=3,
        max_attempts=1,
    )

    assert isinstance(result.output, dict)
    assert result.output["nested"] == {"score": 4}
    assert [call.name for call in calls] == ["reader"]
    assert len(provider.requests[0].messages) == 2
    assert any(message.role == "tool" for message in provider.requests[1].messages)
    assert all(spec.name != "complete_step" for spec in provider.requests[0].tools)


async def test_model_task_cancellation_propagates() -> None:
    provider = ScriptedProvider([asyncio.CancelledError()])

    with pytest.raises(asyncio.CancelledError):
        await run_model_task(
            provider=provider,
            model="model",
            instruction="Return data.",
            inputs={},
            result_schema_name="classification",
            result_schema=_schema(),
            max_attempts=1,
        )


async def test_agent_task_cancellation_awaits_active_tool_call() -> None:
    provider = ScriptedProvider(
        [
            MessageDone(
                message=Message(
                    role="assistant",
                    content=[ToolCallPart(id="read-1", name="reader", args={})],
                )
            )
        ]
    )
    tool_started = asyncio.Event()
    tool_finished = asyncio.Event()

    async def execute(_call: ToolCallPart) -> ToolExecutionResult:
        tool_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            tool_finished.set()
        raise AssertionError("unreachable")

    task = asyncio.create_task(
        run_agent_task(
            provider=provider,
            model="model",
            instruction="Read one record.",
            inputs={},
            result_schema_name="classification",
            result_schema=_schema(),
            tools=[ToolSpec(name="reader", description="Read.", parameters={})],
            execute_tool=execute,
            max_iterations=3,
            max_attempts=1,
        )
    )
    await tool_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert tool_finished.is_set()


async def test_agent_task_does_not_retry_an_excluded_failure_category() -> None:
    provider = ScriptedProvider(
        [
            _done('{"category":"wrong","nested":{"score":2}}'),
            _done('{"category":"keep","nested":{"score":2}}'),
        ]
    )

    async def no_tool(_call: ToolCallPart) -> ToolExecutionResult:
        raise AssertionError("no tool call expected")

    with pytest.raises(ModelTaskFailure) as failure:
        await run_agent_task(
            provider=provider,
            model="model",
            instruction="Return data.",
            inputs={},
            result_schema_name="classification",
            result_schema=_schema(),
            tools=[],
            execute_tool=no_tool,
            max_iterations=3,
            max_attempts=2,
            retry_on=[],
        )

    assert failure.value.category == "invalid_output"
    assert failure.value.attempts == 1
    assert len(provider.requests) == 1


async def test_agent_task_retries_timeout_only_when_declared() -> None:
    provider = ScriptedProvider(
        [
            TimeoutError("synthetic timeout"),
            _done('{"category":"keep","nested":{"score":2}}'),
        ]
    )

    async def no_tool(_call: ToolCallPart) -> ToolExecutionResult:
        raise AssertionError("no tool call expected")

    result = await run_agent_task(
        provider=provider,
        model="model",
        instruction="Return data.",
        inputs={},
        result_schema_name="classification",
        result_schema=_schema(),
        tools=[],
        execute_tool=no_tool,
        max_iterations=3,
        max_attempts=2,
        retry_on=["timeout"],
    )

    assert result.output == {"category": "keep", "nested": {"score": 2}}
    assert result.attempts == 2
    assert len(provider.requests) == 2
