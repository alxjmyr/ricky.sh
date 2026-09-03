"""OpenRouter provider adapter."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from typing import Any, Literal

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

OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"


OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


class OpenRouterProvider:
    """OpenRouter chat completions streaming adapter."""

    name = "openrouter"

    def __init__(
        self,
        settings: RickySettings,
        *,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        retry_base_seconds: float = 0.25,
        media_resolver: MediaResolver | None = None,
    ) -> None:
        if settings.openrouter_api_key is None:
            raise AuthError("OpenRouter API key is not configured")
        self._api_key = settings.openrouter_api_key
        self._timeout = settings.request_timeout_seconds
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
        """Fetch OpenRouter's live model catalog."""
        headers = {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
        }
        try:
            response = await self._client.get(
                OPENROUTER_MODELS_URL,
                headers=headers,
                timeout=self._timeout,
            )
            await _raise_for_status(response)
        except httpx.HTTPError as exc:
            raise TransportError(str(exc)) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise TransportError("Malformed OpenRouter model catalog") from exc

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise TransportError("Malformed OpenRouter model catalog")

        models: list[ModelInfo] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if not isinstance(model_id, str):
                continue
            context_length = item.get("context_length")
            name = item.get("name")
            architecture = item.get("architecture")
            advertised = (
                architecture.get("input_modalities") if isinstance(architecture, dict) else None
            )
            modalities: list[Literal["text", "image"]] = [
                modality
                for modality in ("text", "image")
                if isinstance(advertised, list) and modality in advertised
            ]
            if "text" not in modalities:
                modalities.insert(0, "text")
            models.append(
                ModelInfo(
                    id=model_id,
                    name=name if isinstance(name, str) else None,
                    context_length=context_length if isinstance(context_length, int) else None,
                    input_modalities=modalities,
                )
            )
        return models

    async def stream(
        self, request: CompletionRequest
    ) -> AsyncIterator[TextDelta | ThinkingDelta | ToolCallDelta | MessageDone]:
        """Stream a completion from OpenRouter."""
        for attempt in range(self._max_retries):
            yielded = False
            try:
                async for event in self._stream_once(request):
                    yielded = True
                    yield event
                return
            except ProviderError as exc:
                # A failure after events were yielded cannot be retried: replaying
                # the stream would duplicate deltas already seen by the consumer.
                if yielded or attempt >= self._max_retries - 1 or not is_retryable(exc):
                    raise
                await asyncio.sleep(retry_delay(self._retry_base_seconds, attempt))

    async def _stream_once(
        self, request: CompletionRequest
    ) -> AsyncIterator[TextDelta | ThinkingDelta | ToolCallDelta | MessageDone]:
        payload = await to_openrouter_request_with_media(request, self._media_resolver)
        headers = {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        text = ""
        thinking = ""
        usage = Usage()
        stop_reason: str | None = None
        tool_calls: dict[int, ToolCallState] = {}
        finished = False

        try:
            async with self._client.stream(
                "POST",
                OPENROUTER_CHAT_COMPLETIONS_URL,
                headers=headers,
                json=payload,
                timeout=self._timeout,
            ) as response:
                await _raise_for_status(response)
                async for data in _iter_sse_data(response):
                    if data == "[DONE]":
                        finished = True
                        break
                    chunk = loads_json_object(data, provider="OpenRouter")
                    chunk_usage = _usage_from_chunk(chunk)
                    if chunk_usage is not None:
                        usage = chunk_usage
                    for choice in chunk.get("choices", []):
                        stop_reason = choice.get("finish_reason") or stop_reason
                        delta = choice.get("delta") or {}

                        for reasoning_key in ("reasoning", "reasoning_content"):
                            reasoning_delta = delta.get(reasoning_key)
                            if isinstance(reasoning_delta, str) and reasoning_delta:
                                thinking += reasoning_delta
                                yield ThinkingDelta(delta=reasoning_delta)

                        content_delta = delta.get("content")
                        if isinstance(content_delta, str) and content_delta:
                            text += content_delta
                            yield TextDelta(delta=content_delta)

                        for tool_delta in delta.get("tool_calls") or []:
                            event = _tool_call_delta(tool_delta)
                            state = tool_calls.setdefault(event.index, ToolCallState())
                            if event.id is not None:
                                state.id = event.id
                            if event.name is not None:
                                state.name = event.name
                            state.args_json += event.args_delta
                            yield event
        except httpx.HTTPError as exc:
            raise TransportError(str(exc)) from exc

        if not finished:
            raise TransportError("OpenRouter stream ended before completion")

        yield MessageDone(
            message=assembled_message(text, thinking, tool_calls),
            usage=usage,
            stop_reason=stop_reason,
        )


def to_openrouter_request(request: CompletionRequest) -> dict[str, Any]:
    """Translate a canonical request to OpenRouter's chat-completions shape."""
    if _contains_images(request):
        raise UnsupportedInputModalityError(
            "OpenRouter image input requires a bound session media resolver"
        )
    return _request_payload(request, [_message_to_wire(message) for message in request.messages])


async def to_openrouter_request_with_media(
    request: CompletionRequest,
    resolver: MediaResolver | None,
) -> dict[str, Any]:
    """Translate text and digest-checked image parts to OpenRouter wire content."""
    if not _contains_images(request):
        return to_openrouter_request(request)
    if resolver is None:
        raise UnsupportedInputModalityError(
            "OpenRouter image input requires a bound session media resolver"
        )
    messages = [
        await _message_to_wire_with_media(message, resolver) for message in request.messages
    ]
    return _request_payload(request, messages)


def _request_payload(request: CompletionRequest, messages: list[dict[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": request.model,
        "messages": messages,
        "stream": True,
    }
    if request.tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in request.tools
        ]
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.max_tokens is not None:
        payload["max_tokens"] = request.max_tokens
    payload.update(request.provider_options)
    return payload


async def _message_to_wire_with_media(
    message: Message,
    resolver: MediaResolver,
) -> dict[str, Any]:
    if not any(isinstance(part, ImagePart) for part in message.content):
        return _message_to_wire(message)
    if message.role != "user":
        raise UnsupportedInputModalityError("OpenRouter images are supported only in user input")
    content: list[dict[str, Any]] = []
    for part in message.content:
        if isinstance(part, TextPart):
            content.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            resolved = await resolver.resolve(part.artifact)
            encoded = base64.b64encode(resolved.content).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{resolved.media_type};base64,{encoded}",
                    },
                }
            )
        else:
            raise ValueError("image-bearing user messages may contain only text and images")
    return {"role": "user", "content": content}


