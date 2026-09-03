"""Strict tool argument and repair contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

import pytest
from pydantic import BaseModel, ConfigDict

from ricky.agent import AgentLoop, AgentSession
from ricky.config import RickySettings
from ricky.durable_tasks.tools import CreateTaskParams
from ricky.llm import (
    CompletionRequest,
    Message,
    MessageDone,
    TextPart,
    ToolCallPart,
    Usage,
)
from ricky.tools import Risk, ToolContext, ToolContractError, ToolRegistry, ToolResult


class _NestedValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int


class _StrictParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: int
    nested: _NestedValue | None = None


class _StrictTool:
    name: ClassVar[str] = "strict_probe"
    description: ClassVar[str] = "Validate one strict integer argument."
    Params: ClassVar[type[BaseModel]] = _StrictParams
    risk: ClassVar[Risk] = "read_only"

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        parsed = _StrictParams.model_validate(params)
        return ToolResult(content=str(parsed.value))


class _Provider:
    name = "strict-test-provider"

    def __init__(self, responses: list[MessageDone]) -> None:
        self.responses = responses
        self.requests: list[CompletionRequest] = []

    async def stream(self, request: CompletionRequest) -> AsyncIterator[MessageDone]:
        self.requests.append(request)
        yield self.responses.pop(0)

    async def aclose(self) -> None:
        return None


def _tool_response(*calls: ToolCallPart) -> MessageDone:
    return MessageDone(
        message=Message(role="assistant", content=list(calls)),
        usage=Usage(),
        stop_reason="tool_calls",
    )


def _final_response(text: str) -> MessageDone:
    return MessageDone(
        message=Message(role="assistant", content=[TextPart(text=text)]),
        usage=Usage(),
        stop_reason="stop",
    )


def test_registry_requires_strict_closed_params_and_nested_models() -> None:
    class NonStrictParams(BaseModel):
        model_config = ConfigDict(extra="forbid")

        value: int

    class LooseNested(BaseModel):
        count: int

    class LooseNestedParams(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)

        nested: LooseNested

    class NonStrictTool(_StrictTool):
        Params: ClassVar[type[BaseModel]] = NonStrictParams

    class LooseNestedTool(_StrictTool):
        Params: ClassVar[type[BaseModel]] = LooseNestedParams

    with pytest.raises(ToolContractError, match="strict=True"):
        ToolRegistry([NonStrictTool()])
    with pytest.raises(ToolContractError, match="nested parameter model LooseNested"):
        ToolRegistry([LooseNestedTool()])


def test_registry_rejects_scalar_coercion_and_unknown_nested_fields() -> None:
    registry = ToolRegistry([_StrictTool()])

    scalar = registry.prepare_args("strict_probe", {"value": "1"})
    nested = registry.prepare_args(
        "strict_probe",
        {"value": 1, "nested": {"count": 2, "unknown": True}},
    )
    nested_scalar = registry.prepare_args(
        "strict_probe",
        {"value": 1, "nested": '{"count":"2"}'},
    )

    assert scalar.error is not None
    assert nested.error is not None
    assert nested_scalar.error is not None
    assert nested_scalar.normalized_paths == ("nested",)


def test_strict_durable_task_params_keep_iso_datetime_provider_shape() -> None:
    class DurableParamsTool(_StrictTool):
        Params: ClassVar[type[BaseModel]] = CreateTaskParams

    registry = ToolRegistry([DurableParamsTool()])
    prepared = registry.prepare_args(
        "strict_probe",
        {
            "title": "Review",
            "objective": "Review the result",
            "closure_criteria": "Decision recorded",
            "execution_mode": "joint",
            "due_at": "2026-08-22T15:30:00Z",
        },
    )
    coerced = registry.prepare_args(
        "strict_probe",
        {
            "title": "Review",
            "objective": "Review the result",
            "closure_criteria": "Decision recorded",
            "execution_mode": "joint",
            "priority": "1",
        },
    )

    assert prepared.error is None
    assert prepared.args is not None
    assert prepared.args["due_at"] == "2026-08-22T15:30:00Z"
    assert coerced.error is not None


@pytest.mark.asyncio
async def test_duplicate_invalid_calls_in_one_response_receive_a_repair_iteration() -> None:
    settings = RickySettings(max_turn_iterations=10)
    session = AgentSession.create(settings, profile_scope=settings.resolve_profile_scope())
    provider = _Provider(
        [
            _tool_response(
                ToolCallPart(id="call_bad_1", name="strict_probe", args={"value": "1"}),
                ToolCallPart(id="call_bad_2", name="strict_probe", args={"value": "1"}),
            ),
            _tool_response(ToolCallPart(id="call_fixed", name="strict_probe", args={"value": 1})),
            _final_response("repaired"),
        ]
    )
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry([_StrictTool()]),
        settings=settings,
    )

    events = [event async for event in loop.run_turn(session, "validate it")]

    assert len(provider.requests) == 3
    assert [event.kind for event in events].count("tool_call_rejected") == 2
    assert [event.kind for event in events].count("tool_call_started") == 1
    assert not any(
        event.kind == "agent_error" and event.error_type == "ToolArgumentRepairLimit"
        for event in events
    )
    assert events[-1].kind == "turn_finished"
    assert events[-1].error is None
