"""Anthropic Messages API provider adapter."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ricky.config import RickySettings
from ricky.llm._assembly import ToolCallState, assembled_message, error_message, loads_json_object
from ricky.llm._retry import is_retryable, retry_delay
from ricky.llm.provider import MediaResolver
from ricky.llm.types import (
    AuthError,
    CompletionRequest,
    ContextLengthError,
    ImagePart,
    Message,
    MessageDone,
    ModelInfo,
    ProviderError,
    RateLimitError,
    TextDelta,
    TextPart,
    ThinkingDelta,
    ToolCallDelta,
    ToolCallPart,
    ToolResultPart,
    TransportError,
    UnsupportedInputModalityError,
    Usage,
)

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider:
    """Anthropic Messages API streaming adapter."""

    name = "anthropic"

    def __init__(
        self,
        settings: RickySettings,
        *,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        retry_base_seconds: float = 0.25,
        media_resolver: MediaResolver | None = None,
    ) -> None:
        if settings.anthropic_api_key is None:
            raise AuthError("Anthropic API key is not configured")
        self._api_key = settings.anthropic_api_key
        self._timeout = settings.request_timeout_seconds
        self._default_max_tokens = settings.providers.anthropic.default_max_tokens
        self._client = client or httpx.AsyncClient(timeout=self._timeout)
        self._owns_client = client is None
        self._max_retries = max(1, max_retries)
        self._retry_base_seconds = retry_base_seconds
        self._media_resolver = media_resolver

    def bind_media_resolver(self, resolver: MediaResolver) -> None:
        """Bind image materialization to this provider's exact session runtime."""
        self._media_resolver = resolver

    async def aclose(self) -> None:
        """Close the owned HTTP client."""
        if self._owns_client:
            await self._client.aclose()

    async def list_models(self) -> list[ModelInfo]:
        """Fetch Anthropic's complete paginated model catalog."""
        models: list[ModelInfo] = []
        after_id: str | None = None
        while True:
            params: dict[str, str | int] = {"limit": 100}
            if after_id is not None:
                params["after_id"] = after_id
            try:
                response = await self._client.get(
                    ANTHROPIC_MODELS_URL,
                    headers=self._headers(include_content_type=False),
                    params=params,
                    timeout=self._timeout,
                )
                await _raise_for_status(response)
            except httpx.HTTPError as exc:
                raise TransportError(str(exc)) from exc

            try:
                payload = response.json()
            except ValueError as exc:
                raise TransportError("Malformed Anthropic model catalog") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise TransportError("Malformed Anthropic model catalog")

            for item in payload["data"]:
                if not isinstance(item, dict):
                    continue
                model_id = item.get("id")
                if not isinstance(model_id, str):
                    continue
                display_name = item.get("display_name")
                models.append(
                    ModelInfo(
                        id=model_id,
                        name=display_name if isinstance(display_name, str) else None,
                    )
                )

            if payload.get("has_more") is not True:
                return models
            last_id = payload.get("last_id")
            if not isinstance(last_id, str) or not last_id or last_id == after_id:
                raise TransportError("Malformed Anthropic model catalog pagination")
            after_id = last_id

    async def stream(
        self, request: CompletionRequest
    ) -> AsyncIterator[TextDelta | ThinkingDelta | ToolCallDelta | MessageDone]:
        """Stream a completion from Anthropic."""
        for attempt in range(self._max_retries):
            yielded = False
            try:
                async for event in self._stream_once(request):
                    yielded = True
                    yield event
                return
            except ProviderError as exc:
                if yielded or attempt >= self._max_retries - 1 or not is_retryable(exc):
                    raise
                await asyncio.sleep(retry_delay(self._retry_base_seconds, attempt))

    async def _stream_once(
        self, request: CompletionRequest
    ) -> AsyncIterator[TextDelta | ThinkingDelta | ToolCallDelta | MessageDone]:
        payload = await to_anthropic_request_with_media(
            request,
            self._media_resolver,
            default_max_tokens=self._default_max_tokens,
        )
        text = ""
        thinking = ""
        usage = Usage()
        stop_reason: str | None = None
        tool_calls: dict[int, ToolCallState] = {}

        try:
            async with self._client.stream(
                "POST",
                ANTHROPIC_MESSAGES_URL,
                headers=self._headers(),
                json=payload,
                timeout=self._timeout,
            ) as response:
                await _raise_for_status(response)
                async for event_name, data in _iter_sse_events(response):
                    chunk = loads_json_object(data, provider="Anthropic")
                    chunk_type = chunk.get("type")
                    if event_name == "error" or chunk_type == "error":
                        _raise_stream_error(chunk)

                    if chunk_type == "message_start":
                        message = chunk.get("message")
                        if isinstance(message, dict):
                            usage = _merge_usage(usage, message.get("usage"))
                        continue

                    if chunk_type == "content_block_start":
                        index = _event_index(chunk)
                        block = chunk.get("content_block")
                        if not isinstance(block, dict):
                            raise TransportError("Malformed Anthropic content block")
                        block_type = block.get("type")
                        if block_type == "tool_use":
                            state = tool_calls.setdefault(index, ToolCallState())
                            tool_id = block.get("id")
                            name = block.get("name")
                            state.id = tool_id if isinstance(tool_id, str) else None
                            state.name = name if isinstance(name, str) else None
                            initial_input = block.get("input")
                            args_delta = ""
                            if isinstance(initial_input, dict) and initial_input:
                                args_delta = json.dumps(initial_input, separators=(",", ":"))
                                state.args_json += args_delta
                            yield ToolCallDelta(
                                index=index,
                                id=state.id,
                                name=state.name,
                                args_delta=args_delta,
                            )
                        elif block_type == "text":
                            initial_text = block.get("text")
                            if isinstance(initial_text, str) and initial_text:
                                text += initial_text
                                yield TextDelta(delta=initial_text)
                        elif block_type == "thinking":
                            initial_thinking = block.get("thinking")
                            if isinstance(initial_thinking, str) and initial_thinking:
                                thinking += initial_thinking
                                yield ThinkingDelta(delta=initial_thinking)
                        continue

                    if chunk_type == "content_block_delta":
                        index = _event_index(chunk)
                        delta = chunk.get("delta")
                        if not isinstance(delta, dict):
                            raise TransportError("Malformed Anthropic content delta")
                        delta_type = delta.get("type")
                        if delta_type == "text_delta":
                            value = delta.get("text")
                            if isinstance(value, str) and value:
                                text += value
                                yield TextDelta(delta=value)
                        elif delta_type == "thinking_delta":
                            value = delta.get("thinking")
                            if isinstance(value, str) and value:
                                thinking += value
                                yield ThinkingDelta(delta=value)
                        elif delta_type == "input_json_delta":
                            value = delta.get("partial_json")
                            if not isinstance(value, str):
                                raise TransportError("Malformed Anthropic tool input delta")
                            state = tool_calls.setdefault(index, ToolCallState())
                            state.args_json += value
                            yield ToolCallDelta(index=index, args_delta=value)
                        continue

                    if chunk_type == "message_delta":
                        delta = chunk.get("delta")
                        if isinstance(delta, dict) and isinstance(delta.get("stop_reason"), str):
                            stop_reason = delta["stop_reason"]
                        usage = _merge_usage(usage, chunk.get("usage"))
                        continue

                    if chunk_type == "message_stop":
                        yield MessageDone(
                            message=assembled_message(text, thinking, tool_calls),
                            usage=usage,
                            stop_reason=_map_stop_reason(stop_reason),
                        )
                        return
        except httpx.HTTPError as exc:
            raise TransportError(str(exc)) from exc

        raise TransportError("Anthropic stream ended before message_stop")

    def _headers(self, *, include_content_type: bool = True) -> dict[str, str]:
        headers = {
            "x-api-key": self._api_key.get_secret_value(),
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if include_content_type:
            headers["content-type"] = "application/json"
        return headers


def to_anthropic_request(
    request: CompletionRequest,
    *,
    default_max_tokens: int = 8192,
) -> dict[str, Any]:
    """Translate a canonical request to Anthropic's Messages API shape."""
    if _contains_images(request):
        raise UnsupportedInputModalityError(
            "Anthropic image input requires a bound session media resolver"
        )
    return _anthropic_request_payload(
        request,
        [_message_to_wire(message) for message in request.messages if message.role != "system"],
        default_max_tokens=default_max_tokens,
    )


async def to_anthropic_request_with_media(
    request: CompletionRequest,
    resolver: MediaResolver | None,
    *,
    default_max_tokens: int = 8192,
) -> dict[str, Any]:
    """Translate canonical text and digest-checked images to Anthropic blocks."""
    if not _contains_images(request):
        return to_anthropic_request(request, default_max_tokens=default_max_tokens)
    if resolver is None:
        raise UnsupportedInputModalityError(
            "Anthropic image input requires a bound session media resolver"
        )
    messages = [
        await _message_to_wire_with_media(message, resolver)
        for message in request.messages
        if message.role != "system"
    ]
    return _anthropic_request_payload(
        request,
        messages,
        default_max_tokens=default_max_tokens,
    )


def _anthropic_request_payload(
    request: CompletionRequest,
    messages: list[dict[str, Any]],
    *,
    default_max_tokens: int,
) -> dict[str, Any]:
    system_parts: list[str] = []
    for message in request.messages:
        if message.role == "system":
            system_parts.extend(part.text for part in message.content if isinstance(part, TextPart))

    payload: dict[str, Any] = {
        "model": request.model,
        "messages": messages,
        "max_tokens": request.max_tokens or default_max_tokens,
        "stream": True,
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    if request.tools:
        payload["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters,
            }
            for tool in request.tools
        ]
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    payload.update(request.provider_options)
    return payload


async def _message_to_wire_with_media(
    message: Message,
    resolver: MediaResolver,
) -> dict[str, Any]:
    if not any(isinstance(part, ImagePart) for part in message.content):
        return _message_to_wire(message)
    if message.role != "user":
        raise UnsupportedInputModalityError("Anthropic images are supported only in user input")
    content: list[dict[str, Any]] = []
    for part in message.content:
        if isinstance(part, TextPart):
            content.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            resolved = await resolver.resolve(part.artifact)
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": resolved.media_type,
                        "data": base64.b64encode(resolved.content).decode("ascii"),
                    },
                }
            )
        else:
            raise ValueError("image-bearing user messages may contain only text and images")
    return {"role": "user", "content": content}


