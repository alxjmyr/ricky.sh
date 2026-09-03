"""Private-context model and read-only agent tasks for Workflow."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Collection
from typing import Literal

from pydantic import BaseModel, Field, JsonValue

from ricky.llm.provider import Provider, collect
from ricky.llm.types import (
    CompletionRequest,
    Message,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolSpec,
    Usage,
)
from ricky.workflows.schema import ResultSchema, SchemaValueError, validate_result

SYSTEM_CONTRACT = """You execute one isolated workflow task.
Use only the instruction and named inputs in this request.
Return exactly one JSON object that matches the result schema.
Do not use Markdown fences. Do not add prose before or after the JSON object."""


class InputMetric(BaseModel):
    name: str
    value_type: str
    chars: int


class ModelTaskMetrics(BaseModel):
    instruction_chars: int
    inputs: list[InputMetric]
    result_schema_name: str
    tool_count: int
    message_count: int
    total_request_chars: int


class ModelTaskResult(BaseModel):
    """Validated output and private task accounting."""

    output: JsonValue
    usage: Usage = Field(default_factory=Usage)
    attempts: int
    iterations: int
    metrics: ModelTaskMetrics


class ToolExecutionResult(BaseModel):
    """The model-task view of one allowlisted read-only tool result."""

    content: str
    is_error: bool = False


ToolExecutor = Callable[[ToolCallPart], Awaitable[ToolExecutionResult]]
FailureCategory = Literal["invalid_output", "provider_error", "timeout"]


class ModelTaskFailure(Exception):
    """A typed bounded task failure for the workflow runner."""

    def __init__(
        self,
        category: FailureCategory,
        message: str,
        *,
        attempts: int,
        iterations: int,
        usage: Usage,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.attempts = attempts
        self.iterations = iterations
        self.usage = usage


async def run_model_task(
    *,
    provider: Provider,
    model: str,
    instruction: str,
    inputs: dict[str, JsonValue],
    result_schema_name: str,
    result_schema: ResultSchema,
    max_attempts: int,
    retry_on: Collection[str] = ("invalid_output", "provider_error"),
    skill_body: str | None = None,
    max_result_chars: int = 120_000,
) -> ModelTaskResult:
    """Run isolated one-request attempts until one strict result validates."""

    usage = Usage()
    last_category: FailureCategory = "invalid_output"
    last_error = "model task did not run"
    metrics = _metrics(
        instruction,
        inputs,
        result_schema_name,
        result_schema,
        tool_count=0,
        skill_body=skill_body,
    )
    for attempt in range(1, max_attempts + 1):
        validation_error = last_error if attempt > 1 else None
        request = _request(
            model=model,
            instruction=instruction,
            inputs=inputs,
            result_schema=result_schema,
            tools=[],
            skill_body=skill_body,
            validation_error=validation_error,
        )
        try:
            message, request_usage = await collect(provider.stream(request))
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            last_category = "timeout"
            last_error = str(exc) or "model request timed out"
            if "timeout" not in retry_on or attempt == max_attempts:
                raise ModelTaskFailure(
                    last_category,
                    last_error,
                    attempts=attempt,
                    iterations=attempt,
                    usage=usage,
                ) from exc
            continue
        except Exception as exc:  # noqa: BLE001 - provider boundary becomes typed failure.
            last_category = "provider_error"
            last_error = str(exc) or type(exc).__name__
            if "provider_error" not in retry_on or attempt == max_attempts:
                raise ModelTaskFailure(
                    last_category,
                    last_error,
                    attempts=attempt,
                    iterations=attempt,
                    usage=usage,
                ) from exc
            continue
        usage = _add_usage(usage, request_usage)
        try:
            output = _validated_output(message, result_schema, max_result_chars)
        except (ValueError, SchemaValueError) as exc:
            last_category = "invalid_output"
            last_error = _bounded_error(exc)
            if "invalid_output" not in retry_on or attempt == max_attempts:
                raise ModelTaskFailure(
                    last_category,
                    last_error,
                    attempts=attempt,
                    iterations=attempt,
                    usage=usage,
                ) from exc
            continue
        return ModelTaskResult(
            output=output,
            usage=usage,
            attempts=attempt,
            iterations=attempt,
            metrics=metrics,
        )
    raise ModelTaskFailure(
        last_category,
        last_error,
        attempts=max_attempts,
        iterations=max_attempts,
        usage=usage,
    )


async def run_agent_task(
    *,
    provider: Provider,
    model: str,
    instruction: str,
    inputs: dict[str, JsonValue],
    result_schema_name: str,
    result_schema: ResultSchema,
    tools: list[ToolSpec],
    execute_tool: ToolExecutor,
    max_iterations: int,
    max_attempts: int,
    retry_on: Collection[str] = ("invalid_output", "provider_error"),
    skill_body: str | None = None,
    max_result_chars: int = 120_000,
) -> ModelTaskResult:
    """Run one private bounded loop whose tools are prevalidated read-only."""

    usage = Usage()
    attempts = 1
    invalid_results = 0
    messages = _messages(
        instruction=instruction,
        inputs=inputs,
        result_schema=result_schema,
        skill_body=skill_body,
        validation_error=None,
    )
    metrics = _metrics(
        instruction,
        inputs,
        result_schema_name,
        result_schema,
        tool_count=len(tools),
        skill_body=skill_body,
    )
    for iteration in range(1, max_iterations + 1):
        request = CompletionRequest(model=model, messages=list(messages), tools=tools)
        try:
            message, request_usage = await collect(provider.stream(request))
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            if "timeout" not in retry_on or attempts >= max_attempts:
                raise ModelTaskFailure(
                    "timeout",
                    str(exc) or "agent request timed out",
                    attempts=attempts,
                    iterations=iteration,
                    usage=usage,
                ) from exc
            attempts += 1
            messages = _messages(
                instruction=instruction,
                inputs=inputs,
                result_schema=result_schema,
                skill_body=skill_body,
                validation_error=f"timeout: {_bounded_error(exc)}",
            )
            continue
        except Exception as exc:  # noqa: BLE001 - provider boundary becomes typed failure.
            if "provider_error" not in retry_on or attempts >= max_attempts:
                raise ModelTaskFailure(
                    "provider_error",
                    str(exc) or type(exc).__name__,
                    attempts=attempts,
                    iterations=iteration,
                    usage=usage,
                ) from exc
            attempts += 1
            messages = _messages(
                instruction=instruction,
                inputs=inputs,
                result_schema=result_schema,
                skill_body=skill_body,
                validation_error=f"provider error: {_bounded_error(exc)}",
            )
            continue
        usage = _add_usage(usage, request_usage)
        calls = [part for part in message.content if isinstance(part, ToolCallPart)]
        if calls:
            messages.append(message)
            results = await asyncio.gather(*(execute_tool(call) for call in calls))
            for call, result in zip(calls, results, strict=True):
                messages.append(
                    Message(
                        role="tool",
                        content=[
                            ToolResultPart(
                                call_id=call.id,
                                content=result.content,
                                is_error=result.is_error,
                            )
                        ],
                    )
                )
            continue
        try:
            output = _validated_output(message, result_schema, max_result_chars)
        except (ValueError, SchemaValueError) as exc:
            invalid_results += 1
            if "invalid_output" not in retry_on or invalid_results >= max_attempts:
                raise ModelTaskFailure(
                    "invalid_output",
                    _bounded_error(exc),
                    attempts=invalid_results,
                    iterations=iteration,
                    usage=usage,
                ) from exc
            messages.append(message)
            messages.append(
                Message.text(
                    "user",
                    "Your final JSON result failed validation. Return a corrected JSON "
                    f"object only. Validation error: {_bounded_error(exc)}",
                )
            )
            continue
        return ModelTaskResult(
            output=output,
            usage=usage,
            attempts=max(attempts, invalid_results + 1),
            iterations=iteration,
            metrics=metrics.model_copy(update={"message_count": len(messages) + 1}),
        )
    raise ModelTaskFailure(
        "invalid_output",
        f"agent task exceeded {max_iterations} iterations",
        attempts=max(attempts, invalid_results + 1),
        iterations=max_iterations,
        usage=usage,
    )


def _request(
    *,
    model: str,
    instruction: str,
    inputs: dict[str, JsonValue],
    result_schema: ResultSchema,
    tools: list[ToolSpec],
    skill_body: str | None,
    validation_error: str | None,
) -> CompletionRequest:
    return CompletionRequest(
        model=model,
        messages=_messages(
            instruction=instruction,
            inputs=inputs,
            result_schema=result_schema,
            skill_body=skill_body,
            validation_error=validation_error,
        ),
        tools=tools,
    )


def _messages(
    *,
    instruction: str,
    inputs: dict[str, JsonValue],
    result_schema: ResultSchema,
    skill_body: str | None,
    validation_error: str | None,
) -> list[Message]:
    system = SYSTEM_CONTRACT
    if skill_body is not None:
        system += f"\n\nDeclared instruction asset:\n{skill_body}"
    payload = {
        "instruction": instruction,
        "inputs": inputs,
        "result_schema": result_schema.model_dump(mode="json"),
    }
    user = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if validation_error is not None:
        user += f"\nValidation error from the prior attempt: {validation_error}"
    return [Message.text("system", system), Message.text("user", user)]


def _validated_output(
    message: Message,
    schema: ResultSchema,
    max_result_chars: int,
) -> JsonValue:
    if any(isinstance(part, ToolCallPart) for part in message.content):
        raise ValueError("final result contains a tool call")
    text = "".join(part.text for part in message.content if isinstance(part, TextPart))
    if not text:
        raise ValueError("final assistant text is empty")
    if len(text) > max_result_chars:
        raise ValueError(f"final result exceeds {max_result_chars} characters")
    if "```" in text:
        raise ValueError("Markdown fences are not allowed")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"final assistant text is not one JSON object: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("final assistant JSON must be an object")
    return validate_result(schema, value)


def _metrics(
    instruction: str,
    inputs: dict[str, JsonValue],
    result_schema_name: str,
    result_schema: ResultSchema,
    *,
    tool_count: int,
    skill_body: str | None,
) -> ModelTaskMetrics:
    input_metrics = [
        InputMetric(
            name=name,
            value_type=type(value).__name__,
            chars=len(json.dumps(value, ensure_ascii=False, sort_keys=True)),
        )
        for name, value in inputs.items()
    ]
    messages = _messages(
        instruction=instruction,
        inputs=inputs,
        result_schema=result_schema,
        skill_body=skill_body,
        validation_error=None,
    )
    total = sum(
        len(part.text)
        for message in messages
        for part in message.content
        if isinstance(part, TextPart)
    )
    return ModelTaskMetrics(
        instruction_chars=len(instruction),
        inputs=input_metrics,
        result_schema_name=result_schema_name,
        tool_count=tool_count,
        message_count=2,
        total_request_chars=total,
    )


def _bounded_error(error: BaseException, *, limit: int = 1_000) -> str:
    text = str(error).replace("\n", " ")
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def _add_usage(left: Usage, right: Usage) -> Usage:
    return Usage(
        prompt_tokens=left.prompt_tokens + right.prompt_tokens,
        completion_tokens=left.completion_tokens + right.completion_tokens,
    )