def _message_to_wire(message: Message) -> dict[str, Any]:
    text_parts = [part.text for part in message.content if isinstance(part, TextPart)]
    tool_results = [part for part in message.content if isinstance(part, ToolResultPart)]
    tool_calls = [part for part in message.content if isinstance(part, ToolCallPart)]

    if message.role == "tool":
        if len(tool_results) != 1:
            raise ValueError("tool messages must contain exactly one tool result part")
        result = tool_results[0]
        return {
            "role": "tool",
            "tool_call_id": result.call_id,
            "content": result.content,
        }

    wire: dict[str, Any] = {
        "role": message.role,
        "content": "\n".join(text_parts) if text_parts else None,
    }
    if tool_calls:
        wire["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.args, separators=(",", ":")),
                },
            }
            for call in tool_calls
        ]
    return wire


def _contains_images(request: CompletionRequest) -> bool:
    return any(
        isinstance(part, ImagePart) for message in request.messages for part in message.content
    )


async def _iter_sse_data(response: httpx.Response) -> AsyncIterator[str]:
    buffer: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            if buffer:
                yield "\n".join(buffer)
                buffer = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            buffer.append(line.removeprefix("data:").strip())
    if buffer:
        yield "\n".join(buffer)


async def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    body = await response.aread()
    detail = body.decode(errors="replace")
    if response.status_code in (401, 403):
        raise AuthError(error_message("OpenRouter authentication failed", detail))
    if response.status_code == 429:
        raise RateLimitError(error_message("OpenRouter rate limit exceeded", detail))
    if response.status_code == 400 and "context" in detail.lower():
        raise ContextLengthError(error_message("OpenRouter context length exceeded", detail))
    if (
        response.status_code == 400
        and "image" in detail.lower()
        and any(marker in detail.lower() for marker in ("unsupported", "not support", "modality"))
    ):
        raise UnsupportedInputModalityError(
            error_message("OpenRouter model does not support image input", detail)
        )
    if response.status_code >= 500:
        raise TransportError(error_message("OpenRouter server error", detail))
    raise ProviderError(error_message(f"OpenRouter HTTP {response.status_code}", detail))


def _usage_from_chunk(chunk: dict[str, Any]) -> Usage | None:
    usage = chunk.get("usage")
    if not isinstance(usage, dict):
        return None
    return Usage(
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
    )


def _tool_call_delta(delta: dict[str, Any]) -> ToolCallDelta:
    function = delta.get("function") or {}
    return ToolCallDelta(
        index=int(delta.get("index") or 0),
        id=delta.get("id"),
        name=function.get("name"),
        args_delta=function.get("arguments") or "",
    )