def _message_to_wire(message: Message) -> dict[str, Any]:
    role = "user" if message.role == "tool" else message.role
    content: list[dict[str, Any]] = []
    for part in message.content:
        if isinstance(part, TextPart):
            content.append({"type": "text", "text": part.text})
        elif isinstance(part, ToolCallPart):
            content.append(
                {
                    "type": "tool_use",
                    "id": part.id,
                    "name": part.name,
                    "input": part.args,
                }
            )
        elif isinstance(part, ToolResultPart):
            content.append(
                {
                    "type": "tool_result",
                    "tool_use_id": part.call_id,
                    "content": part.content,
                    "is_error": part.is_error,
                }
            )
    return {"role": role, "content": content}


def _contains_images(request: CompletionRequest) -> bool:
    return any(
        isinstance(part, ImagePart) for message in request.messages for part in message.content
    )


async def _iter_sse_events(response: httpx.Response) -> AsyncIterator[tuple[str | None, str]]:
    event_name: str | None = None
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())
    if data_lines:
        yield event_name, "\n".join(data_lines)


async def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    body = await response.aread()
    detail = body.decode(errors="replace")
    lowered = detail.lower()
    if response.status_code in (401, 403):
        raise AuthError(error_message("Anthropic authentication failed", detail))
    if response.status_code == 429:
        raise RateLimitError(error_message("Anthropic rate limit exceeded", detail))
    if response.status_code == 400 and (
        "context" in lowered or "prompt is too long" in lowered or "too long" in lowered
    ):
        raise ContextLengthError(error_message("Anthropic context length exceeded", detail))
    if (
        response.status_code == 400
        and "image" in lowered
        and any(marker in lowered for marker in ("unsupported", "not support", "modality"))
    ):
        raise UnsupportedInputModalityError(
            error_message("Anthropic model does not support image input", detail)
        )
    if response.status_code == 529 or response.status_code >= 500:
        raise TransportError(error_message("Anthropic server error", detail))
    raise ProviderError(error_message(f"Anthropic HTTP {response.status_code}", detail))


def _raise_stream_error(chunk: dict[str, Any]) -> None:
    error = chunk.get("error")
    if not isinstance(error, dict):
        raise ProviderError("Anthropic stream error")
    error_type = error.get("type")
    message = error.get("message")
    detail = message if isinstance(message, str) else str(error_type or "unknown error")
    if error_type in {"authentication_error", "permission_error"}:
        raise AuthError(detail)
    if error_type == "rate_limit_error":
        raise RateLimitError(detail)
    if error_type in {"overloaded_error", "api_error"}:
        raise TransportError(detail)
    if error_type == "invalid_request_error" and (
        "context" in detail.lower() or "too long" in detail.lower()
    ):
        raise ContextLengthError(detail)
    if (
        error_type == "invalid_request_error"
        and "image" in detail.lower()
        and any(marker in detail.lower() for marker in ("unsupported", "not support", "modality"))
    ):
        raise UnsupportedInputModalityError(detail)
    raise ProviderError(detail)


def _event_index(chunk: dict[str, Any]) -> int:
    index = chunk.get("index")
    if not isinstance(index, int):
        raise TransportError("Malformed Anthropic stream event index")
    return index


def _merge_usage(current: Usage, raw: Any) -> Usage:
    if not isinstance(raw, dict):
        return current
    input_tokens = raw.get("input_tokens")
    output_tokens = raw.get("output_tokens")
    return Usage(
        prompt_tokens=input_tokens if isinstance(input_tokens, int) else current.prompt_tokens,
        completion_tokens=(
            output_tokens if isinstance(output_tokens, int) else current.completion_tokens
        ),
    )


def _map_stop_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    return {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "tool_use": "tool_calls",
        "max_tokens": "length",
    }.get(reason, reason)
